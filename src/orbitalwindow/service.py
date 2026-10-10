# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The application core: one object holding the DB, Docker and helpers, and every user action.

Every state-changing action is recorded in ``audit``. UI and HTTP handlers call these methods
and never touch Docker or the DB directly.
"""

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

import docker

from . import users as users_mod
from .auth import LoginRateLimiter, Sessions, generate_password, load_or_create_secret
from .auth import authenticate as auth_authenticate
from .builds import TAG_RE, Builder, desktop_image_ref
from .config import Settings
from .db import Database
from .desktops import Desktops, DesktopState, GpuVendor
from .hostops import HostOps
from .samba import Samba, SambaError
from .users import ValidationError, validate_password

log = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "default_cpus": "",
    "default_mem_gb": "",
    "default_quota_gb": "50",
    "scratch_retention_days": "30",
}


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
        self.desktops = Desktops(client, self.db, settings)
        self.samba = Samba(client, settings.samba_container, settings.samba_state_dir)
        self.builder = Builder(client, self.db, settings.image_context, settings.webtop_repo)
        self._reconcile_stop = threading.Event()

    # --- startup -----------------------------------------------------------------------------

    def bootstrap(self) -> None:
        for key, value in DEFAULT_SETTINGS.items():
            if self.db.get_setting(key) is None:
                self.db.set_setting(key, value)
        if self.db.get_setting("gpu_vendor") is None:
            self.db.set_setting("gpu_vendor", os.environ.get("OW_GPU_VENDOR", GpuVendor.NVIDIA))
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
                fixed = self.desktops.ensure_traefik_connected()
                if fixed:
                    log.info("reconnected Traefik to %d user network(s)", fixed)
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
        row = auth_authenticate(self.db, name, password)
        if row is None:
            self.limiter.failure(*keys)
            self.db.audit(name.strip().lower()[:64] or "?", "login.failed", detail=client_ip)
            return None
        self.limiter.success(*keys)
        self.db.audit(row["name"], "login", detail=client_ip)
        return self.sessions.issue(row["id"], row["session_ver"])

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
        if image_id is None:
            image_id = self.db.scalar("SELECT id FROM images WHERE enabled ORDER BY id LIMIT 1")
        row = users_mod.insert_user(
            self.db,
            name=name,
            display_name=display_name,
            password=password,
            is_admin=is_admin,
            image_id=image_id,
            cpus=defaults["cpus"] if cpus == "default" else cpus,  # type: ignore[arg-type]
            mem_gb=defaults["mem_gb"] if mem_gb == "default" else mem_gb,  # type: ignore[arg-type]
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

    # --- desktops ----------------------------------------------------------------------------

    def desktop_state(self, user_id: int) -> DesktopState:
        user = self._require_user(user_id)
        state = self.desktops.state(user["name"])
        if state is DesktopState.STOPPED and user["image_id"] is not None:
            status = self.db.scalar("SELECT status FROM images WHERE id = ?", [user["image_id"]])
            if status == "building":
                return DesktopState.BUILDING
        return state

    def start_desktop(self, actor: str, user_id: int) -> None:
        user = self._require_user(user_id)
        self.desktops.start(user)
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
        self.db.execute("UPDATE users SET image_id = ? WHERE id = ?", [image_id, user_id])
        self.db.audit(actor, "desktop.choose", user["name"], image["base_tag"])

    def enabled_images(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT * FROM images WHERE enabled ORDER BY label")

    # --- images ------------------------------------------------------------------------------

    def add_image(self, actor: str, base_tag: str, label: str | None = None) -> int:
        base_tag = base_tag.strip()
        if not TAG_RE.fullmatch(base_tag):
            raise ValidationError("That is not a valid image tag (for example: ubuntu-xfce).")
        if self.db.one("SELECT id FROM images WHERE base_tag = ?", [base_tag]):
            raise ValidationError(f"The desktop '{base_tag}' already exists.")
        image_id = self.db.scalar(
            "INSERT INTO images (label, base_tag, image_ref) VALUES (?, ?, ?) RETURNING id",
            [label or _label_for(base_tag), base_tag, desktop_image_ref(base_tag)],
        )
        self.db.audit(actor, "image.add", base_tag)
        self.builder.queue_base(image_id)
        return image_id


def _label_for(base_tag: str) -> str:
    """'ubuntu-xfce' → 'Ubuntu XFCE'."""
    words = base_tag.replace("_", "-").split("-")
    return " ".join(w.upper() if len(w) <= 4 and w != "ubuntu" else w.capitalize() for w in words)
