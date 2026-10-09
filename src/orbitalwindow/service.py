# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The application core: one object holding the DB, Docker and helpers, and every user action.

Every state-changing action is recorded in ``audit``. UI and HTTP handlers call these methods
and never touch Docker or the DB directly.
"""

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any

import docker

from . import disk
from . import groups as groups_mod
from . import ssh as ssh_mod
from . import users as users_mod
from .auth import LoginRateLimiter, Sessions, generate_password, load_or_create_secret
from .auth import authenticate as auth_authenticate
from .builds import TAG_RE, Builder, desktop_image_ref
from .config import Settings
from .db import Database
from .desktops import (
    DesktopError,
    Desktops,
    DesktopState,
    GpuVendor,
    SshAccess,
    container_name,
)
from .disk import HostSpace
from .disk import Usage as DiskUsage
from .files import FileManagers, files_container_name
from .hostops import HostOps
from .metrics import ContainerMetrics, GpuMetrics, Usage
from .reconcile import reconcile
from .samba import Samba, SambaError
from .software import Software
from .users import ValidationError, validate_password

log = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "default_cpus": "",
    "default_mem_gb": "",
    "default_quota_gb": "50",
    "scratch_retention_days": "30",
}
GPU_VENDORS = tuple(v.value for v in GpuVendor)


@dataclass
class Result:
    """Outcome of an action: problems that didn't stop it are listed in ``warnings``."""

    warnings: list[str] = field(default_factory=list)
    password: str | None = None  # a generated password, to show once


