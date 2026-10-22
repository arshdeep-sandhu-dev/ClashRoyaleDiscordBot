"""CWL rosters for the clan family.

Start with /roster setup (link each clan to its in-game tag) and /roster sync (load every clan member from the
Clash of Clans API into the player pool), then build the rosters from the pool with /roster add and /roster move.
"""

import asyncio
import json
import os
import re
from collections import Counter
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands

DATA_FILE = os.path.join(os.path.dirname(__file__), "roster_data.json")

# key used in the saved data -> name shown in Discord
CLANS = {
    "charge": "Chaotic Charge",
    "climax": "Chaotic Climax",
    "charge2": "Chaotic Charge 2",
}
CLAN_CHOICES = [app_commands.Choice(name=name, value=key) for key, name in CLANS.items()]
ROSTER_SIZE = 20

GREEN = discord.Color.from_rgb(59, 165, 93)

# Player and clan tags only ever use these characters
TAG_CHARS = set("0289PYLQGRJCUV")


# ---------- Data (saved per server in roster_data.json) ----------
# {guild_id: {
#     "clan_tags": {"charge": "2PL29L2GP", ...},
#     "players": {"8LGGY9QRR": {"name": "CC⚡️CallMeJAS", "th": 18, "heroes": 480, "clan": "charge"}, ...},
#         the pool: everyone in the clans (clan = which one they're in), plus anyone on a roster
#     "rosters": {"charge": ["8LGGY9QRR", ...], ...},  a player is on at most one roster
#     "board": {"channel_id": ..., "message_id": ...}}}


def load_data() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def guild_data(data: dict, guild: discord.Guild) -> dict:
    g = data.setdefault(str(guild.id), {})
    g.setdefault("clan_tags", {})
    players = g.setdefault("players", {})
    rosters = g.setdefault("rosters", {})
    seen = set()
    for clan in CLANS:
        tags = []
        for entry in rosters.get(clan, []):
            if isinstance(entry, dict):  # saved by the first version, which kept names on the roster
                players.setdefault(entry["tag"], {"name": entry["name"], "th": None, "heroes": None, "clan": None})
                entry = entry["tag"]
            if entry not in seen:  # a player can only be on one roster
                seen.add(entry)
                tags.append(entry)
        rosters[clan] = tags
    return g


# ---------- Helpers ----------


def normalize_tag(text: str) -> str | None:
    """'#pu8029yo' -> 'PU8029Y0'. Returns None if it can't be a tag."""
    # People often type the letter O for a zero
    tag = re.sub(r"[^A-Z0-9]", "", text.upper()).replace("O", "0")
    return tag if len(tag) >= 3 and set(tag) <= TAG_CHARS else None


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' * (count != 1)}"


def info_of(g: dict, tag: str) -> dict:
    return g["players"].get(tag, {})


def roster_of(g: dict, tag: str) -> str | None:
    return next((clan for clan, tags in g["rosters"].items() if tag in tags), None)


def rostered(g: dict) -> set[str]:
    return {tag for tags in g["rosters"].values() for tag in tags}


def unassigned(g: dict) -> list[str]:
    on_roster = rostered(g)
    return [tag for tag in g["players"] if tag not in on_roster]


def strength(g: dict, tag: str):
    """Sort key: highest Town Hall first, then highest heroes."""
    info = info_of(g, tag)
    return -(info.get("th") or 0), -(info.get("heroes") or 0), (info.get("name") or "").lower()


def player_line(tag: str, info: dict) -> str:
    """#️⃣ 8LGGY9QRR, 🏷️ CC⚡️CallMeJAS, 🏠 18, 👑 480"""
    parts = [f"#️⃣ {tag}"]
    if info.get("name"):
        parts.append(f"🏷️ {discord.utils.escape_markdown(info['name'])}")
    if info.get("th"):
        parts.append(f"🏠 {info['th']}")
    if info.get("heroes") is not None:
        parts.append(f"👑 {info['heroes']}")
    return ", ".join(parts)


def option_label(tag: str, info: dict) -> str:
    """Plain text for dropdowns: CC⚡️CallMeJAS · 🏠18 · 👑480"""
    parts = [info.get("name") or f"#{tag}"]
    if info.get("th"):
        parts.append(f"🏠{info['th']}")
    if info.get("heroes") is not None:
        parts.append(f"👑{info['heroes']}")
    return " · ".join(parts)


