"""CWL rosters for the clan family: /roster add, /roster remove, /roster copy and /roster post."""

import json
import os
import re

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

# Player tags only ever use these characters
TAG_CHARS = set("0289PYLQGRJCUV")


# ---------- Data (saved per server in roster_data.json) ----------
# {guild_id: {"rosters": {"charge": [{"tag": "8GGYUGQ0G", "name": "Inhuman"}, ...], ...},
#             "board": {"channel_id": ..., "message_id": ...}}}


def load_data() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def guild_rosters(data: dict, guild: discord.Guild) -> dict[str, list[dict]]:
    rosters = data.setdefault(str(guild.id), {}).setdefault("rosters", {})
    for key in CLANS:
        rosters.setdefault(key, [])
    return rosters


# ---------- Helpers ----------


def normalize_tag(text: str) -> str | None:
    """'#pu8029yo' -> 'PU8029Y0'. Returns None if it can't be a player tag."""
    # People often type the letter O for a zero
    tag = re.sub(r"[^A-Z0-9]", "", text.upper()).replace("O", "0")
    return tag if len(tag) >= 3 and set(tag) <= TAG_CHARS else None


def player_label(player: dict) -> str:
    return f"{player['name']} - #{player['tag']}" if player["name"] else f"#{player['tag']}"


def known_players(rosters: dict[str, list[dict]]) -> dict[str, str]:
    """Every tag on any roster -> the name it was saved with."""
    names = {}
    for roster in rosters.values():
        for player in roster:
            if player["name"]:
                names[player["tag"]] = player["name"]
    return names


def roster_embed(clan: str, roster: list[dict]) -> discord.Embed:
    """The numbered 1-20 list, like the roster spreadsheet."""
    lines = []
    for i in range(ROSTER_SIZE):
        label = player_label(roster[i]).replace("`", "'") if i < len(roster) else ""
        lines.append(f"{i + 1:>2}  {label}".rstrip())
    embed = discord.Embed(title=CLANS[clan], description="```\n" + "\n".join(lines) + "\n```", color=GREEN)
    embed.set_footer(text=f"{len(roster)}/{ROSTER_SIZE} players")
    return embed


def board_embeds(rosters: dict[str, list[dict]]) -> list[discord.Embed]:
    return [roster_embed(clan, rosters[clan]) for clan in CLANS]


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
        await channel.get_partial_message(board["message_id"]).edit(embeds=board_embeds(guild_rosters(data, guild)))
    except discord.HTTPException as e:
        print(f"Couldn't update the roster board: {e}")


# ---------- Commands ----------