class Service:
    def __init__(
        self,
        settings: Settings,
        client: docker.DockerClient,
        db: Database | None = None,
        prepare_layout: bool = True,
    ) -> None:
        self.settings = settings
        self.client = client
        self.hostops = HostOps(client, str(settings.root))
        if prepare_layout:
            self.hostops.prepare_layout(os.getuid())
        self.db = db or Database(settings.db_path)
        self.sessions = Sessions(
            self.db, load_or_create_secret(settings.secret_path), settings.session_hours * 3600
        )
        self.limiter = LoginRateLimiter(settings.login_max_failures, settings.login_window_seconds)
        self._login_slots = threading.BoundedSemaphore(4)
        self.desktops = Desktops(client, self.db, settings)
        self.files = FileManagers(client, settings)
        self.samba = Samba(client, settings.samba_container, settings.samba_state_dir)
        self.software = Software(self.db, settings.addons_dir)
        self.builder = Builder(
            client, self.db, self.software, settings.image_context, settings.webtop_repo
        )
        self.builder.on_base_ready = self._rebuild_users_of
        self.metrics = ContainerMetrics(settings.cadvisor_url)
        self.gpus = GpuMetrics()
        self._reconcile_stop = threading.Event()

    # --- startup -----------------------------------------------------------------------------

    def bootstrap(self) -> None:
        for key, value in DEFAULT_SETTINGS.items():
            if self.db.get_setting(key) is None:
                self.db.set_setting(key, value)
        if self.db.get_setting("gpu_vendor") is None:
            self.db.set_setting("gpu_vendor", os.environ.get("OW_GPU_VENDOR", GpuVendor.NVIDIA))
        if self.db.get_setting("hostname") is None:
            self.db.set_setting("hostname", self.settings.hostname)
        # The layout was prepared before the DB was open; write the scratch README with the
        # configured retention now.
        try:
            days = int(self.db.get_setting("scratch_retention_days") or 30)
            self.hostops.prepare_layout(os.getuid(), days)
        except docker.errors.DockerException as e:
            log.warning("could not update the scratch README: %s", e)
        self.builder.start()
        if not self.db.scalar("SELECT count(*) FROM images"):
            try:
                self.add_image("system", self.settings.default_desktop)
            except ValidationError as e:
                log.error("default desktop %r: %s", self.settings.default_desktop, e)
        if not self.db.scalar("SELECT count(*) FROM users"):
            self._create_first_admin()
        try:
            self.samba.sync(self.db)
        except (SambaError, docker.errors.DockerException) as e:
            log.warning("Samba sync at startup failed: %s", e)

    def _create_first_admin(self) -> None:
        secret = self.settings.admin_password
        if secret is None or not secret.get_secret_value():
            log.warning("No users yet and CONTROL_ADMIN_PASSWORD is not set: nobody can log in.")
            return
        result = self.create_user(
            actor="system",
            name=self.settings.admin_name,
            display_name="Administrator",
            password=secret.get_secret_value(),
            is_admin=True,
            allow_reserved=True,
        )
        for w in result.warnings:
            log.warning("first admin: %s", w)
        log.info("Created the first admin %r from CONTROL_ADMIN_PASSWORD", self.settings.admin_name)

    def start_reconcile(self) -> None:
        threading.Thread(target=self._reconcile_loop, name="ow-reconcile", daemon=True).start()

    def stop(self) -> None:
        self._reconcile_stop.set()
        self.builder.stop()

    def _reconcile_loop(self) -> None:
        while True:
            try:
                reconcile(self)
            except Exception:
                log.exception("reconcile failed")
            if self._reconcile_stop.wait(self.settings.reconcile_seconds):
                return

    # --- login -------------------------------------------------------------------------------

    def login(self, name: str, password: str, client_ip: str) -> str | None:
        """Return a session token, or None. Raises PermissionError when rate limited."""
        keys = (f"ip:{client_ip}", f"user:{name.strip().lower()}")
        if self.limiter.blocked(*keys):
            raise PermissionError("Too many failed attempts. Please wait a few minutes.")
        # argon2 needs 64 MB per check; cap how many run at once so a login flood can't exhaust RAM.
        with self._login_slots:
            row = auth_authenticate(self.db, name, password)
        if row is None:
            self.limiter.failure(*keys)
            self.db.audit(name.strip().lower()[:64] or "?", "login.failed", detail=client_ip)
            return None
        self.limiter.success(*keys)
        self.db.audit(row["name"], "login", detail=client_ip)
        return self.sessions.issue(row["id"], row["session_ver"])

    def logout(self, user_id: int) -> None:
        user = self._require_user(user_id)
        self.db.execute("UPDATE users SET session_ver = session_ver + 1 WHERE id = ?", [user_id])
        self.db.audit(user["name"], "logout", user["name"])

    # --- accounts ----------------------------------------------------------------------------

    def _defaults(self) -> dict[str, float | None]:
        return {
            "cpus": users_mod.parse_limit(self.db.get_setting("default_cpus")),
            "mem_gb": users_mod.parse_limit(self.db.get_setting("default_mem_gb")),
            "quota_gb": users_mod.parse_limit(self.db.get_setting("default_quota_gb")),
        }

    def create_user(
        self,
        *,
        actor: str,
        name: str,
        display_name: str,
        password: str | None = None,
        is_admin: bool = False,
        image_id: int | None = None,
        cpus: float | None | str = "default",
        mem_gb: float | None | str = "default",
        allow_reserved: bool = False,
    ) -> Result:
        result = Result()
        if password is None:
            password = result.password = generate_password()
        validate_password(password, self.settings.min_password_length)
        defaults = self._defaults()
        if cpus == "default" and mem_gb == "default":
            cpus_v, mem_v = defaults["cpus"], defaults["mem_gb"]
        else:
            cpus_v, mem_v = _validate_limits(
                defaults["cpus"] if cpus == "default" else cpus,
                defaults["mem_gb"] if mem_gb == "default" else mem_gb,
            )
        if image_id is None:
            image_id = self.db.scalar("SELECT id FROM images WHERE enabled ORDER BY id LIMIT 1")
        row = users_mod.insert_user(
            self.db,
            name=name,
            display_name=display_name,
            password=password,
            is_admin=is_admin,
            image_id=image_id,
            cpus=cpus_v,
            mem_gb=mem_v,
            quota_gb=defaults["quota_gb"],
            allow_reserved=allow_reserved,
        )
        self.db.audit(actor, "user.create", row["name"], "admin" if is_admin else "")
        try:
            self.hostops.create_home(row["name"], row["uid_gid"])
        except docker.errors.DockerException as e:
            log.exception("create home for %s", row["name"])
            result.warnings.append(f"The home folder could not be created: {e}")
        try:
            self.samba.sync(self.db)
            self.samba.set_password(row["name"], password)
        except (SambaError, docker.errors.DockerException) as e:
            log.exception("samba for %s", row["name"])
            result.warnings.append(f"Windows file sharing (Samba) was not updated: {e}")
        result.warnings += self.ensure_files(row["id"])
        return result

    def change_own_password(self, user_id: int, current: str, new: str) -> tuple[int, Result]:
        """Returns the new session version (so the caller can re-issue its cookie)."""
        user = users_mod.get_user(self.db, user_id)
        if user is None or auth_authenticate(self.db, user["name"], current) is None:
            raise ValidationError("The current password is not correct.")
        validate_password(new, self.settings.min_password_length)
        if new == current:
            raise ValidationError("The new password must be different from the current one.")
        ver = users_mod.set_password_hash(self.db, user_id, new)
        self.db.audit(user["name"], "password.change", user["name"])
        return ver, self._samba_password(user["name"], new)

    def admin_set_password(self, actor: str, user_id: int, new: str | None) -> Result:
        user = self._require_user(user_id)
        result = Result()
        if new is None:
            new = result.password = generate_password()
        validate_password(new, self.settings.min_password_length)
        users_mod.set_password_hash(self.db, user_id, new)
        self.db.audit(actor, "password.reset", user["name"])
        result.warnings += self._samba_password(user["name"], new).warnings
        return result

    def _samba_password(self, name: str, password: str) -> Result:
        result = Result()
        try:
            self.samba.set_password(name, password)
        except (SambaError, docker.errors.DockerException) as e:
            log.exception("samba password for %s", name)
            result.warnings.append(f"Windows file sharing (Samba) password was not updated: {e}")
        return result

    def _require_user(self, user_id: int) -> dict[str, Any]:
        user = users_mod.get_user(self.db, user_id)
        if user is None:
            raise ValidationError("That user no longer exists.")
        return user

    def list_users(self) -> list[dict[str, Any]]:
        return self.db.all(
            "SELECT u.*, i.label AS image_label FROM users u "
            "LEFT JOIN images i ON i.id = u.image_id ORDER BY u.name"
        )

    def _admins_left_without(self, user_id: int) -> int:
        return self.db.scalar(
            "SELECT count(*) FROM users WHERE is_admin AND NOT disabled AND id <> ?", [user_id]
        )

    def _guard_admin_change(self, actor_id: int | None, user: dict[str, Any], what: str) -> None:
        """Admins may not lock themselves out, and there must always be an enabled admin."""
        if actor_id is not None and user["id"] == actor_id:
            raise ValidationError(f"You cannot {what} your own account.")
        if user["is_admin"] and not user["disabled"] and not self._admins_left_without(user["id"]):
            raise ValidationError(f"You cannot {what} the last administrator.")

    def set_disabled(
        self, actor: str, actor_id: int | None, user_id: int, disabled: bool
    ) -> Result:
        """Disable: block login, desktop and Samba, end sessions. Enable: reverse (no desktop)."""
        user = self._require_user(user_id)
        result = Result()
        if disabled:
            self._guard_admin_change(actor_id, user, "disable")
            self.db.execute(
                "UPDATE users SET disabled = true, session_ver = session_ver + 1 WHERE id = ?",
                [user_id],
            )
            try:
                self.desktops.stop(user)
                self.files.stop(user["name"])
            except docker.errors.DockerException as e:
                result.warnings.append(f"The desktop could not be stopped: {e}")
        else:
            self.db.execute("UPDATE users SET disabled = false WHERE id = ?", [user_id])
            result.warnings += self.ensure_files(user_id)
        try:
            self.samba.set_enabled(user["name"], not disabled)
        except (SambaError, docker.errors.DockerException) as e:
            result.warnings.append(f"Windows file sharing (Samba) was not updated: {e}")
        self.db.audit(actor, "user.disable" if disabled else "user.enable", user["name"])
        return result

    def delete_user(
        self, actor: str, actor_id: int | None, user_id: int, delete_home: bool = False
    ) -> Result:
        """Remove the user and everything OrbitalWindow made for them. The home folder is kept
        unless ``delete_home``. UID and MAC are never reused. Audit rows are kept."""
        user = self._require_user(user_id)
        self._guard_admin_change(actor_id, user, "delete")
        name = user["name"]
        result = Result()
        try:
            self.desktops.stop(user)
            self.files.remove(name)
            self.hostops.remove_files_data(name)
        except docker.errors.DockerException as e:
            result.warnings.append(f"The desktop or file manager could not be removed: {e}")
        self.builder.remove_user_image(name)
        ssh_mod.remove_upstream_key(self.settings.state_dir, name)
        with self.db.transaction():
            self.db.execute("DELETE FROM disk_usage WHERE subject = 'user' AND name = ?", [name])
            for table in (
                "user_packages",
                "user_addons",
                "addon_grants",
                "user_image",
                "group_members",
                "ssh_keys",
            ):
                self.db.execute(f"DELETE FROM {table} WHERE user_id = ?", [user_id])
            self.db.execute("DELETE FROM users WHERE id = ?", [user_id])
        try:
            self.samba.remove_user(name, self.db)
        except (SambaError, docker.errors.DockerException) as e:
            result.warnings.append(f"Windows file sharing (Samba) was not updated: {e}")
        home = self.settings.users_dir / name
        if delete_home:
            try:
                self.hostops.remove_home(name)
            except docker.errors.DockerException as e:
                result.warnings.append(f"The home folder {home} could not be removed: {e}")
        else:
            result.warnings.append(f"The home folder was kept: {home}")
        self.db.audit(actor, "user.delete", name, "home deleted" if delete_home else "home kept")
        return result

    def set_admin(self, actor: str, actor_id: int | None, user_id: int, is_admin: bool) -> None:
        user = self._require_user(user_id)
        if not is_admin:
            self._guard_admin_change(actor_id, user, "remove administrator rights from")
        self.db.execute("UPDATE users SET is_admin = ? WHERE id = ?", [is_admin, user_id])
        self.db.audit(actor, "user.admin" if is_admin else "user.unadmin", user["name"])

    def set_limits(
        self, actor: str, user_id: int, cpus: float | str | None, mem_gb: float | str | None
    ) -> None:
        """CPU and RAM limits (None/empty/0 = unlimited). They apply at the next start."""
        user = self._require_user(user_id)
        cpus_v, mem_v = _validate_limits(cpus, mem_gb)
        self.db.execute(
            "UPDATE users SET cpus = ?, mem_gb = ? WHERE id = ?", [cpus_v, mem_v, user_id]
        )
        self.db.audit(actor, "user.limits", user["name"], f"cpus={cpus_v} mem_gb={mem_v}")

    def set_quota(self, actor: str, user_id: int, quota_gb: Any) -> None:
        """Soft disk quota for the home folder (empty = none)."""
        user = self._require_user(user_id)
        quota = _parse_quota(quota_gb)
        self.db.execute("UPDATE users SET quota_gb = ? WHERE id = ?", [quota, user_id])
        self.db.audit(actor, "user.quota", user["name"], str(quota))

    def set_display_name(self, actor: str, user_id: int, display_name: str) -> None:
        user = self._require_user(user_id)
        display_name = display_name.strip()
        if not display_name or len(display_name) > 80:
            raise ValidationError("The name must be 1–80 characters.")
        self.db.execute("UPDATE users SET display_name = ? WHERE id = ?", [display_name, user_id])
        self.db.audit(actor, "user.rename", user["name"], display_name)

    # --- file manager ------------------------------------------------------------------------

    def ensure_files(self, user_id: int, *, recreate: bool = False) -> list[str]:
        """Start (or recreate) the user's file manager. Returns warnings instead of raising."""
        user = self._require_user(user_id)
        if user["disabled"]:
            return []
        try:
            if recreate or not self._files_exists(user["name"]):
                self.hostops.create_files_data(user["name"], user["uid_gid"])
            self.files.ensure(user, groups_mod.groups_of(self.db, user_id), recreate=recreate)
        except docker.errors.DockerException as e:
            log.exception("file manager for %s", user["name"])
            return [f"The web file manager could not be started: {e}"]
        return []

    def _files_exists(self, name: str) -> bool:
        try:
            self.client.containers.get(files_container_name(name))
        except docker.errors.NotFound:
            return False
        return True

    # --- SSH keys ----------------------------------------------------------------------------

    def ssh_keys(self, user_id: int) -> list[dict[str, Any]]:
        return self.db.all(
            "SELECT id, label, fingerprint, pubkey, added_at FROM ssh_keys "
            "WHERE user_id = ? ORDER BY added_at",
            [user_id],
        )

    def add_ssh_key(self, actor: str, user_id: int, label: str, text: str) -> dict[str, Any]:
        user = self._require_user(user_id)
        key = ssh_mod.parse_public_key(text)
        label = ssh_mod.validate_label(label, key)
        with self.db.transaction():
            if len(self.ssh_keys(user_id)) >= ssh_mod.MAX_KEYS:
                raise ValidationError(f"You can add at most {ssh_mod.MAX_KEYS} keys.")
            if self.db.scalar(
                "SELECT count(*) FROM ssh_keys WHERE user_id = ? AND fingerprint = ?",
                [user_id, key.fingerprint],
            ):
                raise ValidationError("You have already added this key.")
            self.db.execute(
                "INSERT INTO ssh_keys (user_id, label, pubkey, fingerprint) VALUES (?, ?, ?, ?)",
                [user_id, label, key.line, key.fingerprint],
            )
        self.db.audit(actor, "ssh.key.add", user["name"], f"{label} {key.fingerprint}")
        return {"label": label, "fingerprint": key.fingerprint}

    def remove_ssh_key(self, actor: str, user_id: int, key_id: int) -> None:
        user = self._require_user(user_id)
        row = self.db.one("SELECT * FROM ssh_keys WHERE id = ? AND user_id = ?", [key_id, user_id])
        if row is None:
            raise ValidationError("That key no longer exists.")
        self.db.execute("DELETE FROM ssh_keys WHERE id = ?", [key_id])
        detail = f"{row['label']} {row['fingerprint']}"
        self.db.audit(actor, "ssh.key.remove", user["name"], detail)

    def _ssh_access(self, user: dict[str, Any]) -> SshAccess:
        private, _ = ssh_mod.ensure_upstream_key(self.settings.state_dir, user["name"])
        keys = [k["pubkey"] for k in self.ssh_keys(user["id"])]
        return SshAccess(
            ssh_mod.sshpiper_labels(user["name"], keys, private),
            self.settings.state_dir / "ssh" / f"{user['name']}.pub",
        )

    def ssh_restart_needed(self, user_id: int) -> bool:
        """True when the running desktop has other SSH keys than the user has now."""
        user = self._require_user(user_id)
        try:
            container = self.client.containers.get(container_name(user["name"]))
        except docker.errors.NotFound:
            return False
        running = container.labels.get("sshpiper.authorized_keys")
        if running is None:
            return False
        keys = [k["pubkey"] for k in self.ssh_keys(user_id)]
        wanted = ssh_mod.sshpiper_labels(user["name"], keys, "")["sshpiper.authorized_keys"]
        return running != wanted

    def ssh_command(self, user_name: str) -> str:
        return f"ssh -p {self.settings.ssh_port} {user_name}@{self.public_hostname()}"

    # --- groups ------------------------------------------------------------------------------

    def list_groups(self) -> list[dict[str, Any]]:
        return groups_mod.list_groups(self.db)

    def groups_of(self, user_id: int) -> list[dict[str, Any]]:
        return groups_mod.groups_of(self.db, user_id)

    def group_members(self, group_id: int) -> list[dict[str, Any]]:
        return groups_mod.members(self.db, group_id)

    def create_group(
        self, actor: str, name: str, description: str = "", quota_gb: Any = None
    ) -> Result:
        quota = _parse_quota(quota_gb)
        group = groups_mod.insert_group(self.db, name, description, quota)
        self.db.audit(actor, "group.create", group["name"], f"gid={group['gid']}")
        result = Result()
        try:
            self.hostops.create_group_dir(group["name"], group["gid"])
        except docker.errors.DockerException as e:
            result.warnings.append(f"The group folder could not be created: {e}")
        result.warnings += self._sync_samba()
        return result

    def set_group_quota(self, actor: str, group_id: int, quota_gb: Any) -> None:
        group = self._require_group(group_id)
        quota = _parse_quota(quota_gb)
        self.db.execute("UPDATE groups SET quota_gb = ? WHERE id = ?", [quota, group_id])
        self.db.audit(actor, "group.quota", group["name"], str(quota))

    def set_group_members(self, actor: str, group_id: int, user_ids: list[int]) -> Result:
        """Replace the member list. Samba and file managers follow at once; running desktops
        see the change after a restart (the dashboard says so)."""
        group = self._require_group(group_id)
        before = {m["id"] for m in groups_mod.members(self.db, group_id)}
        after = set(user_ids)
        names = {r["id"]: r["name"] for r in self.db.all("SELECT id, name FROM users")}
        if not after <= set(names):
            raise ValidationError("Some of those users no longer exist.")
        with self.db.transaction():
            self.db.execute("DELETE FROM group_members WHERE group_id = ?", [group_id])
            for uid in sorted(after):
                self.db.execute("INSERT INTO group_members VALUES (?, ?)", [group_id, uid])
        added, removed = after - before, before - after
        changes = [f"+{names[u]}" for u in sorted(added)]
        changes += [f"-{names.get(u, u)}" for u in sorted(removed)]
        self.db.audit(actor, "group.members", group["name"], " ".join(changes))
        result = Result()
        result.warnings += self._sync_samba()
        for uid in sorted(added | removed):
            result.warnings += self.ensure_files(uid, recreate=True)
        return result

    def delete_group(self, actor: str, group_id: int, delete_folder: bool = False) -> Result:
        group = self._require_group(group_id)
        member_ids = [m["id"] for m in groups_mod.members(self.db, group_id)]
        with self.db.transaction():
            self.db.execute("DELETE FROM group_members WHERE group_id = ?", [group_id])
            self.db.execute("DELETE FROM groups WHERE id = ?", [group_id])
            self.db.execute(
                "DELETE FROM disk_usage WHERE subject = 'group' AND name = ?", [group["name"]]
            )
        result = Result()
        result.warnings += self._sync_samba()
        for uid in member_ids:
            result.warnings += self.ensure_files(uid, recreate=True)
        folder = self.settings.groups_dir / group["name"]
        if delete_folder:
            try:
                self.hostops.remove_group_dir(group["name"])
            except docker.errors.DockerException as e:
                result.warnings.append(f"The folder {folder} could not be removed: {e}")
        else:
            result.warnings.append(f"The group folder was kept: {folder}")
        detail = "folder deleted" if delete_folder else "folder kept"
        self.db.audit(actor, "group.delete", group["name"], detail)
        return result

    def group_restart_needed(self, user_id: int) -> bool:
        """True when the running desktop was started with other groups than the user has now."""
        user = self._require_user(user_id)
        running = self.desktops.groups_in_use(user["name"])
        if running is None:
            return False
        return running != {g["name"] for g in groups_mod.groups_of(self.db, user_id)}

    def _require_group(self, group_id: int) -> dict[str, Any]:
        group = groups_mod.get_group(self.db, group_id)
        if group is None:
            raise ValidationError("That group no longer exists.")
        return group

    def _sync_samba(self) -> list[str]:
        try:
            self.samba.sync(self.db)
        except (SambaError, docker.errors.DockerException) as e:
            log.exception("samba sync")
            return [f"Windows file sharing (Samba) was not updated: {e}"]
        return []

    # --- disk --------------------------------------------------------------------------------

    def disk_usage_for(self, user_id: int) -> DiskUsage:
        return disk.usage_for_user(self.db, self._require_user(user_id))

    def disk_overview(self) -> tuple[list[DiskUsage], HostSpace]:
        return disk.all_usage(self.db), disk.host_space(self.settings.root)

    def measure_disk(self) -> int:
        return disk.measure(self.db, self.hostops)

    def clean_scratch(self) -> int:
        days = int(self.db.get_setting("scratch_retention_days") or 30)
        removed = disk.clean_scratch(self.hostops, days)
        detail = f"{removed} item(s), older than {days} days"
        self.db.audit("system", "scratch.clean", detail=detail)
        return removed

    # --- settings ----------------------------------------------------------------------------

    def get_settings(self) -> dict[str, str]:
        rows = self.db.all("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in rows}

    def public_hostname(self) -> str:
        return self.db.get_setting("hostname") or self.settings.hostname

    def update_settings(self, actor: str, values: dict[str, Any]) -> None:
        """Validate and store admin-editable settings (only the keys given)."""
        clean: dict[str, str] = {}
        if "default_cpus" in values or "default_mem_gb" in values:
            cpus, mem = _validate_limits(values.get("default_cpus"), values.get("default_mem_gb"))
            clean["default_cpus"] = "" if cpus is None else f"{cpus:g}"
            clean["default_mem_gb"] = "" if mem is None else f"{mem:g}"
        if "default_quota_gb" in values:
            quota = users_mod.parse_limit(values["default_quota_gb"])
            clean["default_quota_gb"] = "" if quota is None else f"{quota:g}"
        if "scratch_retention_days" in values:
            days = _int_in(values["scratch_retention_days"], 1, 3650, "Scratch retention")
            clean["scratch_retention_days"] = str(days)
        if "gpu_vendor" in values:
            if values["gpu_vendor"] not in GPU_VENDORS:
                raise ValidationError("Unknown GPU vendor.")
            clean["gpu_vendor"] = values["gpu_vendor"]
        if "hostname" in values:
            host = str(values["hostname"]).strip().lower()
            if not HOSTNAME_RE.fullmatch(host):
                raise ValidationError("The host name may contain letters, digits, dots and '-'.")
            clean["hostname"] = host
        with self.db.transaction():
            for key, value in clean.items():
                self.db.set_setting(key, value)
        detail = ", ".join(f"{k}={v}" for k, v in clean.items())
        self.db.audit(actor, "settings.update", detail=detail)
        if "scratch_retention_days" in clean:
            # Rewrites the scratch README with the new number of days.
            self.hostops.prepare_layout(os.getuid(), int(clean["scratch_retention_days"]))

    # --- audit and metrics -------------------------------------------------------------------

    def audit_log(self, search: str = "", limit: int = 200) -> list[dict[str, Any]]:
        like = f"%{search.strip()}%"
        return self.db.all(
            "SELECT * FROM audit WHERE ? = '%%' OR actor ILIKE ? OR action ILIKE ? "
            "OR target ILIKE ? OR detail ILIKE ? ORDER BY ts DESC LIMIT ?",
            [like, like, like, like, like, limit],
        )

    def _managed_ids(self) -> dict[str, str]:
        """Full container ID → name of every OrbitalWindow container (one Docker call)."""
        containers = self.client.containers.list(filters={"label": "ow.managed=true"}, sparse=True)
        return {c.id: c.attrs.get("Names", [c.id])[0].lstrip("/") for c in containers}

    def usage_all(self) -> dict[str, Usage]:
        """CPU/RAM per container name (empty if cAdvisor is unreachable)."""
        return self.metrics.usage(self._managed_ids())

    def usage_for(self, name: str) -> Usage | None:
        try:
            cid = self.client.containers.get(container_name(name)).id
        except docker.errors.NotFound:
            return None
        return self.metrics.usage({cid: container_name(name)}).get(container_name(name))

    def system_overview(self) -> list[dict[str, Any]]:
        """One row per user: desktop state, image, CPU/RAM use and limits."""
        states = self.desktops.states()
        usage = self.usage_all()
        rows = []
        for u in self.list_users():
            use = usage.get(container_name(u["name"]))
            rows.append(
                u
                | {
                    "state": states.get(u["name"], DesktopState.STOPPED).value,
                    "cpu_cores": use.cpu_cores if use else None,
                    "mem_bytes": use.mem_bytes if use else None,
                }
            )
        return rows

    # --- desktops ----------------------------------------------------------------------------

    def desktop_state(self, user_id: int) -> DesktopState:
        user = self._require_user(user_id)
        state = self.desktops.state(user["name"])
        if state is DesktopState.STOPPED and user["image_id"] is not None:
            image = self.db.one("SELECT * FROM images WHERE id = ?", [user["image_id"]])
            if image and image["status"] == "building" and image["built_at"] is None:
                return DesktopState.BUILDING
            row = self.software.user_image(user_id)
            if (
                row
                and row["status"] in ("queued", "building")
                and not self._usable_user_image(user, row)
            ):
                return DesktopState.BUILDING
        return state

    @staticmethod
    def _usable_user_image(user: dict[str, Any], row: dict[str, Any] | None) -> bool:
        return bool(row and row["built_at"] and row["built_from_image_id"] == user["image_id"])

    def image_ref_for(self, user: dict[str, Any]) -> str:
        """The image a start uses: the per-user image if the user needs one, else the desktop."""
        if user["image_id"] is None:
            raise DesktopError("No desktop has been chosen yet.")
        image = self.db.one("SELECT * FROM images WHERE id = ?", [user["image_id"]])
        if image is None or not image["enabled"]:
            raise DesktopError("The chosen desktop is no longer available. Please pick another.")
        if image["built_at"] is None:
            raise DesktopError("The chosen desktop is still being prepared. Try again shortly.")
        if self.software.plan(user).hash is None:
            return image["image_ref"]
        row = self.software.user_image(user["id"])
        if self._usable_user_image(user, row):
            return row["image_ref"]  # type: ignore[index]
        if row and row["status"] in ("queued", "building"):
            raise DesktopError(
                "Your software is being installed. Your desktop can start when it is done."
            )
        raise DesktopError(
            "Your software has not been installed for this desktop yet. "
            "Press Apply in the Software section."
        )

    def restart_needed(self, user_id: int) -> bool:
        """True when the running desktop was started with other software than it would get now."""
        user = self._require_user(user_id)
        try:
            ref = self.image_ref_for(user)
            container = self.client.containers.get(container_name(user["name"]))
            current = self.client.images.get(ref).id
        except DesktopError, docker.errors.NotFound:
            return False
        active = {a.key for a in self.software.active_addons(user_id)}
        return container.image.id != current or active != self.desktops.addons_in_use(user["name"])

    def start_desktop(self, actor: str, user_id: int) -> None:
        user = self._require_user(user_id)
        addons = self.software.active_addons(user_id)
        self.desktops.start(
            user,
            self.image_ref_for(user),
            addons,
            groups_mod.groups_of(self.db, user_id),
            self._ssh_access(user) if any(a.key == ssh_mod.SSH_ADDON for a in addons) else None,
        )
        self.db.audit(actor, "desktop.start", user["name"])

    def stop_desktop(self, actor: str, user_id: int, by_admin: bool = False) -> None:
        user = self._require_user(user_id)
        self.desktops.stop(user)
        if by_admin:
            self.db.execute("UPDATE users SET stopped_by_admin = true WHERE id = ?", [user_id])
        self.db.audit(actor, "desktop.stop", user["name"], "by admin" if by_admin else "")

    def choose_image(self, actor: str, user_id: int, image_id: int) -> None:
        user = self._require_user(user_id)
        image = self.db.one("SELECT * FROM images WHERE id = ? AND enabled", [image_id])
        if image is None:
            raise ValidationError("That desktop is not available.")
        if self.desktops.state(user["name"]) is not DesktopState.STOPPED:
            raise ValidationError("Stop your desktop before choosing another one.")
        self.db.execute("UPDATE users SET image_id = ? WHERE id = ?", [image_id, user_id])
        self.db.audit(actor, "desktop.choose", user["name"], image["base_tag"])
        # Packages may not exist for the new desktop; keep only those that do.
        dropped = self._drop_unknown_packages(user_id, image_id)
        if dropped:
            self.db.audit("system", "packages.drop", user["name"], " ".join(dropped))
        user = self._require_user(user_id)
        if self.software.plan(user).hash is not None and image["built_at"] is not None:
            self.builder.queue_user(user_id)

    def _drop_unknown_packages(self, user_id: int, image_id: int) -> list[str]:
        if not self.db.scalar("SELECT count(*) FROM pkg_index WHERE image_id = ?", [image_id]):
            return []
        rows = self.db.all(
            "SELECT p.name FROM user_packages p WHERE p.user_id = ? AND NOT EXISTS "
            "(SELECT 1 FROM pkg_index i WHERE i.image_id = ? AND i.name = p.name)",
            [user_id, image_id],
        )
        names = [r["name"] for r in rows]
        for name in names:
            self.software.remove_package(user_id, name)
        return names

    def enabled_images(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT * FROM images WHERE enabled ORDER BY label")

    # --- software (add-ons and packages) -----------------------------------------------------

    def set_addon(self, actor: str, user_id: int, key: str, on: bool) -> None:
        user = self._require_user(user_id)
        self.software.set_addon(user_id, key, on)
        self.db.audit(actor, "addon.on" if on else "addon.off", user["name"], key)

    def add_package(self, actor: str, user_id: int, name: str) -> str:
        user = self._require_user(user_id)
        name = self.software.add_package(user_id, user["image_id"], name)
        self.db.audit(actor, "package.add", user["name"], name)
        return name

    def remove_package(self, actor: str, user_id: int, name: str) -> None:
        user = self._require_user(user_id)
        self.software.remove_package(user_id, name)
        self.db.audit(actor, "package.remove", user["name"], name)

    def software_pending(self, user_id: int) -> bool:
        """True when the add-on/package selection differs from the last good per-user image."""
        user = self._require_user(user_id)
        if user["image_id"] is None:
            return False
        if self.software.plan(user).hash is None:
            return self.software.user_image(user_id) is not None
        return not self.software.up_to_date(user)

    def apply_software(self, actor: str, user_id: int) -> int | None:
        """Queue a per-user build if anything changed. Returns the build id, or None."""
        user = self._require_user(user_id)
        if not self.software_pending(user_id):
            return None
        image = self.db.one("SELECT * FROM images WHERE id = ?", [user["image_id"]])
        if image is None or image["built_at"] is None:
            raise ValidationError("The chosen desktop is still being prepared. Try again shortly.")
        build_id = self.builder.queue_user(user_id)
        self.db.audit(actor, "software.apply", user["name"])
        return build_id

    def build_log(self, build_id: int) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM builds WHERE id = ?", [build_id])

    def _rebuild_users_of(self, image_id: int) -> None:
        """After a desktop is rebuilt, rebuild the per-user images built on it."""
        for user in self.db.all("SELECT * FROM users WHERE image_id = ?", [image_id]):
            if self.software.plan(user).hash is not None:
                self.builder.queue_user(user["id"])

    # --- add-on administration ---------------------------------------------------------------

    def reload_addons(self, actor: str) -> list[str]:
        catalog = self.software.reload()
        self.db.audit(actor, "addons.reload", detail=f"{len(catalog.addons)} ok, "
                      f"{len(catalog.errors)} with errors")  # fmt: skip
        self._rebuild_stale()
        return [f"{e.key}: {e.error}" for e in catalog.errors]

    def _rebuild_stale(self) -> None:
        """Rebuild per-user images whose add-ons changed (files edited, add-on disabled)."""
        for row in self.db.all("SELECT user_id FROM user_image WHERE built_at IS NOT NULL"):
            if self.software_pending(row["user_id"]):
                self.builder.queue_user(row["user_id"])

    def set_addon_enabled(self, actor: str, key: str, enabled: bool) -> None:
        self.db.execute("UPDATE addon_settings SET enabled = ? WHERE key = ?", [enabled, key])
        self.db.audit(actor, "addon.enable" if enabled else "addon.disable", key)
        self._rebuild_stale()

    def set_addon_restricted(self, actor: str, key: str, restricted: bool | None) -> None:
        self.db.execute(
            "UPDATE addon_settings SET restricted_override = ? WHERE key = ?", [restricted, key]
        )
        self.db.audit(actor, "addon.restricted", key, str(restricted))

    def grant_addon(self, actor: str, user_id: int, key: str) -> None:
        user = self._require_user(user_id)
        self.db.execute(
            "INSERT INTO addon_grants VALUES (?, ?) ON CONFLICT DO NOTHING", [user_id, key]
        )
        self.db.audit(actor, "addon.grant", user["name"], key)

    def revoke_addon(self, actor: str, user_id: int, key: str) -> Result:
        """Revoke a grant: switch the add-on off, and stop the desktop if it uses its mounts."""
        user = self._require_user(user_id)
        result = Result()
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM addon_grants WHERE user_id = ? AND key = ?", [user_id, key]
            )
            self.db.execute("DELETE FROM user_addons WHERE user_id = ? AND key = ?", [user_id, key])
        self.db.audit(actor, "addon.revoke", user["name"], key)
        addon = self.software.catalog.addons.get(key)
        if addon and addon.spec.mounts and key in self.desktops.addons_in_use(user["name"]):
            self.stop_desktop(actor, user_id, by_admin=True)
            result.warnings.append(f"The desktop of {user['name']} was stopped.")
        if self.software_pending(user_id):
            self.builder.queue_user(user_id)
        return result

    def grants(self, user_id: int) -> set[str]:
        rows = self.db.all("SELECT key FROM addon_grants WHERE user_id = ?", [user_id])
        return {r["key"] for r in rows}

    # --- images ------------------------------------------------------------------------------

    def add_image(self, actor: str, base_tag: str, label: str | None = None) -> int:
        base_tag = base_tag.strip()
        if not TAG_RE.fullmatch(base_tag):
            raise ValidationError("That is not a valid image tag (for example: ubuntu-xfce).")
        if self.db.one("SELECT id FROM images WHERE base_tag = ?", [base_tag]):
            raise ValidationError(f"The desktop '{base_tag}' already exists.")
        image_id = self.db.scalar(
            "INSERT INTO images (label, base_tag, image_ref) VALUES (?, ?, ?) RETURNING id",
            [(label or "").strip() or _label_for(base_tag), base_tag, desktop_image_ref(base_tag)],
        )
        self.db.audit(actor, "image.add", base_tag)
        self.builder.queue_base(image_id)
        return image_id

    def list_images(self) -> list[dict[str, Any]]:
        return self.db.all(
            "SELECT i.*, (SELECT count(*) FROM users u WHERE u.image_id = i.id) AS users, "
            "(SELECT count(*) FROM pkg_index p WHERE p.image_id = i.id) AS packages "
            "FROM images i ORDER BY i.label"
        )

    def rebuild_image(self, actor: str, image_id: int) -> int:
        image = self._require_image(image_id)
        if image["status"] == "building":
            raise ValidationError("This desktop is already being built.")
        self.db.audit(actor, "image.rebuild", image["base_tag"])
        return self.builder.queue_base(image_id)

    def set_image_enabled(self, actor: str, image_id: int, enabled: bool) -> None:
        image = self._require_image(image_id)
        self.db.execute("UPDATE images SET enabled = ? WHERE id = ?", [enabled, image_id])
        self.db.audit(actor, "image.enable" if enabled else "image.disable", image["base_tag"])

    def remove_image(self, actor: str, image_id: int) -> None:
        image = self._require_image(image_id)
        if self.db.scalar("SELECT count(*) FROM users WHERE image_id = ?", [image_id]):
            raise ValidationError("Users still use this desktop. Move them to another one first.")
        if image["status"] == "building":
            raise ValidationError("Wait for the build to finish first.")
        with self.db.transaction():
            self.db.execute("DELETE FROM pkg_index WHERE image_id = ?", [image_id])
            self.db.execute("DELETE FROM images WHERE id = ?", [image_id])
        try:
            self.client.images.remove(image["image_ref"])
        except docker.errors.ImageNotFound:
            pass
        except docker.errors.APIError as e:
            log.warning("could not remove %s: %s", image["image_ref"], e)
        self.db.audit(actor, "image.remove", image["base_tag"])

    def _require_image(self, image_id: int) -> dict[str, Any]:
        image = self.db.one("SELECT * FROM images WHERE id = ?", [image_id])
        if image is None:
            raise ValidationError("That desktop no longer exists.")
        return image


_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
HOSTNAME_RE = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})*$")