def roster_embed(g: dict, clan: str) -> discord.Embed:
    """The numbered 1-20 list, like the roster spreadsheet."""
    tags = g["rosters"][clan]
    lines = [
        f"`{i + 1:>2}.` {player_line(tags[i], info_of(g, tags[i]))}" if i < len(tags) else f"`{i + 1:>2}.`"
        for i in range(ROSTER_SIZE)
    ]
    embed = discord.Embed(title=CLANS[clan], description="\n".join(lines), color=GREEN)
    embed.set_footer(text=f"{len(tags)}/{ROSTER_SIZE} players")
    return embed


def board_embeds(g: dict) -> list[discord.Embed]:
    return [roster_embed(g, clan) for clan in CLANS]


async def refresh_board(guild: discord.Guild):
    """Updates the message posted with /roster post, if there is one."""
    data = load_data()
    board = data.get(str(guild.id), {}).get("board")
    if not board:
        return
    channel = guild.get_channel_or_thread(board["channel_id"])
    if channel is None:
        return
    try:
        await channel.get_partial_message(board["message_id"]).edit(embeds=board_embeds(guild_data(data, guild)))
    except discord.HTTPException as e:
        print(f"Couldn't update the roster board: {e}")


def add_error(g: dict, clan: str, tag: str) -> str | None:
    """Why this player can't be added to this roster, or None if they can."""
    current = roster_of(g, tag)
    if current:
        return (
            f"**{option_label(tag, info_of(g, tag))}** is already on the **{CLANS[current]}** roster. "
            "Use `/roster move` to move them."
        )
    if len(g["rosters"][clan]) >= ROSTER_SIZE:
        return f"The **{CLANS[clan]}** roster is full ({ROSTER_SIZE} players). Remove someone first."
    return None


def place(g: dict, tag: str, clan: str, position: int | None = None) -> int:
    """Puts a player on a roster (taking them off any other one). Returns their index."""
    for tags in g["rosters"].values():
        if tag in tags:
            tags.remove(tag)
    roster = g["rosters"][clan]
    index = len(roster) if position is None else min(position - 1, len(roster))
    roster.insert(index, tag)
    return index


# ---------- Clash of Clans API ----------


class CocError(Exception):
    """An API problem, with a message that can be shown in Discord as is."""


class CocNotFound(CocError):
    pass


def coc_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))


async def coc_get(session: aiohttp.ClientSession, path: str) -> dict:
    token = os.environ.get("COC_API_TOKEN")
    if not token:
        raise CocError("The Clash of Clans API isn't set up yet: add COC_API_TOKEN to env.env and restart the bot.")
    url = os.environ.get("COC_API_URL", "https://api.clashofclans.com/v1").rstrip("/") + path
    try:
        async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
            if resp.status == 200:
                return await resp.json()
            status = resp.status
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise CocError("Couldn't reach the Clash of Clans API, try again in a minute.")

    if status == 404:
        raise CocNotFound("That tag doesn't exist in Clash of Clans.")
    if status == 403:
        raise CocError(
            "The Clash of Clans API rejected the key. Make sure the key on developer.clashofclans.com "
            "lists the IP address the bot runs from."
        )
    if status == 429:
        raise CocError("Too many requests to the Clash of Clans API, try again in a minute.")
    if status == 503:
        raise CocError("Clash of Clans is in maintenance, try again later.")
    raise CocError(f"The Clash of Clans API returned an error ({status}).")


async def fetch_player(session: aiohttp.ClientSession, tag: str) -> dict:
    payload = await coc_get(session, f"/players/{quote('#' + tag)}")
    return {
        "name": payload["name"],
        "th": payload.get("townHallLevel"),
        # Only home village heroes count, not the Builder Base ones
        "heroes": sum(hero["level"] for hero in payload.get("heroes", []) if hero.get("village") == "home"),
    }


async def fetch_players(session: aiohttp.ClientSession, tags) -> dict[str, dict]:
    """Looks up many players at once. Ones that fail are left out."""
    semaphore = asyncio.Semaphore(10)

    async def one(tag):
        async with semaphore:
            try:
                return tag, await fetch_player(session, tag)
            except CocError:
                return tag, None

    results = await asyncio.gather(*(one(tag) for tag in tags))
    return {tag: info for tag, info in results if info}


# ---------- Dropdowns ----------