group = app_commands.Group(
    name="roster",
    description="Manage the CWL rosters",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


async def tag_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Suggests players already on another roster, so their name comes along with the tag."""
    rosters = guild_rosters(load_data(), interaction.guild)
    on_roster = {player["tag"] for player in rosters.get(interaction.namespace.clan, [])}
    search = current.upper().lstrip("#")
    choices = []
    for tag, name in sorted(known_players(rosters).items(), key=lambda item: item[1].lower()):
        if tag not in on_roster and (search in tag or current.lower() in name.lower()):
            choices.append(app_commands.Choice(name=f"{name} - #{tag}"[:100], value=tag))
    return choices[:25]


@group.command(name="add", description="Add a player tag to a clan's roster")
@app_commands.describe(
    clan="Which roster",
    tag="Player tag, e.g. #8GGYUGQ0G",
    name="In-game name (remembered from the other rosters if left out)",
    position="Spot on the roster (default: the next empty one)",
)
@app_commands.choices(clan=CLAN_CHOICES)
@app_commands.autocomplete(tag=tag_autocomplete)
async def add(
    interaction: discord.Interaction,
    clan: app_commands.Choice[str],
    tag: str,
    name: str | None = None,
    position: app_commands.Range[int, 1, ROSTER_SIZE] | None = None,
):
    normalized = normalize_tag(tag)
    if normalized is None:
        return await interaction.response.send_message(f"`{tag}` isn't a valid player tag.", ephemeral=True)

    data = load_data()
    rosters = guild_rosters(data, interaction.guild)
    roster = rosters[clan.value]
    if any(player["tag"] == normalized for player in roster):
        return await interaction.response.send_message(
            f"#{normalized} is already on the **{clan.name}** roster.", ephemeral=True
        )
    if len(roster) >= ROSTER_SIZE:
        return await interaction.response.send_message(
            f"The **{clan.name}** roster is full ({ROSTER_SIZE} players). Remove someone first.", ephemeral=True
        )

    player = {"tag": normalized, "name": (name or "").strip() or known_players(rosters).get(normalized, "")}
    index = len(roster) if position is None else min(position - 1, len(roster))
    roster.insert(index, player)
    save_data(data)

    message = f"Added **{player_label(player)}** to **{clan.name}** at #{index + 1}."
    others = [CLANS[key] for key, other in rosters.items() if key != clan.value and any(p["tag"] == normalized for p in other)]
    if others:
        message += f"\n⚠️ They're also on the {' and '.join(others)} roster."
    await interaction.response.send_message(message, embed=roster_embed(clan.value, roster), ephemeral=True)
    await refresh_board(interaction.guild)


class RemoveView(discord.ui.View):
    """A dropdown of everyone on the roster; pick one or more to remove."""

    def __init__(self, clan: str, roster: list[dict]):
        super().__init__(timeout=300)
        self.clan = clan
        self.select = discord.ui.Select(
            placeholder="Choose players to remove",
            max_values=len(roster),
            options=[
                discord.SelectOption(
                    label=f"{i}. {player['name'] or '#' + player['tag']}"[:100],
                    value=player["tag"],
                    description=f"#{player['tag']}",
                )
                for i, player in enumerate(roster, 1)
            ],
        )
        self.select.callback = self.remove_selected
        self.add_item(self.select)

    async def remove_selected(self, interaction: discord.Interaction):
        # Reload in case the roster changed while the dropdown was open
        data = load_data()
        rosters = guild_rosters(data, interaction.guild)
        chosen = set(self.select.values)
        removed = [player for player in rosters[self.clan] if player["tag"] in chosen]
        rosters[self.clan] = [player for player in rosters[self.clan] if player["tag"] not in chosen]
        save_data(data)

        names = ", ".join(f"**{player_label(player)}**" for player in removed) or "Nobody (already removed)"
        await interaction.response.edit_message(
            content=f"Removed {names} from **{CLANS[self.clan]}**.",
            embed=roster_embed(self.clan, rosters[self.clan]),
            view=None,
        )
        await refresh_board(interaction.guild)


@group.command(name="remove", description="Remove players from a clan's roster")
@app_commands.describe(clan="Which roster")
@app_commands.choices(clan=CLAN_CHOICES)
async def remove(interaction: discord.Interaction, clan: app_commands.Choice[str]):
    roster = guild_rosters(load_data(), interaction.guild)[clan.value]
    if not roster:
        return await interaction.response.send_message(f"The **{clan.name}** roster is empty.", ephemeral=True)
    await interaction.response.send_message(
        f"Pick who to remove from **{clan.name}**:",
        embed=roster_embed(clan.value, roster),
        view=RemoveView(clan.value, roster),
        ephemeral=True,
    )


async def copy_roster(interaction: discord.Interaction, source: str, destination: str, respond):
    data = load_data()
    rosters = guild_rosters(data, interaction.guild)
    rosters[destination] = [dict(player) for player in rosters[source]]
    save_data(data)
    await respond(
        content=f"Copied **{CLANS[source]}** to **{CLANS[destination]}** ({len(rosters[destination])} players).",
        embed=roster_embed(destination, rosters[destination]),
        view=None,
    )
    await refresh_board(interaction.guild)


class ConfirmCopyView(discord.ui.View):
    def __init__(self, source: str, destination: str):
        super().__init__(timeout=60)
        self.source = source
        self.destination = destination

    @discord.ui.button(label="Replace", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await copy_roster(interaction, self.source, self.destination, interaction.response.edit_message)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


@group.command(name="copy", description="Copy one clan's roster onto another, replacing it")
@app_commands.describe(source="Roster to copy from", destination="Roster to copy onto (its players get replaced)")
@app_commands.choices(source=CLAN_CHOICES, destination=CLAN_CHOICES)
async def copy(
    interaction: discord.Interaction, source: app_commands.Choice[str], destination: app_commands.Choice[str]
):
    if source.value == destination.value:
        return await interaction.response.send_message("Pick two different rosters.", ephemeral=True)
    rosters = guild_rosters(load_data(), interaction.guild)
    if not rosters[source.value]:
        return await interaction.response.send_message(
            f"The **{source.name}** roster is empty, nothing to copy.", ephemeral=True
        )

    async def respond(view, **kwargs):
        await interaction.response.send_message(**kwargs, ephemeral=True)  # a new message has no buttons to clear

    if not rosters[destination.value]:
        return await copy_roster(interaction, source.value, destination.value, respond)

    await interaction.response.send_message(
        f"**{destination.name}** already has {len(rosters[destination.value])} players. "
        f"Replace them with the {len(rosters[source.value])} from **{source.name}**?",
        embed=roster_embed(destination.value, rosters[destination.value]),
        view=ConfirmCopyView(source.value, destination.value),
        ephemeral=True,
    )


@group.command(name="post", description="Post the roster board, which updates itself whenever a roster changes")
@app_commands.describe(channel="Channel to post it in (defaults to this one)")
async def post(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    channel = channel or interaction.channel
    try:
        message = await channel.send(embeds=board_embeds(guild_rosters(load_data(), interaction.guild)))
    except discord.HTTPException as e:
        return await interaction.response.send_message(f"Couldn't post the roster: {e.text}", ephemeral=True)

    data = load_data()
    data.setdefault(str(interaction.guild.id), {})["board"] = {"channel_id": channel.id, "message_id": message.id}
    save_data(data)
    await interaction.response.send_message(
        f"Roster board posted in {channel.mention}. It updates itself whenever a roster changes.", ephemeral=True
    )
