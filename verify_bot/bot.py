import csv
import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from students import load_students

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
VERIFIED_PATH = BASE_DIR / "verified.json"
VERIFY_LOG_PATH = BASE_DIR / "verify_log.csv"

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
MAX_STRIKES = 3                # 3 non-numeric messages ...
STRIKE_WINDOW_SECONDS = 60     # ... within 60s counts
TIMEOUT_SECONDS = 5 * 60       # ... then ignore for 5 min


# ---------------- helpers ----------------

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


def log_verification(user_id: int, username: str, display_name: str, matricule: str,
                     full_name: str = "", section: str = "", td: str = "", tp: str = "",
                     status: str = "OK"):
    """
    Append a row to verify_log.csv. Creates the file with a header on first write.
    Status is one of: OK, NOT_IN_LIST, WRONG_SPECIALITY, NOT_ADMIS, ALREADY_VERIFIED.
    """
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


def _all_td_groups(students: dict) -> list[str]:
    td = set()
    for s in students.values():
        if s.is_sii and s.groupe_td:
            td.add(s.groupe_td)
    return sorted(td, key=lambda x: (len(x), x))


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


async def _set_td_group_role(bot, member: discord.Member, td_group: str) -> str | None:
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


async def _notify_mods(bot, interaction: discord.Interaction, matricule: str, student, reason: str, success: bool = False):
    channel_id = bot.config.get("mod_channel_id")
    if not channel_id:
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
        self.verified: dict = load_verified()          # {discord_id_str: matricule}
        self.matricule_to_user: dict = {                # {matricule: discord_id_str}
            mat: uid for uid, mat in self.verified.items()
        }
        # anti-spam state per user: {user_id: {"strikes": [ts, ...], "until": ts}}
        self.spam_state: dict = {}
        self.reload_students()
        self._register_commands()

    # ---------- lifecycle ----------

    async def setup_hook(self):
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
                    "Synced 0 commands to guild %s. This usually means the bot lacks "
                    "the 'applications.commands' OAuth scope. Re-invite with the URL "
                    "from Developer Portal -> OAuth2 -> URL Generator, checking BOTH "
                    "'bot' and 'applications.commands'.",
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

    # ---------- message / interaction logging ----------

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is None:
            log.info("[DM] <%s>: %s", message.author, message.content)
            return

        # Anti-spam: only monitor the verify channel if configured
        verify_channel_id = self.config.get("verify_channel_id")
        if verify_channel_id and message.channel.id == int(verify_channel_id):
            now = time.monotonic()
            state = self.spam_state.setdefault(
                message.author.id,
                {"strikes": [], "until": 0.0},
            )

            # If user is currently timed out, silently ignore
            if state["until"] > now:
                log.info("Ignored message from %s (timeout active for %.0fs)",
                         message.author, state["until"] - now)
                return

            content = message.content.strip()
            if not content.isdigit():
                # count as a strike
                state["strikes"] = [t for t in state["strikes"] if now - t < STRIKE_WINDOW_SECONDS]
                state["strikes"].append(now)
                remaining = MAX_STRIKES - len(state["strikes"])
                log.info("Strike %d/%d for %s", len(state["strikes"]), MAX_STRIKES, message.author)

                if len(state["strikes"]) >= MAX_STRIKES:
                    state["until"] = now + TIMEOUT_SECONDS
                    state["strikes"] = []
                    log.warning("Muted %s for %ds (too many non-numeric messages)",
                                message.author, TIMEOUT_SECONDS)
                    try:
                        await message.channel.send(
                            f"{message.author.mention} you've sent too many non-numeric messages. "
                            f"I'll ignore you for **{TIMEOUT_SECONDS // 60} minutes**. "
                            "Please wait before trying again."
                        )
                    except discord.HTTPException:
                        pass
                elif remaining == 1:
                    try:
                        await message.channel.send(
                            f"{message.author.mention} please send only your matricule (a number). "
                            "One more non-numeric message and you'll be muted for 5 minutes."
                        )
                    except discord.HTTPException:
                        pass
                return  # don't log strikes as normal content

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
            channel = self.get_channel(channel_id)
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

            # ---- duplicate check ----
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
                # notify mods too
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

            # persist + update reverse index
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

        # --------- /groupe ---------

        @self.tree.command(name="groupe", description="List all students in a specific TD group")
        @app_commands.describe(numero="TD group number (e.g. 1)")
        async def groupe(interaction: discord.Interaction, numero: str):
            if not _is_verified_member(self, interaction.user) and not _is_admin(self, interaction.user):
                await interaction.response.send_message(
                    "You must be verified to use this command. Run `/verify` first.",
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

            lines = [f"`{s.matricule}` — {s.full_name} (Section {s.section})" for s in matches[:50]]
            header = f"👥 **TD {num}** — {len(matches)} student(s)"
            if len(matches) > 50:
                header += " (showing first 50)"
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

            created = []
            already = []
            failed = []

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
                "checked": 0,
                "not_linked": 0,
                "no_group": 0,
                "role_missing": 0,
                "assigned": 0,
                "already_ok": 0,
                "removed_stale": 0,
                "errors": 0,
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

            # clear the mapping + reverse index
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
                "`/verify <matricule>` — Verify yourself as an SII student\n"
                "`/mes_infos` — Show your full student record\n"
                "`/mes_groupes` — Show your TD and TP groups\n"
                "`/mes_camarades` — List classmates in your TD group\n"
                "`/groupe <n>` — List all students in TD group n\n"
                "`/annuaire <matricule>` — Look up a classmate's groups\n"
                "`/info <topic>` — Planning, homework, rules\n"
                "`/stats` — Bot statistics\n"
                "`/help` — This message\n\n"
                "**Admins only**\n"
                "`/creer_roles_groupes` · `/sync_groupes` · `/verify_log` · `/check` · `/search` · `/refresh` · `/unverify` · `/export` · `/list_commands`",
                ephemeral=True,
            )


# ---------------- entry point ----------------

def main():
    if not CONFIG_PATH.exists():
        log.error("config.json not found. Copy config.example.json to config.json and fill it in.")
        raise SystemExit(1)
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    token = os.environ.get("DISCORD_TOKEN") or config.get("discord_token")
    if not token:
        log.error("No Discord token. Set DISCORD_TOKEN env var or discord_token in config.json")
        raise SystemExit(1)

    bot = VerifyBot(config)
    bot.run(token)


if __name__ == "__main__":
    main()