def player_choices(g: dict, tags, current: str, suffix: dict[str, str] | None = None) -> list[app_commands.Choice[str]]:
    """Autocomplete suggestions, strongest first. Type a name, a tag, or e.g. th17 to filter."""
    search = current.strip().lower().lstrip("#")
    town_hall = re.fullmatch(r"th\s*(\d+)", search)
    choices = []
    for tag in sorted(tags, key=lambda t: strength(g, t)):
        info = info_of(g, tag)
        if town_hall:
            if info.get("th") != int(town_hall.group(1)):
                continue
        elif search not in tag.lower() and search not in (info.get("name") or "").lower():
            continue
        label = option_label(tag, info) + (f" · #{tag}" if info.get("name") else "")
        if suffix and tag in suffix:
            label += f" · {suffix[tag]}"
        choices.append(app_commands.Choice(name=label[:100], value=tag))
        if len(choices) == 25:
            break
    return choices


async def pool_autocomplete(interaction: discord.Interaction, current: str):
    g = guild_data(load_data(), interaction.guild)
    return player_choices(g, unassigned(g), current)


async def rostered_autocomplete(interaction: discord.Interaction, current: str):
    g = guild_data(load_data(), interaction.guild)
    where = {tag: f"on {CLANS[clan]}" for clan, tags in g["rosters"].items() for tag in tags}
    return player_choices(g, where, current, where)


async def any_player_autocomplete(interaction: discord.Interaction, current: str):
    g = guild_data(load_data(), interaction.guild)
    return player_choices(g, g["players"], current)


class PlayerPicker(discord.ui.View):
    """A dropdown of players for one roster; subclasses decide what happens to the ones picked."""

    def __init__(self, g: dict, clan: str, tags: list[str], max_values: int, placeholder: str):
        super().__init__(timeout=300)
        self.clan = clan
        self.select = discord.ui.Select(
            placeholder=placeholder,
            max_values=max_values,
            options=[
                discord.SelectOption(label=option_label(tag, info_of(g, tag))[:100], value=tag, description=f"#{tag}")
                for tag in tags
            ],
        )
        self.select.callback = self.picked
        self.add_item(self.select)

    async def picked(self, interaction: discord.Interaction):
        # Reload in case the rosters changed while the dropdown was open
        data = load_data()
        g = guild_data(data, interaction.guild)
        message = self.apply(g, self.select.values)
        save_data(data)
        await interaction.response.edit_message(content=message, embed=roster_embed(g, self.clan), view=None)
        await refresh_board(interaction.guild)

    def apply(self, g: dict, tags: list[str]) -> str:
        raise NotImplementedError


class AddPicker(PlayerPicker):
    def apply(self, g, tags):
        added = 0
        for tag in sorted(tags, key=lambda t: strength(g, t)):
            if add_error(g, self.clan, tag) is None:
                place(g, tag, self.clan)
                added += 1
        message = f"Added {plural(added, 'player')} to **{CLANS[self.clan]}**."
        if added < len(tags):
            message += f" Skipped {len(tags) - added} who got put on a roster meanwhile or didn't fit."
        return message


class RemovePicker(PlayerPicker):
    def apply(self, g, tags):
        roster = g["rosters"][self.clan]
        removed = [tag for tag in roster if tag in tags]
        g["rosters"][self.clan] = [tag for tag in roster if tag not in tags]
        names = ", ".join(f"**{option_label(tag, info_of(g, tag))}**" for tag in removed)
        if not names:
            return "Nobody removed, they were already off the roster."
        return f"Removed {names} from **{CLANS[self.clan]}**. They're back in the pool."


# ---------- Commands ----------

