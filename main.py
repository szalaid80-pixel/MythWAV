import os
import logging
from dataclasses import dataclass
from typing import Iterable
import discord
from discord import app_commands
from discord.ext import commands

# Logging beállítása
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("audiosearch")

# --- 1. KERESÉSI SEGÉDFÜGGVÉNYEK ---

@dataclass(frozen=True)
class SearchHit:
    message: discord.Message
    location: str
    matches: tuple[str, ...]

def matching_items(message: discord.Message, query: str) -> tuple[str, ...]:
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

def has_search_permissions(channel: object, member: discord.Member) -> bool:
    permissions = channel.permissions_for(member)
    return permissions.view_channel and permissions.read_message_history

def _location(channel: discord.abc.GuildChannel | discord.Thread) -> str:
    parent = getattr(channel, "parent", None)
    if isinstance(channel, discord.Thread) and parent:
        return f"{parent.name} / {channel.name}"
    return getattr(channel, "name", str(channel.id))

async def _add_archived_threads(parent: object, collected: dict[int, discord.Thread]) -> None:
    archived_threads = getattr(parent, "archived_threads", None)
    if archived_threads is None:
        return
    try:
        iterator = archived_threads(limit=None)
        async for thread in iterator:
            collected[thread.id] = thread
    except Exception as exc:
        log.info("Archived threads log: %s", exc)

async def _collect_targets(guild: discord.Guild, channel_ids: Iterable[int]):
    parents: list[object] = []
    targets: dict[int, discord.abc.Messageable] = {}
    warnings: list[str] = []

    for channel_id in channel_ids:
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except Exception as exc:
                warnings.append(f"Nem érhető el a csatorna ({channel_id}): {exc}")
                continue
        if getattr(channel, "guild", None) is None or channel.guild.id != guild.id:
            continue
        if hasattr(channel, "history"):
            targets[channel.id] = channel
        parents.append(channel)

    active_by_parent: dict[int, list[discord.Thread]] = {}
    try:
        for thread in await guild.active_threads():
            active_by_parent.setdefault(thread.parent_id, []).append(thread)
    except Exception:
        for thread in guild.threads:
            active_by_parent.setdefault(thread.parent_id, []).append(thread)

    for parent in parents:
        for thread in active_by_parent.get(parent.id, []):
            targets[thread.id] = thread
        for thread in getattr(parent, "threads", []):
            targets[thread.id] = thread
        await _add_archived_threads(parent, targets)

    return list(targets.values()), warnings

async def search_guild(
    guild: discord.Guild,
    channel_ids: Iterable[int],
    query: str,
    requester: discord.Member,
    max_messages_per_channel: int | None = None,
):
    targets, warnings = await _collect_targets(guild, channel_ids)
    hits: list[SearchHit] = []
    bot_member = guild.me

    for channel in targets:
        try:
            if not has_search_permissions(channel, requester):
                continue

            if bot_member is not None:
                permissions = channel.permissions_for(bot_member)
                if not permissions.view_channel or not permissions.read_message_history:
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
        except Exception as exc:
            warnings.append(f"Hiba a keresés során ({_location(channel)}): {exc}")

    hits.sort(key=lambda hit: hit.message.created_at, reverse=True)
    return hits, warnings

# --- 2. DISCORD BOT INICIALIZÁLÁSA ÉS PARANCSOK ---

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Ide írd be azoknak a csatornáknak az ID-jét, amikben keresni szeretnél:
# Példa: CHANNEL_IDS = [123456789012345678, 876543210987654321]
CHANNEL_IDS = [] 

@bot.event
async def on_ready():
    print(f"Bot bejelentkezve mint: {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Szinkronizálva {len(synced)} parancs.")
    except Exception as e:
        print(f"Hiba a parancsok szinkronizálásakor: {e}")

@bot.tree.command(name="audiosearch", description="Keresés az audio csatornákon")
async def audio_search(interaction: discord.Interaction, query: str):
    # Válaszadási idő hosszabbítása (megakadályozza a 3 mp-es "did not respond" hibát)
    await interaction.response.defer(ephemeral=True)

    # CSATORNA CHECK
    channels_to_search = CHANNEL_IDS if CHANNEL_IDS else [ch.id for ch in interaction.guild.text_channels]

    hits, warnings = await search_guild(
        interaction.guild,
        channels_to_search,
        query,
        interaction.user
    )

    if not hits:
        await interaction.followup.send(f"Nincs találat erre: `{query}`", ephemeral=True)
        return

    response_text = f"**Találatok erre: `{query}` ({len(hits)} db):**\n"
    for hit in hits[:5]:  # Az első 5 találat megjelenítése
        matches_str = ", ".join(hit.matches)
        response_text += f"- **{hit.location}**: [{matches_str}]({hit.message.jump_url})\n"

    await interaction.followup.send(response_text, ephemeral=True)

# Bot indítása a környezeti változóból (Railway DISCORD_TOKEN / TOKEN)
token = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")
if token:
    bot.run(token)
else:
    print("HIBA: Nem található a DISCORD_TOKEN környezeti változó!")