def _parse_quota(value: Any) -> float | None:
    try:
        quota = users_mod.parse_limit(value)
    except ValueError:
        raise ValidationError("The quota must be a number of GB (empty for none).") from None
    if quota is not None and quota < 1:
        raise ValidationError("The quota must be at least 1 GB (empty for none).")
    return quota


def _int_in(value: Any, low: int, high: int, what: str) -> int:
    try:
        number = int(str(value).strip())
    except ValueError:
        raise ValidationError(f"{what} must be a whole number.") from None
    if not low <= number <= high:
        raise ValidationError(f"{what} must be between {low} and {high}.")
    return number


def _validate_limits(cpus: Any, mem_gb: Any) -> tuple[float | None, float | None]:
    try:
        cpus_v = users_mod.parse_limit(cpus)
        mem_v = users_mod.parse_limit(mem_gb)
    except ValueError:
        raise ValidationError("Limits must be numbers (leave empty for unlimited).") from None
    host_cpus = os.cpu_count() or 1
    if cpus_v is not None and not 0.25 <= cpus_v <= host_cpus:
        raise ValidationError(f"CPUs must be between 0.25 and {host_cpus} (this server).")
    if mem_v is not None and mem_v < 2:
        raise ValidationError("A desktop needs at least 2 GB of RAM.")
    return cpus_v, mem_v


def _label_for(base_tag: str) -> str:
    """'ubuntu-xfce' → 'Ubuntu XFCE'."""
    words = base_tag.replace("_", "-").split("-")
    return " ".join(w.upper() if len(w) <= 4 and w != "ubuntu" else w.capitalize() for w in words)
