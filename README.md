# 🎓 SII Verify Bot

A Discord bot that checks people are **real M1 SII A students** using the official university list (the `.xlsx` file).

- Sends you your student info
- Gives you a **verified role** + your **TD group role**
- Blocks anyone who isn't on the list

---

## 📌 For students — how to use it

### 1) Verify yourself

Go to the **#verify** channel and run:

```
/verify 222231378114
```

*(replace `222231378114` with your own matricule — it's on your student card / PC number)*

| Result | What happens |
| --- | --- |
| ✅ You're on the list, SII + Admis | You get the **SII Verified** role + your TD group role (e.g. `G1`) |
| ❌ Not on the list / not SII / not Admis | You get the **VISITOR** role and a message explaining why |

> Only send your matricule (a number) in #verify. If you spam non-numbers, the bot will **time you out** for 5 minutes after 3 tries.

### 2) Useful commands

| Command | What it does |
| --- | --- |
| `/mes_infos` | Show your own student record |
| `/mes_groupes` | Show your TD / TP groups |
| `/mes_camarades` | List everyone in your TD group |
| `/annuaire <matricule>` | Look up a classmate's groups |
| `/rappel <date> <message>` | Set a reminder → gets DM'd to you (e.g. `/rappel 25/09/2026 14:30 Devoir de maths`) |
| `/mes_rappels` | List your pending reminders |
| `/annuler_rappel <id>` | Cancel a reminder |
| `/signaler <message>` | Report a problem to the moderators |
| `/info <topic>` | Class info: `planning`, `devoirs`, `reglement` |
| `/stats` | Bot stats |
| `/help` | Show all commands |

---

## 🛠️ For whoever runs the bot

```bash
cd verify_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

1. Copy `config.example.json` → `config.json`
2. Fill in your **bot token**, guild ID, role names and channel IDs
3. Create the roles **SII Verified**, **VISITOR**, and `G1`...`G3` in your server
4. ### ⚠️ Never share `config.json` — it contains your secret bot token

```bash
python bot.py
```

The bot sends a 🟢 *online* message and a log of every verification to your **mod channel**.

### Admin commands

`/creer_roles_groupes` · `/sync_groupes` · `/groupe <n>` · `/check` · `/search` · `/verify_log` · `/export` · `/backup` · `/reload_config` · `/timeout` · `/untimeout` · `/unverify` · `/refresh`

---

## 📁 Files

| File | Purpose |
| --- | --- |
| `M1 SII A (Liste Affichage).xlsx` | The official student list the bot checks against |
| `verify_bot/verified.json` | Which Discord user is linked to which matricule |
| `verify_bot/verify_log.csv` | Log of every verification attempt |
| `verify_bot/reminders.json` | Everyone's pending reminders |