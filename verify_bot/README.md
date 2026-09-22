# SII Verify — Discord Bot

A Discord bot that verifies whether a user is a real student of the **SII** speciality by looking up their matricule in the Excel student list.

## Setup

```bash
cd verify_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create your config:

```bash
cp config.example.json config.json
# edit config.json
```

Fill in `config.json`:

| Key | Description |
| --- | --- |
| `discord_token` | Your bot token (from the Discord Developer Portal). Alternatively set the `DISCORD_TOKEN` env var. |
| `sync_guild_id` | Server ID to register commands in instantly (recommended while testing). |
| `admin_user_ids` | Your Discord user ID(s) — allow `/refresh`. |
| `admin_role` | Optional role name allowed to use `/refresh`. |
| `verified_role` | Role granted to confirmed SII students (create it in your server, this name must match). |
| `unverified_role` | Role granted to people who are NOT on the list or not ADM. |
| `mod_channel_id` | Channel where failed verification attempts are reported. |
| `xlsx_file` | Path to the Excel list (from the bot's folder). |

## Run

```bash
source .venv/bin/activate
python bot.py
```

## Commands

| Command | Description |
| --- | --- |
| `/verify <matricule>` | Checks the matricule. If found in the list with speciality **SII** and status **ADM**, replies + assigns the verified role. Otherwise it replies with the reason and assigns the unverified role. |
| `/check <matricule>` | Private lookup of any matricule (useful for moderators). |
| `/refresh` | Reloads students from the xlsx file (admin only). |

## How verification works

A student passes when ALL of these are true:

1. The matricule exists in the list.
2. `Spécialité` == **SII** (note: one row in the file is `SSI` and will be rejected).
3. `Etat` == **ADM** (TRFU, AJR and RNTG rows are rejected).

Anything else → unverified role + a message sent to the mod channel.