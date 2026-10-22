# Clash Discord Bot

A Discord bot for the Chaotic clan family. It does two things:

- **Tickets**: people apply to the clan through a button and a form, staff talk to them in a private channel, and every ticket is logged with a transcript.
- **CWL rosters**: build the rosters for **Chaotic Charge**, **Chaotic Climax** and **Chaotic Charge 2** from the clans' members, with Town Hall and hero levels pulled from the Clash of Clans API.

## Setup

### 1. Create the bot

1. Create an application at https://discord.com/developers/applications and add a bot to it.
2. Under **Bot**, turn on **Message Content Intent**. Transcripts need it to include message text.
3. Invite the bot with the `bot` and `applications.commands` scopes and these permissions:
   - Manage Channels, Manage Roles
   - View Channels, Send Messages, Embed Links, Attach Files, Read Message History

### 2. Install

Requires Python 3.10 or newer.

```
pip install -r requirements.txt
```

### 3. Configure `env.env`

Create `env.env` next to `ticket_bot.py`. It holds secrets, so git ignores it.

```
# Bot token (Developer Portal > Bot > Reset Token)
DISCORD_TOKEN=

# Client ID and secret (Developer Portal > OAuth2). Only needed for /role-owner
bot_ClientID=
bot_Client_Secret=

# Clash of Clans API key from https://developer.clashofclans.com. Only needed for /roster
# The key only works from the IP addresses you list when creating it
COC_API_TOKEN=
```

Optional settings:

| Setting | Default | What it does |
|---|---|---|
| `TICKET_BOT_TOKEN` | `DISCORD_TOKEN` | Run the tickets under a different bot application |
| `GUILD_ID` | none | Your server ID; makes new commands show up instantly instead of after a few minutes |
| `DELETE_CLOSED_AFTER_MINUTES` | `1` | How long a closed ticket stays before it's deleted |
| `CLEANUP_CHECK_MINUTES` | `1` | How often the bot looks for closed tickets to delete |
| `COC_API_URL` | official API | Set to `https://cocproxy.royaleapi.dev/v1` and make your key for `45.79.218.79` if the bot's IP changes often |

### 4. Run it

```
python ticket_bot.py
```

The bot is only online while this is running.

## Tickets

1. `/ticket-setup` sets where tickets go, the staff role, and the log channel. If you don't choose a Closed category or log channel, the bot creates them.
2. `/ticket-panel` posts the **Create Application** button.

When someone presses the button they fill in a short form, and the bot opens a private channel for them and the staff role.

When a ticket is closed:
- The bot DMs the applicant their transcript.
- The ticket moves to the Closed category, where staff get **Transcript**, **Open** and **Delete** buttons.
- The ticket is deleted automatically after `DELETE_CLOSED_AFTER_MINUTES`.

Every open, close and delete is logged in the log channel. A dropdown on each log entry shows the details and the full transcript.

| Command | Who | What it does |
|---|---|---|
| `/ticket-setup` | Admins | Set categories, staff role and log channel |
| `/ticket-panel` | Admins | Post the button people use to open a ticket |
| `/close` | Anyone in a ticket | Close the ticket |
| `/add` `/remove` | Staff | Add or remove someone from a ticket |
| `/help` | Everyone | List the commands you can use |

## CWL rosters

Each roster has 20 spots, and a player can only be on one roster at a time.

**First time:**
1. Link each clan to its in-game tag:
   ```
   /roster setup clan:Chaotic Charge clan_tag:#XXXXXXX
   ```
2. Run `/roster sync` to load every clan member into the player pool.
3. In `#cwl-roster`, run `/roster post` to post the roster board. It updates itself whenever a roster changes.

**Every CWL:**
1. Run `/roster sync` so Town Hall and hero levels are fresh. Your rosters are kept.
2. Run `/roster pool` to see who isn't on a roster yet, strongest first.
3. Fill the rosters with `/roster add`. Leave `player` empty to pick several from a dropdown, or type a name, a tag, or `th17` to search.
4. Adjust with `/roster move`, `/roster remove` and `/roster move-all`.

Players are shown as `#️⃣ 8LGGY9QRR, 🏷️ CC⚡️CallMeJAS, 🏠 18, 👑 480`, meaning tag, name, Town Hall, and the total of their home village hero levels.

| Command | What it does |
|---|---|
| `/roster setup` | Link a clan to its in-game clan tag |
| `/roster sync` | Load all clan members into the pool with fresh levels |
| `/roster pool` | List players who aren't on a roster yet |
| `/roster add` | Add players from the pool to a roster, optionally at a set position |
| `/roster move` | Move a player to another roster, or to another spot on the same one |
| `/roster remove` | Remove players from a roster (they go back to the pool) |
| `/roster move-all` | Move a whole roster onto another one |
| `/roster lookup` | Show one player's name, Town Hall and hero total |
| `/roster post` | Post the self-updating roster board |

## Who can use the admin commands

The `/roster` commands, `/ticket-setup` and `/ticket-panel` are hidden from everyone without the **Manage Server** permission. To give another role access, an admin runs:

```
/role-owner add role:@Leaders
/role-owner remove role:@Leaders
/role-owner list
```

Discord itself hides the commands, so other members never see them. `/role-owner` is only visible to administrators.

Discord only lets a user, not a bot, change who can see a command. So `/role-owner` uses `bot_ClientID` and `bot_Client_Secret` to act as the account that owns the bot. That account needs **Manage Server** and **Manage Roles** in the server.

## Files

| File | What it is |
|---|---|
| `ticket_bot.py` | The bot: tickets, `/role-owner`, `/help` |
| `roster.py` | The `/roster` commands and the Clash of Clans API |
| `ticket_config.json` | Saved settings per server (created by `/ticket-setup`) |
| `roster_data.json` | Saved rosters and player pool (created by `/roster`) |
| `ticket_logs/` | Transcripts behind the log channel's dropdowns |

The `.json` files and `ticket_logs/` are created by the bot, and git ignores them.
