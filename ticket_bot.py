import asyncio
import io
import json
import os
import re
from collections import Counter

import chat_exporter
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "env.env"))

# Use a separate bot application for tickets so it gets its own icon and commands.
# Falls back to DISCORD_TOKEN if TICKET_BOT_TOKEN isn't set.
TOKEN = os.environ.get("TICKET_BOT_TOKEN") or os.environ["DISCORD_TOKEN"]
# Optional: GUILD_ID=<your server id> in env.env makes commands show up instantly in that server.
GUILD_ID = os.environ.get("GUILD_ID")
GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "ticket_config.json")
# Transcripts and details behind each log entry's dropdown
LOG_DIR = os.path.join(os.path.dirname(__file__), "ticket_logs")

GREEN = discord.Color.from_rgb(59, 165, 93)
YELLOW = discord.Color.from_rgb(250, 168, 26)
RED = discord.Color.from_rgb(237, 66, 69)
GRAY = discord.Color.from_rgb(47, 49, 54)

# Ticket channels store their owner in the channel topic, e.g. "owner:1234 | Ticket for bob"
OWNER_RE = re.compile(r"owner:(\d+)")

# Questions in the application form. Discord allows at most 5, and labels can be up to 45 characters.
# (label, placeholder, required)
QUESTIONS = [
    ("Joining for CWL, Regular Wars, or Both?", "Enter your reason", True),
    ("Why are you leaving your current clan?", "Enter your reason", True),
    ("What attacks do you specialize in?", "Enter your attacks", True),
    ("What region are you? Timezone?", "UTC or Country", True),
    ("Player tag", "#ABC1234", False),
]


