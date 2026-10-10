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
from .desktops import DesktopError, Desktops, DesktopState, GpuVendor, container_name
from .hostops import HostOps
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
        self.software = Software(self.db, settings.addons_dir)
        self.builder = Builder(
            client, self.db, self.software, settings.image_context, settings.webtop_repo
        )
        self.builder.on_base_ready = self._rebuild_users_of
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
        self.desktops.start(user, self.image_ref_for(user), self.software.active_addons(user_id))
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


def _label_for(base_tag: str) -> str:
    """'ubuntu-xfce' → 'Ubuntu XFCE'."""
    words = base_tag.replace("_", "-").split("-")
    return " ".join(w.upper() if len(w) <= 4 and w != "ubuntu" else w.capitalize() for w in words)
