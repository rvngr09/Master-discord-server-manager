import csv
import io
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from students import load_students

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
VERIFIED_PATH = BASE_DIR / "verified.json"
VERIFY_LOG_PATH = BASE_DIR / "verify_log.csv"
REMINDERS_PATH = BASE_DIR / "reminders.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sii-verify")

ETAT_LABELS = {
    "ADM": "Admis",
    "TRFU": "Transferé",
    "AJR": "Ajourné",
    "RNTG": "Non admissible",
    "RS": "Redoublant",
}

# ---- anti-spam / timeout settings ----
MAX_STRIKES = 3
STRIKE_WINDOW_SECONDS = 60
TIMEOUT_SECONDS = 5 * 60

# ---- reminders ----
MAX_REMINDERS_PER_USER = 20
REMINDER_MAX_DAYS_AHEAD = 365


# ---------------- helpers ----------------

def _get_channel_id(config: dict, key: str):
    value = config.get(key)
    if not value:
        return None
    try:
        cid = int(value)
        return cid if cid > 0 else None
    except (TypeError, ValueError):
        return None


def _is_admin(bot, user) -> bool:
    admin_ids = bot.config.get("admin_user_ids", [])
    if user.id in admin_ids:
        return True
    if isinstance(user, discord.Member):
        admin_role = bot.config.get("admin_role")
        if admin_role and discord.utils.get(user.roles, name=admin_role):
            return True
    return False


def _is_verified_member(bot, user) -> bool:
    if not isinstance(user, discord.Member):
        return False
    role_name = bot.config.get("verified_role")
    return bool(role_name and discord.utils.get(user.roles, name=role_name))


def load_verified() -> dict:
    if VERIFIED_PATH.exists():
        try:
            with open(VERIFIED_PATH) as f:
                return json.load(f)
        except Exception as exc:
            log.warning("Could not read verified.json: %s", exc)
    return {}


def save_verified(data: dict):
    try:
        with open(VERIFIED_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning("Could not write verified.json: %s", exc)


def load_config_file() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_reminders() -> list:
    if REMINDERS_PATH.exists():
        try:
            with open(REMINDERS_PATH) as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as exc:
            log.warning("Could not read reminders.json: %s", exc)
    return []


def save_reminders(data: list):
    try:
        with open(REMINDERS_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning("Could not write reminders.json: %s", exc)


def log_verification(user_id: int, username: str, display_name: str, matricule: str,
                     full_name: str = "", section: str = "", td: str = "", tp: str = "",
                     status: str = "OK"):
    file_exists = VERIFY_LOG_PATH.exists()
    try:
        with open(VERIFY_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp_utc", "discord_id", "discord_username",
                    "discord_display_name", "matricule", "student_full_name",
                    "section", "groupe_td", "groupe_tp", "status",
                ])
            writer.writerow([
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                user_id, username, display_name, matricule, full_name,
                section, td, tp, status,
            ])
    except Exception as exc:
        log.warning("Could not write verify_log.csv: %s", exc)


def _all_td_groups(students: dict) -> list:
    td = set()
    for s in students.values():
        if s.is_sii and s.groupe_td:
            td.add(s.groupe_td)
    return sorted(td, key=lambda x: (len(x), x))


def _parse_reminder_date(text: str):
    """
    Accept:
      - ISO 8601: 2026-09-25T14:30 or 2026-09-25 14:30
      - DD/MM/YYYY HH:MM
      - YYYY-MM-DD HH:MM
    Returns a timezone-aware UTC datetime, or None if invalid.
    """
    text = text.strip()
    formats = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%Y-%m-%d",
    ]
    # Assume server runs in UTC (adjust if you prefer another TZ)
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            if fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                dt = dt.replace(hour=9, minute=0)  # default 9 AM UTC
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


async def _apply_role(interaction: discord.Interaction, role_name: str) -> bool:
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if member is None:
        return False
    role = discord.utils.get(interaction.guild.roles, name=role_name)
    if role is None:
        log.warning("Role '%s' not found on guild '%s'", role_name, interaction.guild.name)
        return False
    if role in member.roles:
        return True
    try:
        await member.add_roles(role, reason="SII verification")
        return True
    except discord.Forbidden:
        log.warning("No permission to assign role '%s' to %s", role_name, member)
        return False


async def _set_td_group_role(bot, member: discord.Member, td_group: str):
    if not td_group:
        return None

    fmt = bot.config.get("group_role_format", "G{group}")
    target_name = fmt.format(group=td_group)
    target_role = discord.utils.get(member.guild.roles, name=target_name)

    if target_role is None:
        log.warning("Target group role '%s' does not exist on this server", target_name)
        return None

    group_prefix = fmt.split("{group}")[0] if "{group}" in fmt else "G"
    to_remove = [
        r for r in member.roles
        if r.name != target_name
        and r.name.startswith(group_prefix)
        and r.name[len(group_prefix):].isdigit()
    ]

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="SII group sync")
        if target_role not in member.roles:
            await member.add_roles(target_role, reason="SII group assignment")
        return target_role.name
    except discord.Forbidden:
        log.warning("No permission to modify roles for %s", member)
        return None
    except discord.HTTPException as exc:
        log.warning("HTTP error updating roles for %s: %s", member, exc)
        return None


async def _notify_mods(bot, interaction: discord.Interaction, matricule: str, student,
                       reason: str, success: bool = False):
    channel_id = _get_channel_id(bot.config, "mod_channel_id")
    if channel_id is None:
        return
    channel = interaction.guild.get_channel(channel_id)
    if channel is None:
        log.warning("Mod channel %s not found", channel_id)
        return
    who = f"{interaction.user} (`{interaction.user.id}`)"
    name = f"{student.full_name}" if student else "unknown"
    icon = "✅" if success else "🚨"
    label = "Successful verification" if success else "Failed verification"
    msg = f"{icon} **{label}** by {who}\nMatricule: `{matricule}` · Matched: {name}"
    if reason:
        msg += f" · Reason: **{reason}**"
    try:
        await channel.send(msg)
    except discord.HTTPException as exc:
        log.warning("Failed to send mod notification: %s", exc)


# ---------------- bot ----------------