# ---------- Config (saved per server in ticket_config.json) ----------


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_config(config: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def guild_settings(guild: discord.Guild) -> dict:
    return load_config().get(str(guild.id), {})


# ---------- Helpers ----------


def ticket_owner_id(channel) -> int | None:
    match = OWNER_RE.search(getattr(channel, "topic", None) or "")
    return int(match.group(1)) if match else None


def is_staff(member: discord.Member, settings: dict) -> bool:
    return member.guild_permissions.manage_channels or any(
        role.id == settings.get("staff_role_id") for role in member.roles
    )


def with_footer(embed: discord.Embed) -> discord.Embed:
    embed.set_footer(text=f"{client.user.name} - Ticketing made simple", icon_url=client.user.display_avatar.url)
    return embed


async def fetch_owner(channel: discord.TextChannel) -> discord.Member | None:
    owner_id = ticket_owner_id(channel)
    if owner_id is None:
        return None
    try:
        return await channel.guild.fetch_member(owner_id)
    except discord.NotFound:
        return None  # owner left the server


class Transcript:
    """A downloadable HTML copy of a ticket, plus what's needed to preview it inside Discord."""

    def __init__(self, filename: str, data: bytes, messages: list[discord.Message]):
        self.filename = filename
        self.data = data
        self.messages = messages

    def file(self) -> discord.File:
        # A discord.File can only be sent once, so make a fresh one each time
        return discord.File(io.BytesIO(self.data), filename=self.filename)


async def build_transcript(channel: discord.TextChannel) -> Transcript:
    """Builds a Discord-style HTML page of the whole channel."""
    messages = [msg async for msg in channel.history(limit=None, oldest_first=True)]
    html = await chat_exporter.raw_export(channel, messages, bot=client, military_time=True) or ""
    return Transcript(f"transcript-{channel.name}.html", html.encode("utf-8"), messages)


def transcript_embeds(
    channel: discord.TextChannel, action: str, by: discord.abc.User, transcript: Transcript, color
) -> dict[str, discord.Embed]:
    """Embeds that preview the ticket in Discord, so it can be read on mobile without opening the file.
    Returned as {"info": ..., "answers": ... (if the ticket had a form), "conversation": ...}."""
    people = [msg for msg in transcript.messages if not msg.author.bot]
    counts = Counter(msg.author.id for msg in people)
    owner_id = ticket_owner_id(channel)

    info = discord.Embed(title=f"Ticket {action}", color=color)
    info.add_field(name="Ticket Owner", value=f"<@{owner_id}>" if owner_id else "Unknown")
    info.add_field(name="Ticket Name", value=channel.name)
    info.add_field(name=f"{action} By", value=by.mention)
    participants = "\n".join(f"<@{user_id}> - {n} message{'s' * (n != 1)}" for user_id, n in counts.most_common())
    info.add_field(name="Participants", value=participants[:1024] or "Nobody talked", inline=False)
    info.add_field(
        name="Full Transcript",
        value="📎 Open the `.html` transcript file in a browser to see the whole chat.",
        inline=False,
    )
    embeds = {"info": with_footer(info)}

    # The application answers, exactly as they looked in the ticket
    for msg in transcript.messages:
        answers = next((e for e in msg.embeds if e.fields), None) if msg.author == client.user else None
        if answers:
            embeds["answers"] = answers
            break

    # A short version of the chat
    lines = []
    for i, msg in enumerate(people):
        text = msg.content.replace("\n", " ") or ("📎 attachment" if msg.attachments else "")
        if len(text) > 200:
            text = text[:200] + "…"
        line = f"`{msg.created_at:%H:%M}` **{msg.author.display_name}**: {text}"
        if sum(len(l) + 1 for l in lines) + len(line) > 3500:
            lines.append(f"*…and {len(people) - i} more, see the attached file*")
            break
        lines.append(line)
    embeds["conversation"] = discord.Embed(
        title="💬 Conversation", description="\n".join(lines) or "*No messages*", color=color
    )
    return embeds


async def send_embeds(destination, embeds: list[discord.Embed], file: discord.File | None = None):
    """Sends embeds in as few messages as possible (Discord allows 10 embeds / 6000 characters per message)."""
    batches = [[]]
    for embed in embeds:
        if batches[-1] and (sum(map(len, batches[-1])) + len(embed) > 6000 or len(batches[-1]) == 10):
            batches.append([])
        batches[-1].append(embed)
    for i, batch in enumerate(batches):
        if i == 0 and file:
            await destination.send(embeds=batch, file=file)
        else:
            await destination.send(embeds=batch)


def get_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    log_channel = guild.get_channel(guild_settings(guild).get("log_channel_id") or 0)
    return log_channel if isinstance(log_channel, discord.TextChannel) else None


async def send_to_log(guild: discord.Guild, text: str):
    log_channel = get_log_channel(guild)
    if log_channel:
        await log_channel.send(text, allowed_mentions=discord.AllowedMentions.none())


async def log_transcript(
    channel: discord.TextChannel, action: str, by: discord.abc.User, transcript: Transcript, color, emoji: str
) -> dict[str, discord.Embed]:
    """Posts a one-line log entry with a dropdown that reveals the details. Returns the detail embeds."""
    embeds = transcript_embeds(channel, action, by, transcript, color)
    log_channel = get_log_channel(channel.guild)
    if log_channel is None:
        return embeds

    owner_id = ticket_owner_id(channel)
    owner = f"<@{owner_id}>" if owner_id else "unknown user"
    message = await log_channel.send(
        f"{emoji} **Ticket {action}** - {owner} - {action} by {by.mention}",
        view=LogView(),
        allowed_mentions=discord.AllowedMentions.none(),  # show the names without pinging anyone
    )

    # Save the details so the dropdown still works after the bot restarts
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"{message.id}.html"), "wb") as f:
        f.write(transcript.data)
    record = {"filename": transcript.filename, "embeds": {name: e.to_dict() for name, e in embeds.items()}}
    with open(os.path.join(LOG_DIR, f"{message.id}.json"), "w", encoding="utf-8") as f:
        json.dump(record, f)
    return embeds


class LogView(discord.ui.View):
    """The 'View ticket details' dropdown under each log entry."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        custom_id="ticketlog:view",
        placeholder="View ticket details",
        options=[
            discord.SelectOption(label="Ticket Info", value="info", emoji="📋", description="Owner, staff and participants"),
            discord.SelectOption(label="Application Answers", value="answers", emoji="📝", description="What they filled in the form"),
            discord.SelectOption(label="Conversation", value="conversation", emoji="💬", description="A short version of the chat"),
            discord.SelectOption(label="Transcript File", value="file", emoji="📎", description="Download the full chat as a web page"),
        ],
    )
    async def view_details(self, interaction: discord.Interaction, select: discord.ui.Select):
        message_id = interaction.message.id
        try:
            with open(os.path.join(LOG_DIR, f"{message_id}.json"), encoding="utf-8") as f:
                record = json.load(f)
        except FileNotFoundError:
            return await interaction.response.send_message("The details for this log aren't saved anymore.", ephemeral=True)

        choice = select.values[0]
        if choice == "file":
            path = os.path.join(LOG_DIR, f"{message_id}.html")
            await interaction.response.send_message(file=discord.File(path, filename=record["filename"]), ephemeral=True)
        elif choice in record["embeds"]:
            embed = discord.Embed.from_dict(record["embeds"][choice])
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message("This ticket didn't have an application form.", ephemeral=True)

        # Reset the dropdown so the same option can be picked again
        await interaction.message.edit(view=LogView())


# ---------- Buttons ----------


class PanelView(discord.ui.View):
    """The green 'Create Application' button on the ticket panel."""

    def __init__(self, label: str = "Create Ticket", emoji: str = "📩"):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label=label, emoji=emoji, style=discord.ButtonStyle.success, custom_id="ticket:create"
        )
        button.callback = self.create_ticket
        self.add_item(button)

    async def create_ticket(self, interaction: discord.Interaction):
        error = ticket_precheck(interaction)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        await interaction.response.send_modal(ApplicationModal())


def ticket_precheck(interaction: discord.Interaction) -> str | None:
    """Returns an error message if this user can't open a ticket right now."""
    settings = guild_settings(interaction.guild)
    if not settings:
        return "Tickets aren't set up yet. An admin needs to run `/ticket-setup`."
    category = interaction.guild.get_channel(settings["category_id"])
    if not isinstance(category, discord.CategoryChannel):
        return "The ticket category is missing. An admin needs to run `/ticket-setup` again."
    for channel in category.text_channels:
        if ticket_owner_id(channel) == interaction.user.id and not channel.name.startswith("closed-"):
            return f"You already have an open ticket: {channel.mention}"
    return None