group = app_commands.Group(
    name="roster",
    description="Manage the CWL rosters",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@group.command(name="setup", description="Link one of the clans to its in-game clan tag")
@app_commands.describe(clan="Which clan", clan_tag="The clan's in-game tag, e.g. #2PL29L2GP")
@app_commands.choices(clan=CLAN_CHOICES)
async def setup(interaction: discord.Interaction, clan: app_commands.Choice[str], clan_tag: str):
    tag = normalize_tag(clan_tag)
    if tag is None:
        return await interaction.response.send_message(f"`{clan_tag}` isn't a valid clan tag.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        async with coc_session() as session:
            payload = await coc_get(session, f"/clans/{quote('#' + tag)}")
    except CocError as e:
        return await interaction.followup.send(str(e), ephemeral=True)

    data = load_data()
    guild_data(data, interaction.guild)["clan_tags"][clan.value] = tag
    save_data(data)
    await interaction.followup.send(
        f"Linked **{clan.name}** to **{discord.utils.escape_markdown(payload['name'])}** "
        f"(#{tag}, {payload.get('members', 0)} members). Run `/roster sync` to load the members into the pool.",
        ephemeral=True,
    )


@group.command(name="sync", description="Load every clan member into the player pool, with fresh Town Hall and hero levels")
async def sync(interaction: discord.Interaction):
    g = guild_data(load_data(), interaction.guild)
    clan_tags = dict(g["clan_tags"])
    if not clan_tags:
        return await interaction.response.send_message("Link the clans first with `/roster setup`.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        async with coc_session() as session:
            members = {}  # player tag -> which of our clans they're in
            for clan, clan_tag in clan_tags.items():
                payload = await coc_get(session, f"/clans/{quote('#' + clan_tag)}/members")
                for member in payload["items"]:
                    members[member["tag"].lstrip("#")] = clan
            # Players on a roster who aren't in the clans get refreshed too
            tags = set(members) | rostered(g)
            fresh = await fetch_players(session, tags)
    except CocError as e:
        return await interaction.followup.send(str(e), ephemeral=True)

    # Reload, the rosters may have changed while the API was busy
    data = load_data()
    g = guild_data(data, interaction.guild)
    players = {}
    for tag in set(members) | rostered(g):
        info = fresh.get(tag) or g["players"].get(tag) or {"name": "", "th": None, "heroes": None}
        players[tag] = {**info, "clan": members.get(tag)}
    g["players"] = players
    save_data(data)
    await refresh_board(interaction.guild)

    counts = Counter(members.values())
    lines = [f"**{CLANS[clan]}**: {counts[clan]} members" for clan in clan_tags]
    lines.append(f"**Not on a roster yet**: {len(unassigned(g))}")
    missed = len(tags - set(fresh))
    if missed:
        lines.append(f"⚠️ Couldn't load {plural(missed, 'player')}, kept their old info")
    embed = discord.Embed(title="Player pool synced", description="\n".join(lines), color=GREEN)
    embed.set_footer(text="See who's free with /roster pool, add them with /roster add")
    await interaction.followup.send(embed=embed, ephemeral=True)


@group.command(name="pool", description="List players who aren't on a roster yet, strongest first")
async def pool(interaction: discord.Interaction):
    g = guild_data(load_data(), interaction.guild)
    tags = sorted(unassigned(g), key=lambda t: strength(g, t))
    if not tags:
        return await interaction.response.send_message(
            "Nobody in the pool. Run `/roster sync` to load the clans' members.", ephemeral=True
        )
    lines = []
    for i, tag in enumerate(tags):
        clan = info_of(g, tag).get("clan")
        line = player_line(tag, info_of(g, tag)) + (f" ({CLANS[clan]})" if clan else "")
        if sum(len(l) + 1 for l in lines) + len(line) > 3900:
            lines.append(f"*…and {len(tags) - i} more*")
            break
        lines.append(line)
    embed = discord.Embed(title=f"Player pool: {len(tags)} not on a roster", description="\n".join(lines), color=GREEN)
    embed.set_footer(text="Add them with /roster add")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@group.command(name="lookup", description="Show a player's name, Town Hall and total hero levels")
@app_commands.describe(tag="Player tag, e.g. #8LGGY9QRR")
@app_commands.autocomplete(tag=any_player_autocomplete)
async def lookup(interaction: discord.Interaction, tag: str):
    normalized = normalize_tag(tag)
    if normalized is None:
        return await interaction.response.send_message(f"`{tag}` isn't a valid player tag.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        async with coc_session() as session:
            info = await fetch_player(session, normalized)
    except CocError as e:
        return await interaction.followup.send(str(e), ephemeral=True)

    # Keep the pool's copy fresh while we're at it
    data = load_data()
    g = guild_data(data, interaction.guild)
    if normalized in g["players"]:
        g["players"][normalized].update(info)
        save_data(data)
    clan = roster_of(g, normalized)
    await interaction.followup.send(
        player_line(normalized, info) + (f"\nOn the **{CLANS[clan]}** roster" if clan else ""), ephemeral=True
    )
    if clan:
        await refresh_board(interaction.guild)


@group.command(name="add", description="Add players from the pool to a clan's roster")
@app_commands.describe(
    clan="Which roster",
    player="Search by name, tag, or e.g. th17. Leave empty to pick several from a list",
    position="Spot on the roster (default: the next empty one)",
)
@app_commands.choices(clan=CLAN_CHOICES)
@app_commands.autocomplete(player=pool_autocomplete)
async def add(
    interaction: discord.Interaction,
    clan: app_commands.Choice[str],
    player: str | None = None,
    position: app_commands.Range[int, 1, ROSTER_SIZE] | None = None,
):
    g = guild_data(load_data(), interaction.guild)

    if player is None:
        free = ROSTER_SIZE - len(g["rosters"][clan.value])
        if free <= 0:
            return await interaction.response.send_message(
                f"The **{clan.name}** roster is full ({ROSTER_SIZE} players). Remove someone first.", ephemeral=True
            )
        pool_tags = sorted(unassigned(g), key=lambda t: strength(g, t))
        if not pool_tags:
            return await interaction.response.send_message(
                "Nobody left in the pool. Run `/roster sync` to load the clans' members, "
                "or type a tag in the `player` option.",
                ephemeral=True,
            )
        more = " (showing the top 25, type in the `player` option to search everyone)" if len(pool_tags) > 25 else ""
        return await interaction.response.send_message(
            f"Pick players to add to **{clan.name}**, strongest first{more}:",
            embed=roster_embed(g, clan.value),
            view=AddPicker(g, clan.value, pool_tags[:25], min(free, len(pool_tags[:25])), "Choose players to add"),
            ephemeral=True,
        )

    tag = normalize_tag(player)
    if tag is None:
        return await interaction.response.send_message(f"`{player}` isn't a valid player tag.", ephemeral=True)
    error = add_error(g, clan.value, tag)
    if error:
        return await interaction.response.send_message(error, ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)

    # Someone who isn't in the pool gets looked up, which also checks the tag exists
    info, warning = None, ""
    if not info_of(g, tag).get("name"):
        try:
            async with coc_session() as session:
                info = await fetch_player(session, tag)
        except CocNotFound as e:
            return await interaction.followup.send(f"#{tag}: {e}", ephemeral=True)
        except CocError as e:
            warning = f"\n⚠️ Couldn't look up their name: {e}"

    # Reload and check again, the rosters may have changed during the lookup
    data = load_data()
    g = guild_data(data, interaction.guild)
    error = add_error(g, clan.value, tag)
    if error:
        return await interaction.followup.send(error, ephemeral=True)
    saved = g["players"].setdefault(tag, {"name": "", "th": None, "heroes": None, "clan": None})
    if info:
        saved.update(info)
    index = place(g, tag, clan.value, position)
    save_data(data)

    await interaction.followup.send(
        f"Added {player_line(tag, saved)} to **{clan.name}** at #{index + 1}.{warning}",
        embed=roster_embed(g, clan.value),
        ephemeral=True,
    )
    await refresh_board(interaction.guild)


@group.command(name="move", description="Move a player to another roster, or to another spot on the same one")
@app_commands.describe(
    player="Search by name, tag, or e.g. th17",
    destination="Roster to move them to",
    position="Spot on the roster (default: the end)",
)
@app_commands.choices(destination=CLAN_CHOICES)
@app_commands.autocomplete(player=rostered_autocomplete)
async def move(
    interaction: discord.Interaction,
    player: str,
    destination: app_commands.Choice[str],
    position: app_commands.Range[int, 1, ROSTER_SIZE] | None = None,
):
    data = load_data()
    g = guild_data(data, interaction.guild)
    tag = normalize_tag(player)
    current = roster_of(g, tag) if tag else None
    if current is None:
        return await interaction.response.send_message(
            f"`{player}` isn't on a roster. Use `/roster add` to add players from the pool.", ephemeral=True
        )
    if current == destination.value and position is None:
        return await interaction.response.send_message(
            f"They're already on **{destination.name}**. Pick a `position` to move them up or down.", ephemeral=True
        )
    if current != destination.value and len(g["rosters"][destination.value]) >= ROSTER_SIZE:
        return await interaction.response.send_message(
            f"The **{destination.name}** roster is full ({ROSTER_SIZE} players). Remove someone first.", ephemeral=True
        )

    index = place(g, tag, destination.value, position)
    save_data(data)
    embeds = [roster_embed(g, current)] if current != destination.value else []
    embeds.append(roster_embed(g, destination.value))
    await interaction.response.send_message(
        f"Moved {player_line(tag, info_of(g, tag))} from **{CLANS[current]}** to **{destination.name}** at #{index + 1}.",
        embeds=embeds,
        ephemeral=True,
    )
    await refresh_board(interaction.guild)


@group.command(name="remove", description="Remove players from a clan's roster (they go back to the pool)")
@app_commands.describe(clan="Which roster")
@app_commands.choices(clan=CLAN_CHOICES)
async def remove(interaction: discord.Interaction, clan: app_commands.Choice[str]):
    g = guild_data(load_data(), interaction.guild)
    roster = g["rosters"][clan.value]
    if not roster:
        return await interaction.response.send_message(f"The **{clan.name}** roster is empty.", ephemeral=True)
    await interaction.response.send_message(
        f"Pick who to remove from **{clan.name}**:",
        embed=roster_embed(g, clan.value),
        view=RemovePicker(g, clan.value, roster, len(roster), "Choose players to remove"),
        ephemeral=True,
    )


def move_whole_roster(guild: discord.Guild, source: str, destination: str) -> dict:
    """Moves everyone from source onto destination. Returns the reply to show."""
    data = load_data()
    g = guild_data(data, guild)
    sent_back = len(g["rosters"][destination])
    g["rosters"][destination] = g["rosters"][source]
    g["rosters"][source] = []
    save_data(data)
    content = f"Moved the **{CLANS[source]}** roster to **{CLANS[destination]}**."
    if sent_back:
        content += f" The {plural(sent_back, 'player')} already on it went back to the pool."
    return {"content": content, "embed": roster_embed(g, destination)}


class ConfirmMoveAllView(discord.ui.View):
    def __init__(self, source: str, destination: str):
        super().__init__(timeout=60)
        self.source = source
        self.destination = destination

    @discord.ui.button(label="Replace", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        reply = move_whole_roster(interaction.guild, self.source, self.destination)
        await interaction.response.edit_message(**reply, view=None)
        await refresh_board(interaction.guild)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


@group.command(name="move-all", description="Move a whole roster onto another, e.g. Chaotic Charge to Chaotic Charge 2")
@app_commands.describe(
    source="Roster to move everyone from",
    destination="Roster to move them onto (anyone already on it goes back to the pool)",
)
@app_commands.choices(source=CLAN_CHOICES, destination=CLAN_CHOICES)
async def move_all(
    interaction: discord.Interaction, source: app_commands.Choice[str], destination: app_commands.Choice[str]
):
    if source.value == destination.value:
        return await interaction.response.send_message("Pick two different rosters.", ephemeral=True)
    g = guild_data(load_data(), interaction.guild)
    if not g["rosters"][source.value]:
        return await interaction.response.send_message(
            f"The **{source.name}** roster is empty, nothing to move.", ephemeral=True
        )

    if not g["rosters"][destination.value]:
        reply = move_whole_roster(interaction.guild, source.value, destination.value)
        await interaction.response.send_message(**reply, ephemeral=True)
        return await refresh_board(interaction.guild)

    await interaction.response.send_message(
        f"**{destination.name}** already has {plural(len(g['rosters'][destination.value]), 'player')}. "
        f"Replace them with the {plural(len(g['rosters'][source.value]), 'player')} from **{source.name}**? "
        "The replaced players go back to the pool.",
        embed=roster_embed(g, destination.value),
        view=ConfirmMoveAllView(source.value, destination.value),
        ephemeral=True,
    )


@group.command(name="post", description="Post the roster board, which updates itself whenever a roster changes")
@app_commands.describe(channel="Channel to post it in (defaults to this one)")
async def post(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    channel = channel or interaction.channel
    try:
        message = await channel.send(embeds=board_embeds(guild_data(load_data(), interaction.guild)))
    except discord.HTTPException as e:
        return await interaction.response.send_message(f"Couldn't post the roster: {e.text}", ephemeral=True)

    data = load_data()
    guild_data(data, interaction.guild)["board"] = {"channel_id": channel.id, "message_id": message.id}
    save_data(data)
    await interaction.response.send_message(
        f"Roster board posted in {channel.mention}. It updates itself whenever a roster changes.", ephemeral=True
    )