class VerifyBot(commands.Bot):
    def __init__(self, config: dict):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.config = config
        self.students: dict = {}
        self.skipped_rows = 0
        self.verified: dict = load_verified()
        self.matricule_to_user: dict = {
            mat: uid for uid, mat in self.verified.items()
        }
        self.spam_state: dict = {}
        self.reminders: list = load_reminders()
        self.reload_students()
        self._register_commands()

    # ---------- lifecycle ----------

    async def setup_hook(self):
        self.reminder_loop.start()

        guild_id = self.config.get("sync_guild_id")
        if not guild_id:
            log.warning("sync_guild_id not set — commands will sync globally (slow).")
            try:
                synced = await self.tree.sync()
                log.info("Globally synced %d command(s): %s", len(synced), [c.name for c in synced])
            except discord.HTTPException as exc:
                log.error("Global command sync failed: %s", exc)
            return

        guild_obj = discord.Object(id=int(guild_id))
        self.tree.copy_global_to(guild=guild_obj)
        try:
            synced = await self.tree.sync(guild=guild_obj)
            if not synced:
                log.error(
                    "Synced 0 commands to guild %s. Bot likely lacks 'applications.commands' scope.",
                    guild_id,
                )
            else:
                log.info(
                    "Synced %d command(s) to guild %s: %s",
                    len(synced), guild_id, [c.name for c in synced],
                )
        except discord.Forbidden as exc:
            log.error("Forbidden when syncing to guild %s: %s", guild_id, exc)
        except discord.HTTPException as exc:
            log.error("HTTP error when syncing to guild %s: %s", guild_id, exc)

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        log.info("Connected to %d guild(s): %s",
                 len(self.guilds),
                 [(g.name, g.id) for g in self.guilds])
        await self._send_startup_message()

    # ---------- reminder background task ----------

    @tasks.loop(seconds=30)
    async def reminder_loop(self):
        """Check for due reminders and DM users."""
        if not self.reminders:
            return
        now = datetime.now(timezone.utc)
        due = []
        keep = []
        for r in self.reminders:
            try:
                when = datetime.fromisoformat(r["when"])
            except Exception:
                continue
            if when <= now:
                due.append(r)
            else:
                keep.append(r)

        if not due:
            return

        log.info("Firing %d reminder(s)", len(due))
        for r in due:
            try:
                user = self.get_user(int(r["user_id"])) or await self.fetch_user(int(r["user_id"]))
                if user is None:
                    log.warning("Reminder: user %s not found", r["user_id"])
                    continue
                embed = discord.Embed(
                    title="⏰ Reminder",
                    description=r["message"],
                    color=discord.Color.blurple(),
                    timestamp=now,
                )
                embed.set_footer(text=f"Scheduled for {r['when']} UTC")
                await user.send(embed=embed)
            except discord.Forbidden:
                log.warning("Reminder: cannot DM user %s", r.get("user_id"))
            except discord.HTTPException as exc:
                log.warning("Reminder send failed: %s", exc)

        self.reminders = keep
        save_reminders(self.reminders)

    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        await self.wait_until_ready()

    # ---------- message / interaction logging ----------

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is None:
            log.info("[DM] <%s>: %s", message.author, message.content)
            return

        verify_channel_id = _get_channel_id(self.config, "verify_channel_id")
        if verify_channel_id and message.channel.id == verify_channel_id:
            now = time.monotonic()
            state = self.spam_state.setdefault(
                message.author.id,
                {"strikes": [], "until": 0.0},
            )

            if state["until"] > now:
                log.info("Muted user %s posted in verify channel — deleting", message.author)
                try:
                    await message.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass
                return

            content = message.content.strip()
            if not content.isdigit():
                state["strikes"] = [
                    t for t in state["strikes"] if now - t < STRIKE_WINDOW_SECONDS
                ]
                state["strikes"].append(now)
                count = len(state["strikes"])
                log.info("Strike %d/%d for %s in verify channel", count, MAX_STRIKES, message.author)

                try:
                    await message.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass

                if count >= MAX_STRIKES:
                    state["until"] = now + TIMEOUT_SECONDS
                    state["strikes"] = []

                    if isinstance(message.author, discord.Member):
                        try:
                            until = datetime.now(timezone.utc) + timedelta(seconds=TIMEOUT_SECONDS)
                            await message.author.timeout(
                                until,
                                reason="Too many non-numeric messages in verify channel",
                            )
                            log.warning("Discord-timeout applied to %s for %ds",
                                        message.author, TIMEOUT_SECONDS)
                        except discord.Forbidden:
                            log.warning("Could not timeout %s — missing Moderate Members perm "
                                        "or role hierarchy issue", message.author)
                        except discord.HTTPException as exc:
                            log.warning("timeout() failed for %s: %s", message.author, exc)

                    try:
                        await message.channel.send(
                            f"{message.author.mention} you've been timed out for "
                            f"**{TIMEOUT_SECONDS // 60} minutes** — please send only your "
                            "matricule (a number)."
                        )
                    except discord.HTTPException:
                        pass
                elif count == MAX_STRIKES - 1:
                    try:
                        await message.channel.send(
                            f"{message.author.mention} please send only your matricule (a number). "
                            f"One more non-numeric message and you'll be timed out for "
                            f"{TIMEOUT_SECONDS // 60} minutes."
                        )
                    except discord.HTTPException:
                        pass
                return

        extra = f" · {len(message.attachments)} attachment(s)" if message.attachments else ""
        log.info("[%s] #%s <%s>: %s%s",
                 message.guild.name, message.channel.name,
                 message.author, message.content, extra)

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.bot or before.content == after.content:
            return
        log.info("[%s] #%s <%s> edited: %r -> %r",
                 after.guild.name, after.channel.name,
                 after.author, before.content, after.content)

    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.application_command:
            return
        opts = ""
        if interaction.data.get("options"):
            opts = " [" + ", ".join(
                f"{o['name']}={o.get('value')}" for o in interaction.data["options"]
            ) + "]"
        log.info("Command /%s by %s (%s) in %s%s",
                 interaction.command.name if interaction.command else "?",
                 interaction.user, interaction.user.id,
                 interaction.guild.name if interaction.guild else "DM",
                 opts)

    async def on_app_command_completion(self, interaction: discord.Interaction, command: app_commands.Command):
        log.info("Command /%s completed for %s", command.name, interaction.user)

    async def _send_startup_message(self):
        channel_ids = self.config.get("startup_channel_ids")
        if not channel_ids and self.config.get("mod_channel_id"):
            channel_ids = [self.config["mod_channel_id"]]
        if not channel_ids:
            return
        sent = 0
        for channel_id in channel_ids:
            try:
                cid = int(channel_id)
            except (TypeError, ValueError):
                continue
            channel = self.get_channel(cid)
            if channel is None:
                log.warning("Startup channel %s not found", channel_id)
                continue
            try:
                await channel.send(
                    f"🟢 **{self.user} is online.** "
                    f"Loaded **{len(self.students)}** students · "
                    f"Slash commands ready."
                )
                sent += 1
            except discord.HTTPException as exc:
                log.warning("Could not send startup message to channel %s: %s", channel_id, exc)
        log.info("Startup message sent to %d channel(s)", sent)

    # ---------- student list ----------

    def reload_students(self):
        xlsx = BASE_DIR / self.config.get("xlsx_file", "students.xlsx")
        self.students, self.skipped_rows = load_students(str(xlsx))
        log.info("Loaded %d students from %s (%d rows skipped)",
                 len(self.students), xlsx.name, self.skipped_rows)

    def _find_matricule_for_user(self, user_id: int):
        return self.verified.get(str(user_id))

    def _find_user_for_matricule(self, matricule: str):
        return self.matricule_to_user.get(matricule)

    # ---------- commands ----------

    def _register_commands(self):
        cfg = self.config
        verified_role = cfg.get("verified_role", "SII Verified")
        unverified_role = cfg.get("unverified_role", "SII Unverified")

        # --------- /verify ---------

        @self.tree.command(name="verify", description="Check your matricule and get verified as an SII student")
        @app_commands.describe(matricule="Your university matricule number")
        async def verify(interaction: discord.Interaction, matricule: str):
            if not isinstance(interaction.channel, discord.TextChannel):
                await interaction.response.send_message("Please run this in a server channel.", ephemeral=True)
                return

            verify_channel_id = _get_channel_id(self.config, "verify_channel_id")
            if verify_channel_id is None:
                await interaction.response.send_message(
                    "Verification channel is not configured. Contact an admin.", ephemeral=True
                )
                return
            if interaction.channel.id != verify_channel_id:
                await interaction.response.send_message(
                    f"❌ Please use `/verify` in <#{verify_channel_id}> only.",
                    ephemeral=True,
                )
                log.info(
                    "Blocked /verify from %s in #%s (correct channel: %s)",
                    interaction.user, interaction.channel.name, verify_channel_id,
                )
                return

            uid = interaction.user.id
            uname = str(interaction.user)
            dname = getattr(interaction.user, "display_name", str(interaction.user))

            mat = matricule.strip().replace(" ", "")
            if not mat.isdigit():
                await interaction.response.send_message(
                    "That doesn't look like a matricule. A matricule is a number (e.g. `222231378114`).",
                    ephemeral=True,
                )
                return

            existing_owner_id = self._find_user_for_matricule(mat)
            if existing_owner_id is not None and existing_owner_id != str(uid):
                owner_mention = f"<@{existing_owner_id}>"
                log.warning(
                    "Duplicate matricule %s: %s (%s) tried to claim it, already owned by %s",
                    mat, uname, uid, existing_owner_id,
                )
                log_verification(uid, uname, dname, mat, status="ALREADY_VERIFIED")
                await interaction.response.send_message(
                    f"❌ The matricule `{mat}` is **already linked** to another Discord account "
                    f"({owner_mention}).\n"
                    "If this is a mistake, contact a moderator so they can unlink it with `/unverify`.",
                    ephemeral=True,
                )
                await _notify_mods(
                    self, interaction, mat, None,
                    f"duplicate attempt (owner: <@{existing_owner_id}>)",
                )
                return

            student = self.students.get(mat)

            if student is None:
                await _apply_role(interaction, unverified_role)
                log_verification(uid, uname, dname, mat, status="NOT_IN_LIST")
                await interaction.response.send_message(
                    f"`{mat}` is **not** on the SII student list, so you can't be verified as an SII student.\n"
                    "If you believe this is a mistake, contact a moderator.",
                )
                await _notify_mods(self, interaction, mat, None, "not in list")
                return

            if not student.is_sii:
                await _apply_role(interaction, unverified_role)
                log_verification(
                    uid, uname, dname, mat,
                    full_name=student.full_name, section=student.section,
                    td=student.groupe_td, tp=student.groupe_tp,
                    status="WRONG_SPECIALITY",
                )
                await interaction.response.send_message(
                    f"`{mat}` matches **{student.full_name}** but their speciality is **{student.specialite}**, not SII.\n"
                    "You can't be verified as an SII student.",
                )
                await _notify_mods(self, interaction, mat, student, "wrong speciality")
                return

            if not student.is_admis:
                await _apply_role(interaction, unverified_role)
                log_verification(
                    uid, uname, dname, mat,
                    full_name=student.full_name, section=student.section,
                    td=student.groupe_td, tp=student.groupe_tp,
                    status="NOT_ADMIS",
                )
                etat_label = ETAT_LABELS.get(student.etat.upper(), student.etat)
                await interaction.response.send_message(
                    f"`{mat}` matches **{student.full_name}** but their status is **{etat_label}**.\n"
                    "Only **Admis (ADM)** students are verified. Please contact the administration.",
                )
                await _notify_mods(self, interaction, mat, student, f"etat={student.etat}")
                return

            role_ok = await _apply_role(interaction, verified_role)

            unverified = discord.utils.get(interaction.guild.roles, name=unverified_role)
            if unverified and isinstance(interaction.user, discord.Member) and unverified in interaction.user.roles:
                try:
                    await interaction.user.remove_roles(unverified, reason="SII verification passed")
                except discord.Forbidden:
                    log.warning("Could not remove unverified role from %s", interaction.user)

            group_role_assigned = None
            if isinstance(interaction.user, discord.Member) and student.groupe_td:
                group_role_assigned = await _set_td_group_role(self, interaction.user, student.groupe_td)

            self.verified[str(uid)] = mat
            self.matricule_to_user[mat] = str(uid)
            save_verified(self.verified)

            log_verification(
                uid, uname, dname, mat,
                full_name=student.full_name, section=student.section,
                td=student.groupe_td, tp=student.groupe_tp,
                status="OK",
            )

            td = student.groupe_td or "?"
            tp = student.groupe_tp or "?"
            role_note = "" if role_ok else "\n*(Could not assign the verified role automatically — ask a moderator.)*"
            group_note = ""
            if group_role_assigned:
                group_note = f"\nAssigned group role: **{group_role_assigned}**"
            elif student.groupe_td:
                fmt = cfg.get("group_role_format", "G{group}")
                group_note = (
                    f"\n*(Group role `{fmt.format(group=student.groupe_td)}` "
                    "not created yet — ask an admin to run `/creer_roles_groupes`.)*"
                )

            await interaction.response.send_message(
                "✅ **Verified!**\n"
                f"**{student.full_name}** — {student.palier} {student.specialite} (Section {student.section})\n"
                f"Groupe TD: **{td}** · Groupe TP: **{tp}**{role_note}{group_note}\n\n"
                "Use `/mes_infos`, `/mes_groupes`, or `/mes_camarades` to see more.",
            )
            await _notify_mods(self, interaction, mat, student, "", success=True)

        # --------- /mes_infos ---------

        @self.tree.command(name="mes_infos", description="Show your own student record")
        async def mes_infos(interaction: discord.Interaction):
            mat = self._find_matricule_for_user(interaction.user.id)
            if mat is None:
                await interaction.response.send_message(
                    "I don't know your matricule yet. Run `/verify <matricule>` first.",
                    ephemeral=True,
                )
                return
            student = self.students.get(mat)
            if student is None:
                await interaction.response.send_message(
                    "Your matricule is no longer in the database. Contact a moderator.",
                    ephemeral=True,
                )
                return

            etat_label = ETAT_LABELS.get(student.etat.upper(), student.etat)
            await interaction.response.send_message(
                f"🎓 **Your student record**\n"
                f"**Name:** {student.full_name}\n"
                f"**Matricule:** `{student.matricule}`\n"
                f"**Palier:** {student.palier}\n"
                f"**Spécialité:** {student.specialite}\n"
                f"**Section:** {student.section}\n"
                f"**Statut:** {student.etat} ({etat_label})\n"
                f"**Groupe TD:** `{student.groupe_td or '?'}` · **Groupe TP:** `{student.groupe_tp or '?'}`",
                ephemeral=True,
            )

        # --------- /mes_groupes ---------

        @self.tree.command(name="mes_groupes", description="Show your TD and TP groups")
        async def mes_groupes(interaction: discord.Interaction):
            mat = self._find_matricule_for_user(interaction.user.id)
            if mat is None:
                await interaction.response.send_message(
                    "I don't know your matricule yet. Run `/verify <matricule>` first.",
                    ephemeral=True,
                )
                return
            student = self.students.get(mat)
            if student is None:
                await interaction.response.send_message("Your record was not found.", ephemeral=True)
                return

            await interaction.response.send_message(
                f"📚 **Your groups**\n"
                f"TD: **{student.groupe_td or '?'}** *(assigned as a role)*\n"
                f"TP: **{student.groupe_tp or '?'}** *(informational)*\n"
                f"Section: **{student.section}** · Palier: **{student.palier}**",
                ephemeral=True,
            )

        # --------- /mes_camarades ---------

        @self.tree.command(name="mes_camarades", description="List classmates in your TD group")
        async def mes_camarades(interaction: discord.Interaction):
            mat = self._find_matricule_for_user(interaction.user.id)
            if mat is None:
                await interaction.response.send_message(
                    "I don't know your matricule yet. Run `/verify <matricule>` first.",
                    ephemeral=True,
                )
                return
            me = self.students.get(mat)
            if me is None:
                await interaction.response.send_message("Your record was not found.", ephemeral=True)
                return

            group_num = me.groupe_td
            if not group_num:
                await interaction.response.send_message("You don't have a TD group assigned.", ephemeral=True)
                return

            classmates = [
                s for s in self.students.values()
                if s.is_sii
                and s.section == me.section
                and s.palier == me.palier
                and s.groupe_td == group_num
                and s.matricule != mat
            ]
            classmates.sort(key=lambda s: s.nom.lower())

            if not classmates:
                await interaction.response.send_message(
                    f"You're the only one in **TD {group_num}** (Section {me.section}).",
                    ephemeral=True,
                )
                return

            lines = [f"`{s.matricule}` — {s.full_name}" for s in classmates[:40]]
            header = (
                f"👥 **TD {group_num}** — Section {me.section} · "
                f"{len(classmates)} classmate(s)"
            )
            if len(classmates) > 40:
                header += " (showing first 40)"
            await interaction.response.send_message(header + "\n" + "\n".join(lines), ephemeral=True)

        # --------- /groupe (admin-only) ---------

        @self.tree.command(name="groupe", description="List ALL students in a specific TD group (admin only)")
        @app_commands.describe(numero="TD group number (e.g. 1)")
        async def groupe(interaction: discord.Interaction, numero: str):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message(
                    "❌ This command is restricted to admins. Use `/mes_camarades` instead.",
                    ephemeral=True,
                )
                return

            num = numero.strip()
            matches = [
                s for s in self.students.values()
                if s.is_sii and s.groupe_td == num
            ]
            matches.sort(key=lambda s: (s.section, s.nom.lower()))

            if not matches:
                await interaction.response.send_message(
                    f"No SII students in **TD {num}**.", ephemeral=True
                )
                return

            lines = [f"`{s.matricule}` — {s.full_name} (Section {s.section})" for s in matches[:80]]
            header = f"👥 **TD {num}** — {len(matches)} student(s)"
            if len(matches) > 80:
                header += " (showing first 80)"
            await interaction.response.send_message(header + "\n" + "\n".join(lines), ephemeral=True)

        # --------- /annuaire ---------

        @self.tree.command(name="annuaire", description="Look up a classmate's groups by matricule")
        @app_commands.describe(matricule="Classmate's matricule")
        async def annuaire(interaction: discord.Interaction, matricule: str):
            if not _is_verified_member(self, interaction.user) and not _is_admin(self, interaction.user):
                await interaction.response.send_message(
                    "You must be verified to use this command. Run `/verify` first.",
                    ephemeral=True,
                )
                return
            mat = matricule.strip().replace(" ", "")
            if not mat.isdigit():
                await interaction.response.send_message("Please provide a numeric matricule.", ephemeral=True)
                return
            student = self.students.get(mat)
            if student is None:
                await interaction.response.send_message(f"`{mat}` not found.", ephemeral=True)
                return
            if not student.is_sii:
                await interaction.response.send_message(f"`{mat}` is not an SII student.", ephemeral=True)
                return

            await interaction.response.send_message(
                f"👤 **{student.full_name}**\n"
                f"Palier: {student.palier} · Section: **{student.section}**\n"
                f"TD: **{student.groupe_td or '?'}** · TP: **{student.groupe_tp or '?'}**",
                ephemeral=True,
            )

        # --------- /info ---------

        @self.tree.command(name="info", description="Show class info (planning, homework, etc.)")
        @app_commands.choices(topic=[
            app_commands.Choice(name="Planning", value="planning"),
            app_commands.Choice(name="Devoirs", value="devoirs"),
            app_commands.Choice(name="Règlement", value="reglement"),
        ])
        async def info(interaction: discord.Interaction, topic: app_commands.Choice[str]):
            msgs = self.config.get("info_messages", {})
            text = msgs.get(topic.value)
            if not text:
                await interaction.response.send_message(
                    f"No info available for **{topic.name}** yet.", ephemeral=True
                )
                return
            await interaction.response.send_message(text, ephemeral=True)

        # --------- /rappel ---------

        @self.tree.command(name="rappel", description="Set a personal reminder — the bot will DM you at that time")
        @app_commands.describe(
            date="When to remind you (e.g. `2026-09-25 14:30`, `25/09/2026 14:30`, or `2026-09-25`)",
            message="What to remind you about",
        )
        async def rappel(interaction: discord.Interaction, date: str, message: str):
            when = _parse_reminder_date(date)
            if when is None:
                await interaction.response.send_message(
                    "❌ Couldn't parse that date. Try formats like:\n"
                    "`2026-09-25 14:30` · `25/09/2026 14:30` · `2026-09-25` (defaults to 09:00 UTC)",
                    ephemeral=True,
                )
                return

            now = datetime.now(timezone.utc)
            if when <= now:
                await interaction.response.send_message(
                    "❌ That date is in the past. Pick a future time.", ephemeral=True
                )
                return
            if (when - now).days > REMINDER_MAX_DAYS_AHEAD:
                await interaction.response.send_message(
                    f"❌ Reminders can only be set up to {REMINDER_MAX_DAYS_AHEAD} days ahead.",
                    ephemeral=True,
                )
                return

            uid_str = str(interaction.user.id)
            user_count = sum(1 for r in self.reminders if r.get("user_id") == uid_str)
            if user_count >= MAX_REMINDERS_PER_USER:
                await interaction.response.send_message(
                    f"❌ You already have {MAX_REMINDERS_PER_USER} pending reminders. "
                    "Cancel some with `/annuler_rappel` first.",
                    ephemeral=True,
                )
                return

            msg = message.strip()
            if len(msg) > 500:
                msg = msg[:500] + "…"

            rid = uuid.uuid4().hex[:8]
            entry = {
                "id": rid,
                "user_id": uid_str,
                "username": str(interaction.user),
                "message": msg,
                "when": when.isoformat(timespec="seconds"),
                "created_at": now.isoformat(timespec="seconds"),
            }
            self.reminders.append(entry)
            save_reminders(self.reminders)

            unix = int(when.timestamp())
            await interaction.response.send_message(
                f"⏰ Reminder set for <t:{unix}:F> (<t:{unix}:R>).\n"
                f"**ID:** `{rid}` · **Message:** {msg}\n"
                "_The bot will DM you at that time. Make sure your DMs are open._",
                ephemeral=True,
            )
            log.info("Reminder %s set by %s for %s", rid, interaction.user, entry["when"])

        # --------- /mes_rappels ---------

        @self.tree.command(name="mes_rappels", description="List your pending reminders")
        async def mes_rappels(interaction: discord.Interaction):
            uid_str = str(interaction.user.id)
            mine = [r for r in self.reminders if r.get("user_id") == uid_str]
            if not mine:
                await interaction.response.send_message(
                    "You have no pending reminders.", ephemeral=True
                )
                return
            mine.sort(key=lambda r: r.get("when", ""))
            lines = []
            for r in mine[:20]:
                try:
                    when = datetime.fromisoformat(r["when"])
                    unix = int(when.timestamp())
                    when_str = f"<t:{unix}:F> (<t:{unix}:R>)"
                except Exception:
                    when_str = r.get("when", "?")
                msg = r.get("message", "")
                if len(msg) > 80:
                    msg = msg[:80] + "…"
                lines.append(f"`{r['id']}` — {when_str} — {msg}")
            header = f"⏰ **Your reminders** ({len(mine)})"
            if len(mine) > 20:
                header += " — showing first 20"
            await interaction.response.send_message(header + "\n" + "\n".join(lines), ephemeral=True)

        # --------- /annuler_rappel ---------

        @self.tree.command(name="annuler_rappel", description="Cancel one of your reminders by ID")
        @app_commands.describe(id="The reminder ID (see `/mes_rappels`)")
        async def annuler_rappel(interaction: discord.Interaction, id: str):
            uid_str = str(interaction.user.id)
            rid = id.strip()
            target = None
            for r in self.reminders:
                if r.get("id") == rid:
                    target = r
                    break
            if target is None:
                await interaction.response.send_message(
                    f"No reminder with ID `{rid}`.", ephemeral=True
                )
                return
            if target.get("user_id") != uid_str and not _is_admin(self, interaction.user):
                await interaction.response.send_message(
                    "❌ That reminder isn't yours.", ephemeral=True
                )
                return
            self.reminders = [r for r in self.reminders if r.get("id") != rid]
            save_reminders(self.reminders)
            await interaction.response.send_message(f"✅ Reminder `{rid}` cancelled.", ephemeral=True)

        # --------- /signaler ---------

        @self.tree.command(name="signaler", description="Report a problem to the moderators")
        @app_commands.describe(
            message="What's the problem?",
            anonymous="Hide your name from mods (default: no)",
        )
        async def signaler(interaction: discord.Interaction, message: str, anonymous: bool = False):
            report_channel_id = (
                _get_channel_id(self.config, "report_channel_id")
                or _get_channel_id(self.config, "mod_channel_id")
            )
            if report_channel_id is None:
                await interaction.response.send_message(
                    "Reporting is not configured. Contact an admin.", ephemeral=True
                )
                return
            channel = interaction.guild.get_channel(report_channel_id)
            if channel is None:
                await interaction.response.send_message(
                    "Report channel not found. Contact an admin.", ephemeral=True
                )
                return

            msg = message.strip()
            if len(msg) > 1500:
                msg = msg[:1500] + "…"

            reporter = "anonymous" if anonymous else f"{interaction.user} (`{interaction.user.id}`)"
            header = f"📨 **New report** from {reporter}"
            if interaction.channel and isinstance(interaction.channel, discord.TextChannel):
                header += f"\nChannel: {interaction.channel.mention}"

            embed = discord.Embed(
                title="Student report",
                description=msg,
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text=f"Guild: {interaction.guild.name}")

            try:
                await channel.send(content=header, embed=embed)
            except discord.HTTPException as exc:
                log.warning("Could not deliver report: %s", exc)
                await interaction.response.send_message(
                    "Could not deliver your report. Please contact a mod directly.", ephemeral=True
                )
                return

            log.info("Report from %s: %s", interaction.user, msg[:100])
            await interaction.response.send_message(
                "✅ Your report has been sent to the moderators. Thank you.",
                ephemeral=True,
            )

        # --------- /backup ---------

        @self.tree.command(name="backup", description="Send data files to the mod channel (admin)")
        async def backup(interaction: discord.Interaction):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            files = []
            missing = []
            for path, label in [
                (VERIFIED_PATH, "verified.json"),
                (VERIFY_LOG_PATH, "verify_log.csv"),
                (CONFIG_PATH, "config.json"),
                (REMINDERS_PATH, "reminders.json"),
            ]:
                if path.exists():
                    try:
                        files.append(discord.File(str(path), filename=label))
                    except Exception as exc:
                        missing.append(f"{label} ({exc})")
                else:
                    missing.append(label)

            if not files:
                await interaction.followup.send(
                    "No files to back up: " + ", ".join(missing), ephemeral=True
                )
                return

            mod_channel_id = _get_channel_id(self.config, "mod_channel_id")
            channel = interaction.guild.get_channel(mod_channel_id) if mod_channel_id else None

            if channel is None:
                await interaction.followup.send(
                    f"Mod channel not found — sending here instead.\n"
                    + (f"Missing: {', '.join(missing)}" if missing else ""),
                    files=files,
                    ephemeral=True,
                )
            else:
                try:
                    await channel.send(
                        content=f"💾 **Backup** requested by {interaction.user.mention}",
                        files=files,
                    )
                    await interaction.followup.send(
                        f"✅ Backup sent to {channel.mention}."
                        + (f"\nMissing: {', '.join(missing)}" if missing else ""),
                        ephemeral=True,
                    )
                except discord.HTTPException as exc:
                    log.warning("Backup delivery failed: %s", exc)
                    await interaction.followup.send(
                        f"Could not deliver backup: {exc}", ephemeral=True
                    )

        # --------- /reload_config ---------

        @self.tree.command(name="reload_config", description="Re-read config.json without restart (admin)")
        async def reload_config(interaction: discord.Interaction):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return

            try:
                fresh = load_config_file()
            except Exception as exc:
                await interaction.response.send_message(
                    f"❌ Failed to read config.json: {exc}", ephemeral=True
                )
                return

            old_keys = set(self.config.keys())
            new_keys = set(fresh.keys())
            changed = []
            added = sorted(new_keys - old_keys)
            removed = sorted(old_keys - new_keys)
            for k in sorted(new_keys & old_keys):
                if self.config[k] != fresh[k]:
                    changed.append(k)

            self.config = fresh
            log.info("Config reloaded: +%s -%s ~%s", added, removed, changed)

            summary = ["🔄 **Config reloaded**"]
            if added:
                summary.append(f"➕ Added keys: `{', '.join(added)}`")
            if removed:
                summary.append(f"➖ Removed keys: `{', '.join(removed)}`")
            if changed:
                summary.append(f"✏️ Changed keys: `{', '.join(changed)}`")
            if not (added or removed or changed):
                summary.append("No changes detected.")

            summary.append(
                "\n*Note: `xlsx_file` and role names are re-read at next use. "
                "Run `/refresh` to reload the student list, and re-run `/sync_groupes` if roles changed.*"
            )

            await interaction.response.send_message("\n".join(summary), ephemeral=True)

        # --------- /timeout ---------

        @self.tree.command(name="timeout", description="Apply a real Discord timeout to a member (admin)")
        @app_commands.describe(
            user="Member to time out",
            minutes="Duration in minutes (1–10080)",
            reason="Reason shown in the audit log",
        )
        async def timeout_cmd(interaction: discord.Interaction, user: discord.Member,
                              minutes: int, reason: str = "Manual timeout"):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return
            if minutes < 1 or minutes > 10080:
                await interaction.response.send_message(
                    "Minutes must be between 1 and 10080 (7 days).", ephemeral=True
                )
                return
            try:
                until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
                await user.timeout(until, reason=f"{reason} (by {interaction.user})")
            except discord.Forbidden:
                await interaction.response.send_message(
                    "❌ I can't time out that user — check my role hierarchy and "
                    "the `Moderate Members` permission.", ephemeral=True
                )
                return
            except discord.HTTPException as exc:
                await interaction.response.send_message(f"❌ Timeout failed: {exc}", ephemeral=True)
                return

            await interaction.response.send_message(
                f"🔇 Timed out {user.mention} for **{minutes} min**. Reason: {reason}",
                ephemeral=False,
            )

        # --------- /untimeout ---------

        @self.tree.command(name="untimeout", description="Remove a Discord timeout from a member (admin)")
        @app_commands.describe(user="Member to release")
        async def untimeout_cmd(interaction: discord.Interaction, user: discord.Member):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return
            try:
                await user.timeout(None, reason=f"Untimeout by {interaction.user}")
            except discord.Forbidden:
                await interaction.response.send_message(
                    "❌ I can't modify that user's timeout.", ephemeral=True
                )
                return
            except discord.HTTPException as exc:
                await interaction.response.send_message(f"❌ Failed: {exc}", ephemeral=True)
                return
            await interaction.response.send_message(f"🔊 {user.mention} is free again.", ephemeral=True)

        # --------- /creer_roles_groupes ---------

        @self.tree.command(name="creer_roles_groupes", description="Create G1..Gn roles from the list (admin)")
        async def creer_roles_groupes(interaction: discord.Interaction):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            fmt = cfg.get("group_role_format", "G{group}")
            groups = _all_td_groups(self.students)
            if not groups:
                await interaction.followup.send("No TD groups found in the student list.", ephemeral=True)
                return

            created, already, failed = [], [], []
            for g in groups:
                name = fmt.format(group=g)
                existing = discord.utils.get(interaction.guild.roles, name=name)
                if existing:
                    already.append(name)
                    continue
                try:
                    await interaction.guild.create_role(
                        name=name,
                        reason="Created by SII verify bot",
                        mentionable=True,
                    )
                    created.append(name)
                except discord.Forbidden:
                    failed.append(name)
                except discord.HTTPException as exc:
                    log.warning("Failed to create role %s: %s", name, exc)
                    failed.append(name)

            msg = []
            if created:
                msg.append(f"✅ Created: {', '.join(f'`{n}`' for n in created)}")
            if already:
                msg.append(f"ℹ️ Already existed: {', '.join(f'`{n}`' for n in already)}")
            if failed:
                msg.append(f"❌ Failed: {', '.join(f'`{n}`' for n in failed)}")
            await interaction.followup.send("\n".join(msg) or "Nothing to do.", ephemeral=True)

        # --------- /sync_groupes ---------

        @self.tree.command(name="sync_groupes", description="Bulk-assign G* roles to verified members from the list (admin)")
        @app_commands.describe(dry_run="If true, only reports what would change without touching roles")
        async def sync_groupes(interaction: discord.Interaction, dry_run: bool = False):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            fmt = cfg.get("group_role_format", "G{group}")
            verified_role_name = cfg.get("verified_role")
            verified_role_obj = discord.utils.get(interaction.guild.roles, name=verified_role_name)

            if verified_role_obj is None:
                await interaction.followup.send(
                    f"Role `{verified_role_name}` doesn't exist on this server.", ephemeral=True
                )
                return

            stats = {
                "checked": 0, "not_linked": 0, "no_group": 0, "role_missing": 0,
                "assigned": 0, "already_ok": 0, "removed_stale": 0, "errors": 0,
            }
            details = []

            for member in verified_role_obj.members:
                stats["checked"] += 1
                mat = self.verified.get(str(member.id))
                if not mat:
                    stats["not_linked"] += 1
                    details.append(f"• {member.mention} — *no linked matricule*")
                    continue
                student = self.students.get(mat)
                if student is None:
                    stats["not_linked"] += 1
                    details.append(f"• {member.mention} — matricule `{mat}` not in DB")
                    continue
                if not student.groupe_td:
                    stats["no_group"] += 1
                    details.append(f"• {member.mention} — no TD group in list")
                    continue

                target_name = fmt.format(group=student.groupe_td)
                target_role = discord.utils.get(interaction.guild.roles, name=target_name)
                if target_role is None:
                    stats["role_missing"] += 1
                    details.append(f"• {member.mention} — role `{target_name}` missing")
                    continue

                group_prefix = fmt.split("{group}")[0] if "{group}" in fmt else "G"
                current_group_roles = [
                    r for r in member.roles
                    if r.name.startswith(group_prefix)
                    and r.name[len(group_prefix):].isdigit()
                ]
                stale = [r for r in current_group_roles if r.name != target_name]

                if target_role in member.roles and not stale:
                    stats["already_ok"] += 1
                    continue

                if dry_run:
                    action = []
                    if stale:
                        action.append(f"remove {', '.join(r.name for r in stale)}")
                    if target_role not in member.roles:
                        action.append(f"add {target_name}")
                    details.append(f"• {member.mention} — would {'; '.join(action)}")
                    continue

                try:
                    if stale:
                        await member.remove_roles(*stale, reason="SII sync")
                        stats["removed_stale"] += len(stale)
                    if target_role not in member.roles:
                        await member.add_roles(target_role, reason="SII sync")
                        stats["assigned"] += 1
                except (discord.Forbidden, discord.HTTPException) as exc:
                    stats["errors"] += 1
                    details.append(f"• {member.mention} — ERROR: {exc}")

            header = (
                f"**{'DRY RUN — ' if dry_run else ''}Group sync report**\n"
                f"Verified members scanned: **{stats['checked']}**\n"
                f"Roles assigned: **{stats['assigned']}** · already OK: **{stats['already_ok']}**\n"
                f"Stale roles removed: **{stats['removed_stale']}**\n"
                f"No matricule link: **{stats['not_linked']}** · no TD group: **{stats['no_group']}**\n"
                f"Role missing on server: **{stats['role_missing']}** · errors: **{stats['errors']}**"
            )

            detail_text = ""
            if details:
                shown = details[:30]
                detail_text = "\n\n" + "\n".join(shown)
                if len(details) > 30:
                    detail_text += f"\n… and {len(details) - 30} more"

            await interaction.followup.send(header + detail_text, ephemeral=True)

        # --------- /verify_log ---------

        @self.tree.command(name="verify_log", description="Download the verification log as CSV (admin)")
        @app_commands.describe(last="Only include the last N entries (default 200, 0 = all)")
        async def verify_log_cmd(interaction: discord.Interaction, last: int = 200):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return

            if not VERIFY_LOG_PATH.exists():
                await interaction.response.send_message("No verifications logged yet.", ephemeral=True)
                return

            with open(VERIFY_LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()

            if len(lines) <= 1:
                await interaction.response.send_message("Log file is empty.", ephemeral=True)
                return

            header_row = lines[0]
            body = lines[1:]
            total = len(body)
            if last and last > 0:
                body = body[-last:]

            content = header_row + "".join(body)
            data = content.encode("utf-8")
            file = discord.File(io.BytesIO(data), filename="verify_log.csv")
            await interaction.response.send_message(
                f"📋 Verification log — **{len(body)}** entries shown (of {total} total).",
                file=file,
                ephemeral=True,
            )

        # --------- /check ---------

        @self.tree.command(name="check", description="Look up any matricule in the database")
        @app_commands.describe(matricule="Matricule to look up")
        async def check(interaction: discord.Interaction, matricule: str):
            mat = matricule.strip().replace(" ", "")
            if not mat.isdigit():
                await interaction.response.send_message("Please provide a numeric matricule.", ephemeral=True)
                return
            student = self.students.get(mat)
            if student is None:
                await interaction.response.send_message(f"`{mat}`: not found in the database.", ephemeral=True)
                return
            etat_label = ETAT_LABELS.get(student.etat.upper(), student.etat)
            await interaction.response.send_message(
                f"**{student.full_name}** — {student.palier} {student.specialite} (Section {student.section})\n"
                f"Matricule: `{student.matricule}` · Status: **{student.etat}** ({etat_label})\n"
                f"Groupe TD: {student.groupe_td or '?'} · TP: {student.groupe_tp or '?'}",
                ephemeral=True,
            )

        # --------- /stats ---------

        @self.tree.command(name="stats", description="Show bot statistics")
        async def stats(interaction: discord.Interaction):
            total = len(self.students)
            sii = sum(1 for s in self.students.values() if s.is_sii)
            admis = sum(1 for s in self.students.values() if s.is_admis)
            sii_admis = sum(1 for s in self.students.values() if s.is_sii and s.is_admis)

            verified_role_obj = discord.utils.get(interaction.guild.roles, name=verified_role)
            verified_count = len(verified_role_obj.members) if verified_role_obj else 0

            groups = _all_td_groups(self.students)

            log_entries = 0
            if VERIFY_LOG_PATH.exists():
                try:
                    with open(VERIFY_LOG_PATH, "r", encoding="utf-8") as f:
                        log_entries = max(0, sum(1 for _ in f) - 1)
                except Exception:
                    pass

            await interaction.response.send_message(
                f"📊 **Bot Statistics**\n"
                f"**Students loaded:** {total}\n"
                f"**SII students:** {sii}\n"
                f"**Admis (all):** {admis}\n"
                f"**SII + Admis (verifiable):** {sii_admis}\n"
                f"**Verified members in this server:** {verified_count}\n"
                f"**Rows skipped in xlsx:** {self.skipped_rows}\n"
                f"**Linked accounts (verified.json):** {len(self.verified)}\n"
                f"**Verification log entries:** {log_entries}\n"
                f"**Pending reminders:** {len(self.reminders)}\n"
                f"**TD groups in list:** {', '.join(groups) if groups else 'none'}",
                ephemeral=True,
            )

        # --------- /search ---------

        @self.tree.command(name="search", description="Search for a student by name or matricule (admin)")
        @app_commands.describe(query="Partial name or matricule (case-insensitive)")
        async def search(interaction: discord.Interaction, query: str):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return
            q = query.strip().lower()
            if len(q) < 2:
                await interaction.response.send_message("Enter at least 2 characters.", ephemeral=True)
                return

            matches = [
                s for s in self.students.values()
                if q in s.full_name.lower() or q in s.matricule.lower()
            ][:15]

            if not matches:
                await interaction.response.send_message(f"No students match `{query}`.", ephemeral=True)
                return

            lines = [
                f"`{s.matricule}` — **{s.full_name}** · {s.palier} {s.specialite} · {s.etat}"
                for s in matches
            ]
            header = f"🔎 **{len(matches)} result(s)** for `{query}`"
            if len(matches) == 15:
                header += " (showing first 15)"
            await interaction.response.send_message(header + "\n" + "\n".join(lines), ephemeral=True)

        # --------- /refresh ---------

        @self.tree.command(name="refresh", description="Reload the student list from the xlsx file (admin)")
        async def refresh(interaction: discord.Interaction):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return
            try:
                self.reload_students()
            except Exception as exc:
                await interaction.response.send_message(f"Failed to reload: {exc}", ephemeral=True)
                return
            await interaction.response.send_message(
                f"Reloaded **{len(self.students)}** students from the xlsx file.", ephemeral=True
            )

        # --------- /unverify ---------

        @self.tree.command(name="unverify", description="Remove verification from a user (admin)")
        @app_commands.describe(user="Member to unverify")
        async def unverify(interaction: discord.Interaction, user: discord.Member):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return

            verified_role_obj = discord.utils.get(interaction.guild.roles, name=verified_role)
            unverified_role_obj = discord.utils.get(interaction.guild.roles, name=unverified_role)

            if verified_role_obj is None:
                await interaction.response.send_message(f"Role `{verified_role}` not found.", ephemeral=True)
                return

            removed = []
            try:
                if verified_role_obj in user.roles:
                    await user.remove_roles(verified_role_obj, reason=f"Unverified by {interaction.user}")
                    removed.append(f"-{verified_role_obj.name}")
                if unverified_role_obj and unverified_role_obj not in user.roles:
                    await user.add_roles(unverified_role_obj, reason=f"Unverified by {interaction.user}")
                    removed.append(f"+{unverified_role_obj.name}")

                fmt = cfg.get("group_role_format", "G{group}")
                prefix = fmt.split("{group}")[0] if "{group}" in fmt else "G"
                group_roles = [
                    r for r in user.roles
                    if r.name.startswith(prefix) and r.name[len(prefix):].isdigit()
                ]
                if group_roles:
                    await user.remove_roles(*group_roles, reason=f"Unverified by {interaction.user}")
                    removed.extend(f"-{r.name}" for r in group_roles)
            except discord.Forbidden:
                await interaction.response.send_message(
                    "I don't have permission to modify that user's roles.", ephemeral=True
                )
                return

            if str(user.id) in self.verified:
                old_mat = self.verified[str(user.id)]
                self.matricule_to_user.pop(old_mat, None)
                del self.verified[str(user.id)]
                save_verified(self.verified)

            await interaction.response.send_message(
                f"✅ Unverified {user.mention}. Changes: {', '.join(removed) or 'none'}",
                ephemeral=True,
            )

        # --------- /export ---------

        @self.tree.command(name="export", description="Export the student list as CSV (admin)")
        async def export(interaction: discord.Interaction):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return

            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["Matricule", "Nom", "Prénom", "Palier", "Spécialité", "Section", "État", "TD", "TP"])
            for s in sorted(self.students.values(), key=lambda x: x.matricule):
                writer.writerow([
                    s.matricule, s.nom, s.prenom, s.palier, s.specialite,
                    s.section, s.etat, s.groupe_td, s.groupe_tp,
                ])

            data = buf.getvalue().encode("utf-8")
            file = discord.File(io.BytesIO(data), filename="students_export.csv")
            await interaction.response.send_message(
                f"📁 Export of **{len(self.students)}** students.", file=file, ephemeral=True
            )

        # --------- /list_commands ---------

        @self.tree.command(name="list_commands", description="Debug: show registered commands")
        async def list_commands(interaction: discord.Interaction):
            try:
                cmds = await self.tree.fetch_commands(guild=interaction.guild)
            except discord.HTTPException as exc:
                await interaction.response.send_message(f"Fetch failed: {exc}", ephemeral=True)
                return
            if not cmds:
                await interaction.response.send_message(
                    "No commands registered. The bot is missing the `applications.commands` scope — re-invite.",
                    ephemeral=True,
                )
                return
            names = "\n".join(f"- `/{c.name}` — {c.description}" for c in cmds)
            await interaction.response.send_message(f"Registered commands:\n{names}", ephemeral=True)

        # --------- /help ---------

        @self.tree.command(name="help", description="Show available commands")
        async def help_cmd(interaction: discord.Interaction):
            await interaction.response.send_message(
                "**📖 Available commands**\n\n"
                "**For students**\n"
                "`/verify <matricule>` — Verify yourself (only in the verify channel)\n"
                "`/mes_infos` — Show your full student record\n"
                "`/mes_groupes` — Show your TD and TP groups\n"
                "`/mes_camarades` — List classmates in your TD group\n"
                "`/annuaire <matricule>` — Look up a classmate's groups\n"
                "`/rappel <date> <message>` — Set a personal reminder (DM'd to you)\n"
                "`/mes_rappels` — List your reminders\n"
                "`/annuler_rappel <id>` — Cancel a reminder\n"
                "`/signaler <message>` — Send a report to the mod team\n"
                "`/info <topic>` — Planning, homework, rules\n"
                "`/stats` — Bot statistics\n"
                "`/help` — This message\n\n"
                "**Admins only**\n"
                "`/groupe <n>` — List ALL students in TD n\n"
                "`/timeout <user> <min>` · `/untimeout <user>`\n"
                "`/backup` · `/reload_config` · `/creer_roles_groupes` · `/sync_groupes` · `/verify_log`\n"
                "`/check` · `/search` · `/refresh` · `/unverify` · `/export` · `/list_commands`",
                ephemeral=True,
            )


# ---------------- entry point ----------------

def main():
    if not CONFIG_PATH.exists():
        log.error("config.json not found. Copy config.example.json to config.json and fill it in.")
        raise SystemExit(1)
    config = load_config_file()

    token = os.environ.get("DISCORD_TOKEN") or config.get("discord_token")
    if not token:
        log.error("No Discord token. Set DISCORD_TOKEN env var or discord_token in config.json")
        raise SystemExit(1)

    bot = VerifyBot(config)
    bot.run(token)


if __name__ == "__main__":
    main()