def channel_name_for(user: discord.abc.User) -> str:
    name = re.sub(r"[^a-z0-9_-]+", "-", user.name.lower()).strip("-")
    return name or f"ticket-{user.id}"


class ApplicationModal(discord.ui.Modal, title="Please fill this out"):
    def __init__(self):
        super().__init__(timeout=600)
        self.answers: list[discord.ui.TextInput] = []
        for label, placeholder, required in QUESTIONS:
            text_input = discord.ui.TextInput(
                label=label, placeholder=placeholder, required=required, max_length=1000
            )
            self.answers.append(text_input)
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction):
        # Check again in case they opened the form twice
        error = ticket_precheck(interaction)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        user = interaction.user
        settings = guild_settings(guild)
        category = guild.get_channel(settings["category_id"])
        staff_role = guild.get_role(settings["staff_role_id"])
        can_talk = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: can_talk,
            guild.me: can_talk,
        }
        if staff_role:
            overwrites[staff_role] = can_talk

        channel = await category.create_text_channel(
            channel_name_for(user),
            overwrites=overwrites,
            topic=f"owner:{user.id} | Ticket for {user.name}",
        )

        welcome = discord.Embed(
            description="Support will be with you shortly.\nTo close this press the close button",
            color=GREEN,
        )
        # No color = the plain gray side bar, like Ticket Tool's answers box
        answers = discord.Embed()
        for text_input in self.answers:
            value = text_input.value.strip().replace("```", "'''") or "Not provided"
            answers.add_field(name=text_input.label, value=f"```{value}```", inline=False)

        await channel.send(
            content=(f"{staff_role.mention}\n" if staff_role else "") + f"{user.mention} Welcome to {guild.name}.",
            embeds=[with_footer(welcome), answers],
            view=TicketView(),
        )
        await interaction.followup.send(f"Ticket created: {channel.mention}", ephemeral=True)
        await send_to_log(guild, f"🎫 **Ticket Opened** - {user.mention} - {channel.mention}")


