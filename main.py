"""Search helpers for configured Discord channels and their threads."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

import discord

log = logging.getLogger("audiosearch")


@dataclass(frozen=True)
class SearchHit:
    message: discord.Message
    location: str
    matches: tuple[str, ...]


def matching_items(message: discord.Message, query: str) -> tuple[str, ...]:
    """Return only individual lines or filenames that contain every query term."""
    terms = [term.casefold() for term in query.split() if term]
    if not terms:
        return ()

    candidates = list((message.content or "").splitlines())
    for embed in message.embeds:
        candidates.extend((embed.title or "").splitlines())
        candidates.extend((embed.description or "").splitlines())
        if embed.author and embed.author.name:
            candidates.extend(embed.author.name.splitlines())
        if embed.footer and embed.footer.text:
            candidates.extend(embed.footer.text.splitlines())
    candidates.extend(attachment.filename for attachment in message.attachments)

    matches: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        item = candidate.strip()
        folded_item = item.casefold()
        if item and all(term in folded_item for term in terms) and item not in seen:
            matches.append(item)
            seen.add(item)
    return tuple(matches)


def matches_query(message: discord.Message, query: str) -> bool:
    """Match when one line, embed line, or filename contains all query terms."""
    return bool(matching_items(message, query))


def has_search_permissions(channel: object, member: discord.Member) -> bool:
    """Prevent returning content from a channel the command user cannot read."""
    permissions = channel.permissions_for(member)  # type: ignore[attr-defined]
    return permissions.view_channel and permissions.read_message_history


def _location(channel: discord.abc.GuildChannel | discord.Thread) -> str:
    parent = getattr(channel, "parent", None)
    if isinstance(channel, discord.Thread) and parent:
        return f"{parent.name} / {channel.name}"
    return getattr(channel, "name", str(channel.id))


async def _add_archived_threads(parent: object, collected: dict[int, discord.Thread]) -> None:
    """Load archived posts/threads where the parent channel supports it."""
    archived_threads = getattr(parent, "archived_threads", None)
    if archived_threads is None:
        return
    try:
        iterator = archived_threads(limit=None)
        async for thread in iterator:
            collected[thread.id] = thread
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.info("Could not read archived threads for channel %s: %s", parent.id, exc)
    except TypeError:
        # Older discord.py versions may not accept limit=None.
        try:
            iterator = archived_threads()
            async for thread in iterator:
                collected[thread.id] = thread
        except (discord.Forbidden, discord.HTTPException, TypeError) as exc:
            log.info("Could not read archived threads for channel %s: %s", parent.id, exc)

    # Text channels can also contain private archived threads. Discord only
    # returns those the bot is allowed to view (or manage).
    if isinstance(parent, discord.TextChannel):
        try:
            async for thread in parent.archived_threads(
                private=True, joined=False, limit=None
            ):
                collected[thread.id] = thread
        except (discord.Forbidden, discord.HTTPException, TypeError) as exc:
            log.info("Could not read private archived threads for channel %s: %s", parent.id, exc)


async def _collect_targets(
    guild: discord.Guild,
    channel_ids: Iterable[int],
) -> tuple[list[discord.abc.Messageable], list[str]]:
    """Resolve configured parents and discover active/archived child threads."""
    parents: list[object] = []
    targets: dict[int, discord.abc.Messageable] = {}
    warnings: list[str] = []

    for channel_id in channel_ids:
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                warnings.append(f"Could not access configured channel {channel_id}: {exc}")
                continue
        if getattr(channel, "guild", None) is None or channel.guild.id != guild.id:
            warnings.append(f"Configured channel {channel_id} is not in this server.")
            continue
        if hasattr(channel, "history"):
            targets[channel.id] = channel
        parents.append(channel)

    # Fetch active threads instead of relying only on the local cache.
    active_by_parent: dict[int, list[discord.Thread]] = {}
    try:
        for thread in await guild.active_threads():
            active_by_parent.setdefault(thread.parent_id, []).append(thread)
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.info("Could not fetch all active threads in guild %s: %s", guild.id, exc)
        for thread in guild.threads:
            active_by_parent.setdefault(thread.parent_id, []).append(thread)

    for parent in parents:
        for thread in active_by_parent.get(parent.id, []):
            targets[thread.id] = thread
        # ForumChannel.threads and TextChannel.threads include cached active threads.
        for thread in getattr(parent, "threads", []):
            targets[thread.id] = thread
        await _add_archived_threads(parent, targets)  # type: ignore[arg-type]

    return list(targets.values()), warnings


async def search_guild(
    guild: discord.Guild,
    channel_ids: Iterable[int],
    query: str,
    requester: discord.Member,
    max_messages_per_channel: int | None = None,
) -> tuple[list[SearchHit], list[str]]:
    """Search selected channel history, active threads, and archived posts."""
    targets, warnings = await _collect_targets(guild, channel_ids)
    hits: list[SearchHit] = []
    bot_member = guild.me

    for channel in targets:
        try:
            # Search only sources the invoking member can already read. This
            # avoids disclosing private channel or thread content in an
            # ephemeral result to someone who lacks access.
            if not has_search_permissions(channel, requester):
                continue

            if bot_member is not None:
                permissions = channel.permissions_for(bot_member)
                if not permissions.view_channel or not permissions.read_message_history:
                    warnings.append(f"Missing View Channel / Read Message History in {_location(channel)}.")
                    continue

            async for message in channel.history(
                limit=max_messages_per_channel,
                oldest_first=False,
            ):
                matched_items = matching_items(message, query)
                if matched_items:
                    hits.append(
                        SearchHit(
                            message=message,
                            location=_location(channel),
                            matches=matched_items,
                        )
                    )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            warnings.append(f"Could not search {_location(channel)}: {exc}")
        except AttributeError:
            # Some channel types are not messageable in the installed Discord API.
            continue

    hits.sort(key=lambda hit: hit.message.created_at, reverse=True)
    return hits, warnings