class TicketView(discord.ui.View):
    """The Close button inside an open ticket."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await ask_to_close(interaction)


class ConfirmCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Closing ticket...", view=None)
        await close_ticket(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


class ClosedView(discord.ui.View):
    """Staff controls shown after a ticket is closed: Transcript / Open / Delete."""

    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_staff(interaction.user, guild_settings(interaction.guild)):
            return True
        await interaction.response.send_message("Only support staff can use these buttons.", ephemeral=True)
        return False

    @discord.ui.button(label="Transcript", emoji="📑", style=discord.ButtonStyle.secondary, custom_id="ticket:transcript")
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        channel = interaction.channel
        transcript = await build_transcript(channel)
        await interaction.followup.send(
            embed=discord.Embed(description="📑 Transcript saved. Download the file and open it in a browser.", color=GREEN),
            file=transcript.file(),
        )
        await log_transcript(channel, "Transcript Saved", interaction.user, transcript, GREEN, "📑")

    @discord.ui.button(label="Open", emoji="🔓", style=discord.ButtonStyle.secondary, custom_id="ticket:reopen")
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        channel = interaction.channel
        owner = await fetch_owner(channel)
        if owner:
            await channel.set_permissions(
                owner, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True
            )
        await interaction.message.delete()
        await channel.send(embed=discord.Embed(description=f"Ticket Opened by {interaction.user.mention}", color=GREEN))
        owner_id = ticket_owner_id(channel)
        await send_to_log(
            interaction.guild,
            f"🔓 **Ticket Reopened** - <@{owner_id}> - Reopened by {interaction.user.mention}",
        )

        # Move it back to the open tickets category, keeping the ticket's own permissions
        open_category = interaction.guild.get_channel(guild_settings(interaction.guild).get("category_id") or 0)
        if isinstance(open_category, discord.CategoryChannel):
            await channel.edit(category=open_category, sync_permissions=False)
        # Discord only allows 2 renames per channel every 10 minutes, so this can be delayed
        await channel.edit(name=channel.name.removeprefix("closed-"))

    @discord.ui.button(label="Delete", emoji="⛔", style=discord.ButtonStyle.secondary, custom_id="ticket:delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        await interaction.response.send_message(
            embed=discord.Embed(description="Ticket will be deleted in a few seconds", color=RED)
        )
        transcript = await build_transcript(channel)
        await log_transcript(channel, "Deleted", interaction.user, transcript, RED, "⛔")
        await asyncio.sleep(5)
        await channel.delete(reason=f"Ticket deleted by {interaction.user}")


async def ask_to_close(interaction: discord.Interaction):
    if ticket_owner_id(interaction.channel) is None:
        return await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
    if interaction.channel.name.startswith("closed-"):
        return await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
    await interaction.response.send_message(
        "Are you sure you would like to close this ticket?", view=ConfirmCloseView(), ephemeral=True
    )


async def close_ticket(interaction: discord.Interaction):
    channel = interaction.channel
    guild = interaction.guild
    settings = guild_settings(guild)
    owner = await fetch_owner(channel)
    if owner and not is_staff(owner, settings):
        await channel.set_permissions(owner, view_channel=False)

    await channel.send(embed=discord.Embed(description=f"Ticket Closed by {interaction.user.mention}", color=YELLOW))

    # Every close is logged with a transcript automatically
    transcript = await build_transcript(channel)
    embeds = await log_transcript(channel, "Closed", interaction.user, transcript, YELLOW, "🔒")

    # DM the ticket owner their transcript, since they can't see the channel anymore
    if owner:
        try:
            embeds["info"].description = f"Your ticket in **{guild.name}** was closed. Here's a copy of it."
            await send_embeds(owner, list(embeds.values()), transcript.file())
        except discord.HTTPException:
            pass  # they have DMs turned off

    await channel.send(
        embed=discord.Embed(description="```Support team ticket controls```", color=GRAY), view=ClosedView()
    )

    # Move to the Closed category, keeping the ticket's own permissions
    closed_category = guild.get_channel(settings.get("closed_category_id") or 0)
    if isinstance(closed_category, discord.CategoryChannel):
        await channel.edit(category=closed_category, sync_permissions=False)
    # Discord only allows 2 renames per channel every 10 minutes, so this can be delayed
    await channel.edit(name=f"closed-{channel.name}"[:100])


# ---------- Bot ----------


class TicketBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed so transcripts include message text
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Keep buttons working after the bot restarts
        self.add_view(PanelView())
        self.add_view(TicketView())
        self.add_view(ClosedView())
        self.add_view(LogView())

        if GUILD:
            self.tree.copy_global_to(guild=GUILD)
            synced = await self.tree.sync(guild=GUILD)
        else:
            synced = await self.tree.sync()
        print(f"Synced {len(synced)} command(s)")


client = TicketBot()


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


# ---------- Commands ----------


@client.tree.command(name="ticket-setup", description="Set the ticket categories, staff role and log channel")
@app_commands.describe(
    category="Category where new tickets get created (e.g. Applications)",
    staff_role="Role that can see and manage all tickets",
    closed_category="Category closed tickets move to (default: a 'Closed' category is made for you)",
    log_channel="Channel for ticket logs and transcripts (default: a private #ticket-logs is made for you)",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def ticket_setup(
    interaction: discord.Interaction,
    category: discord.CategoryChannel,
    staff_role: discord.Role,
    closed_category: discord.CategoryChannel | None = None,
    log_channel: discord.TextChannel | None = None,
):
    guild = interaction.guild
    if staff_role.is_default():
        return await interaction.response.send_message(
            "Pick a real staff role, not @everyone, otherwise everyone could see every ticket.", ephemeral=True
        )
    await interaction.response.defer(ephemeral=True, thinking=True)

    config = load_config()
    settings = config.setdefault(str(guild.id), {})

    # Only staff and the bot can see the Closed category and the log channel
    staff_only = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        staff_role: discord.PermissionOverwrite(view_channel=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True
        ),
    }

    if closed_category is None:
        closed_category = guild.get_channel(settings.get("closed_category_id") or 0)
    if not isinstance(closed_category, discord.CategoryChannel):
        closed_category = discord.utils.find(lambda c: c.name.lower() == "closed", guild.categories)
    if closed_category is None:
        closed_category = await guild.create_category("Closed", overwrites=staff_only)

    if log_channel is None:
        log_channel = guild.get_channel(settings.get("log_channel_id") or 0)
    if not isinstance(log_channel, discord.TextChannel):
        log_channel = await guild.create_text_channel("ticket-logs", category=closed_category, overwrites=staff_only)

    settings.update(
        category_id=category.id,
        closed_category_id=closed_category.id,
        staff_role_id=staff_role.id,
        log_channel_id=log_channel.id,
    )
    save_config(config)

    embed = discord.Embed(title="✅ Tickets set up", color=GREEN)
    embed.add_field(name="Open tickets", value=category.name)
    embed.add_field(name="Closed tickets", value=closed_category.name)
    embed.add_field(name="Staff role", value=staff_role.mention)
    embed.add_field(name="Log channel", value=log_channel.mention)
    embed.set_footer(text="Now run /ticket-panel in the channel where people should open tickets")
    await interaction.followup.send(embed=embed, ephemeral=True)


@client.tree.command(name="ticket-panel", description="Post a ticket panel with a button to open tickets")
@app_commands.describe(
    title="Panel title",
    description="Panel text",
    button_label="Text on the button",
    button_emoji="Emoji on the button",
    channel="Channel to post the panel in (defaults to this one)",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def ticket_panel(
    interaction: discord.Interaction,
    title: str = "Create Ticket to Apply to the Clan",
    description: str = "To create a ticket use the Create ticket button",
    button_label: str = "Create Application",
    button_emoji: str = "📩",
    channel: discord.TextChannel | None = None,
):
    channel = channel or interaction.channel
    embed = with_footer(discord.Embed(title=title, description=description, color=GREEN))
    try:
        await channel.send(embed=embed, view=PanelView(label=button_label, emoji=button_emoji))
    except discord.HTTPException as e:
        return await interaction.response.send_message(f"Couldn't post the panel: {e.text}", ephemeral=True)
    await interaction.response.send_message(f"Panel posted in {channel.mention}", ephemeral=True)


@client.tree.command(name="close", description="Close this ticket")
@app_commands.guild_only()
async def close(interaction: discord.Interaction):
    await ask_to_close(interaction)


@client.tree.command(name="add", description="Add someone to this ticket")
@app_commands.describe(user="Who to add")
@app_commands.guild_only()
async def add(interaction: discord.Interaction, user: discord.Member):
    if ticket_owner_id(interaction.channel) is None:
        return await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
    if not is_staff(interaction.user, guild_settings(interaction.guild)):
        return await interaction.response.send_message("Only support staff can add people.", ephemeral=True)
    await interaction.channel.set_permissions(
        user, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True
    )
    await interaction.response.send_message(
        embed=discord.Embed(description=f"{user.mention} was added to the ticket", color=GREEN)
    )


@client.tree.command(name="remove", description="Remove someone from this ticket")
@app_commands.describe(user="Who to remove")
@app_commands.guild_only()
async def remove(interaction: discord.Interaction, user: discord.Member):
    if ticket_owner_id(interaction.channel) is None:
        return await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
    if not is_staff(interaction.user, guild_settings(interaction.guild)):
        return await interaction.response.send_message("Only support staff can remove people.", ephemeral=True)
    await interaction.channel.set_permissions(user, overwrite=None)
    await interaction.response.send_message(
        embed=discord.Embed(description=f"{user.mention} was removed from the ticket", color=RED)
    )


@client.tree.command(name="help", description="List all of the bot's commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title=f"{client.user.name} commands", color=GREEN)
    embed.set_thumbnail(url=client.user.display_avatar.url)
    for cmd in sorted(client.tree.get_commands(), key=lambda c: c.name):
        options = " ".join(
            f"<{p.name}>" if p.required else f"[{p.name}]" for p in getattr(cmd, "parameters", [])
        )
        embed.add_field(name=f"/{cmd.name} {options}".strip(), value=cmd.description, inline=False)
    embed.set_footer(text="<required>  [optional]")
    await interaction.response.send_message(embed=embed, ephemeral=True)


client.run(TOKEN)
