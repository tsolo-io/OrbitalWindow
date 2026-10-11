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
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

import docker

from . import disk
from . import fit as fit_mod
from . import groups as groups_mod
from . import models as models_mod
from . import ssh as ssh_mod
from . import users as users_mod
from .aisession import MIN_CONTEXT, AiSessions, SessionRequest, SessionState
from .auth import LoginRateLimiter, Sessions, generate_password, load_or_create_secret
from .auth import authenticate as auth_authenticate
from .builds import TAG_RE, Builder, desktop_image_ref
from .chat import Chat
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
from .downloads import Downloader
from .files import FileManagers, files_container_name
from .hostops import HostOps
from .hoststats import NodeExporter
from .metrics import ContainerMetrics, GpuMetrics, Usage
from .models import HubClient, LocalModel, ModelLibrary, RepoInfo, Space
from .reconcile import reconcile
from .samba import Samba, SambaError
from .software import Software
from .suggestions import LlmFit, Suggestion, SuggestionError
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
        """Wire up the database, Docker helpers and background workers.

        Args:
            settings: Application settings.
            client: Docker client used for every container and image operation.
            db: Database to use; opened from ``settings.db_path`` when None.
            prepare_layout: Create the host folder layout under the root before anything else.
        """
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
        self.node_exporter = NodeExporter(settings.node_exporter_url)
        self.models = ModelLibrary(settings.models_dir)
        self.hub = HubClient()
        self.llmfit = LlmFit(client, settings.llmfit_image)
        self.ai = AiSessions(client, self.db, settings)
        self.chat = Chat(client, settings)
        self.downloads = Downloader(client, self.db, self.models, os.getuid(), self.hf_token)
        self._reconcile_stop = threading.Event()

    # --- startup -----------------------------------------------------------------------------

    def bootstrap(self) -> None:
        """Prepare a fresh or restarted installation.

        Stores default settings that are missing, rewrites the scratch README, starts the builder,
        adds the default desktop and the first admin when there are none yet, and syncs Samba.
        Problems are logged rather than raised.
        """
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
        except docker.errors.DockerException as exc:
            log.warning("could not update the scratch README: %s", exc)
        self.builder.start()
        self.downloads.start()
        if not self.db.scalar("SELECT count(*) FROM images"):
            try:
                self.add_image("system", self.settings.default_desktop)
            except ValidationError as exc:
                log.error("default desktop %r: %s", self.settings.default_desktop, exc)
        if not self.db.scalar("SELECT count(*) FROM users"):
            self._create_first_admin()
        try:
            self.samba.sync(self.db)
        except (SambaError, docker.errors.DockerException) as exc:
            log.warning("Samba sync at startup failed: %s", exc)

    def _create_first_admin(self) -> None:
        """Create the first administrator from ``CONTROL_ADMIN_PASSWORD``.

        Logs a warning and does nothing when no password is configured.
        """
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
        for warning in result.warnings:
            log.warning("first admin: %s", warning)
        log.info("Created the first admin %r from CONTROL_ADMIN_PASSWORD", self.settings.admin_name)

    def start_reconcile(self) -> None:
        """Start the reconcile loop in a daemon thread."""
        threading.Thread(target=self._reconcile_loop, name="ow-reconcile", daemon=True).start()

    def stop(self) -> None:
        """Stop the reconcile loop, the image builder and the download worker."""
        self._reconcile_stop.set()
        self.builder.stop()
        self.downloads.stop()

    def _reconcile_loop(self) -> None:
        """Run ``reconcile`` every ``reconcile_seconds`` until ``stop`` is called.

        Errors are logged and the loop carries on.
        """
        while True:
            try:
                reconcile(self)
            except Exception:
                log.exception("reconcile failed")
            if self._reconcile_stop.wait(self.settings.reconcile_seconds):
                return

    # --- login -------------------------------------------------------------------------------

    def login(self, name: str, password: str, client_ip: str) -> str | None:
        """Return a session token, or None.

        Failed and successful attempts are audited and counted by the rate limiter.

        Args:
            name: Login name as typed.
            password: Password as typed.
            client_ip: Address of the client, used for rate limiting and the audit log.

        Returns:
            A session token, or None when the name or password is wrong.

        Raises:
            PermissionError: When the IP or user is rate limited.
        """
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
        """End all sessions of a user by bumping their session version.

        Args:
            user_id: Id of the user logging out.

        Raises:
            ValidationError: When the user no longer exists.
        """
        user = self._require_user(user_id)
        self.db.execute("UPDATE users SET session_ver = session_ver + 1 WHERE id = ?", [user_id])
        self.db.audit(user["name"], "logout", user["name"])

    # --- accounts ----------------------------------------------------------------------------

    def _defaults(self) -> dict[str, float | None]:
        """Return the default limits for new users from the settings.

        Returns:
            ``cpus``, ``mem_gb`` and ``quota_gb``; None means unlimited/none.
        """
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
        """Create a user with home folder, Samba account and file manager.

        Problems after the user row is stored are returned as warnings.

        Args:
            actor: Name recorded in the audit log.
            name: Login name of the new user.
            display_name: Human-readable name.
            password: Initial password; a generated one is used (and returned) when None.
            is_admin: Make the user an administrator.
            image_id: Desktop image; the first enabled one when None.
            cpus: CPU limit, or ``"default"`` for the configured default.
            mem_gb: RAM limit in GB, or ``"default"`` for the configured default.
            allow_reserved: Allow names that are normally reserved.

        Returns:
            The warnings and, if generated, the password to show once.

        Raises:
            ValidationError: When the password, limits or user details are not valid.
        """
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
        except docker.errors.DockerException as exc:
            log.exception("create home for %s", row["name"])
            result.warnings.append(f"The home folder could not be created: {exc}")
        try:
            self.samba.sync(self.db)
            self.samba.set_password(row["name"], password)
        except (SambaError, docker.errors.DockerException) as exc:
            log.exception("samba for %s", row["name"])
            result.warnings.append(f"Windows file sharing (Samba) was not updated: {exc}")
        result.warnings += self.ensure_files(row["id"])
        return result

    def change_own_password(self, user_id: int, current: str, new: str) -> tuple[int, Result]:
        """Change a user's own password after checking the current one.

        Args:
            user_id: Id of the user changing their password.
            current: The current password.
            new: The new password.

        Returns:
            The new session version (so the caller can re-issue its cookie) and the result of
            updating the Samba password.

        Raises:
            ValidationError: When the current password is wrong or the new one is not acceptable.
        """
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
        """Set or reset a user's password as an administrator.

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user whose password is set.
            new: The new password; a generated one is used (and returned) when None.

        Returns:
            Warnings and, if generated, the password to show once.

        Raises:
            ValidationError: When the user no longer exists or the password is not acceptable.
        """
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
        """Set the Samba password of a user, turning failures into warnings.

        Args:
            name: User name.
            password: The new password.

        Returns:
            A result with a warning when Samba was not updated.
        """
        result = Result()
        try:
            self.samba.set_password(name, password)
        except (SambaError, docker.errors.DockerException) as exc:
            log.exception("samba password for %s", name)
            result.warnings.append(f"Windows file sharing (Samba) password was not updated: {exc}")
        return result

    def _require_user(self, user_id: int) -> dict[str, Any]:
        """Return a user row or fail.

        Args:
            user_id: Id of the user.

        Returns:
            The user row.

        Raises:
            ValidationError: When the user no longer exists.
        """
        user = users_mod.get_user(self.db, user_id)
        if user is None:
            raise ValidationError("That user no longer exists.")
        return user

    def list_users(self) -> list[dict[str, Any]]:
        """Return all users, sorted by name.

        Returns:
            User rows with the ``image_label`` of their desktop added.
        """
        return self.db.all(
            "SELECT u.*, i.label AS image_label FROM users u "
            "LEFT JOIN images i ON i.id = u.image_id ORDER BY u.name"
        )

    def _admins_left_without(self, user_id: int) -> int:
        """Count the enabled administrators other than the given user.

        Args:
            user_id: Id of the user to leave out.

        Returns:
            The number of other enabled administrators.
        """
        return self.db.scalar(
            "SELECT count(*) FROM users WHERE is_admin AND NOT disabled AND id <> ?", [user_id]
        )

    def _guard_admin_change(self, actor_id: int | None, user: dict[str, Any], what: str) -> None:
        """Check that a change does not lock an admin out.

        Admins may not lock themselves out, and there must always be an enabled admin.

        Args:
            actor_id: Id of the acting user, or None for the system.
            user: The user being changed.
            what: Verb for the error message, e.g. ``"disable"``.

        Raises:
            ValidationError: When the change targets the actor or the last enabled administrator.
        """
        if actor_id is not None and user["id"] == actor_id:
            raise ValidationError(f"You cannot {what} your own account.")
        if user["is_admin"] and not user["disabled"] and not self._admins_left_without(user["id"]):
            raise ValidationError(f"You cannot {what} the last administrator.")

    def set_disabled(
        self, actor: str, actor_id: int | None, user_id: int, disabled: bool
    ) -> Result:
        """Disable or enable a user.

        Disable: block login, desktop and Samba, end sessions. Enable: reverse (no desktop).

        Args:
            actor: Name recorded in the audit log.
            actor_id: Id of the acting user, or None for the system.
            user_id: Id of the user to change.
            disabled: True to disable, False to enable.

        Returns:
            Warnings about the desktop, file manager or Samba.

        Raises:
            ValidationError: When the user no longer exists or the change would lock an admin out.
        """
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
            except docker.errors.DockerException as exc:
                result.warnings.append(f"The desktop could not be stopped: {exc}")
        else:
            self.db.execute("UPDATE users SET disabled = false WHERE id = ?", [user_id])
            result.warnings += self.ensure_files(user_id)
        try:
            self.samba.set_enabled(user["name"], not disabled)
        except (SambaError, docker.errors.DockerException) as exc:
            result.warnings.append(f"Windows file sharing (Samba) was not updated: {exc}")
        self.db.audit(actor, "user.disable" if disabled else "user.enable", user["name"])
        return result

    def delete_user(
        self, actor: str, actor_id: int | None, user_id: int, delete_home: bool = False
    ) -> Result:
        """Remove the user and everything OrbitalWindow made for them.

        The home folder is kept unless ``delete_home``. UID and MAC are never reused. Audit rows
        are kept.

        Args:
            actor: Name recorded in the audit log.
            actor_id: Id of the acting user, or None for the system.
            user_id: Id of the user to delete.
            delete_home: Also remove the home folder.

        Returns:
            Warnings, including where the home folder was kept.

        Raises:
            ValidationError: When the user no longer exists or the change would lock an admin out.
        """
        user = self._require_user(user_id)
        self._guard_admin_change(actor_id, user, "delete")
        name = user["name"]
        result = Result()
        try:
            self.desktops.stop(user)
            self.files.remove(name)
            self.hostops.remove_files_data(name)
        except docker.errors.DockerException as exc:
            result.warnings.append(f"The desktop or file manager could not be removed: {exc}")
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
        except (SambaError, docker.errors.DockerException) as exc:
            result.warnings.append(f"Windows file sharing (Samba) was not updated: {exc}")
        home = self.settings.users_dir / name
        if delete_home:
            try:
                self.hostops.remove_home(name)
            except docker.errors.DockerException as exc:
                result.warnings.append(f"The home folder {home} could not be removed: {exc}")
        else:
            result.warnings.append(f"The home folder was kept: {home}")
        self.db.audit(actor, "user.delete", name, "home deleted" if delete_home else "home kept")
        return result

    def set_admin(self, actor: str, actor_id: int | None, user_id: int, is_admin: bool) -> None:
        """Grant or remove administrator rights.

        Args:
            actor: Name recorded in the audit log.
            actor_id: Id of the acting user, or None for the system.
            user_id: Id of the user to change.
            is_admin: True to grant, False to remove administrator rights.

        Raises:
            ValidationError: When the user no longer exists or the change would lock an admin out.
        """
        user = self._require_user(user_id)
        if not is_admin:
            self._guard_admin_change(actor_id, user, "remove administrator rights from")
        self.db.execute("UPDATE users SET is_admin = ? WHERE id = ?", [is_admin, user_id])
        self.db.audit(actor, "user.admin" if is_admin else "user.unadmin", user["name"])

    def set_ai_manager(self, actor: str, user_id: int, on: bool) -> None:
        """Grant or remove the AI manager role (manage models and the AI session).

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user to change.
            on: True to grant, False to remove the role.

        Raises:
            ValidationError: When the user no longer exists.
        """
        user = self._require_user(user_id)
        self.db.execute("UPDATE users SET is_ai_manager = ? WHERE id = ?", [on, user_id])
        self.db.audit(actor, "user.ai_manager" if on else "user.not_ai_manager", user["name"])

    # --- AI: models --------------------------------------------------------------------------

    def hf_token(self) -> str | None:
        """Return the stored Hugging Face token.

        Returns:
            The token, or None when none is set.
        """
        try:
            return self.settings.hf_token_path.read_text().strip() or None
        except FileNotFoundError:
            return None

    def set_hf_token(self, actor: str, token: str) -> None:
        """Store or clear the Hugging Face token (a file only the service can read).

        Args:
            actor: Name recorded in the audit log (the token itself is never logged).
            token: The token; empty clears it.

        Raises:
            ValidationError: When it doesn't look like a Hugging Face token.
        """
        token = token.strip()
        path = self.settings.hf_token_path
        if not token:
            path.unlink(missing_ok=True)
            self.db.audit(actor, "ai.hf_token", detail="cleared")
            return
        if not models_mod.TOKEN_RE.fullmatch(token):
            raise ValidationError(
                "A Hugging Face token starts with hf_ followed by letters and digits."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(token + "\n")
        self.db.audit(actor, "ai.hf_token", detail="set")

    def inspect_model(self, repo: str, revision: str = "") -> RepoInfo:
        """Look up a model on Hugging Face.

        Args:
            repo: Model id as typed.
            revision: Branch, tag or commit; empty means ``main``.

        Returns:
            The model's details and the files a download would fetch.

        Raises:
            ValidationError: When the id is not valid or the model can't be found.
        """
        repo = models_mod.validate_repo(repo)
        revision = models_mod.validate_revision(revision)
        return self.hub.inspect(repo, revision, self.hf_token())

    def download_model(self, actor: str, repo: str, revision: str = "") -> int:
        """Inspect a model and queue its download.

        Args:
            actor: Name recorded in the audit log.
            repo: Model id as typed.
            revision: Branch, tag or commit; empty means ``main``.

        Returns:
            The download id.

        Raises:
            ValidationError: When the model can't be found, has no safetensors weights, is
                already being downloaded or doesn't fit on the disk.
        """
        info = self.inspect_model(repo, revision)
        have = models_mod.folder_bytes(self.models.path_for(info.repo))
        needed = max(info.total_bytes - have, 0)
        free = self.models.space().free
        if needed > free:
            raise ValidationError(
                f"{info.repo} needs {needed / 1024**3:.1f} GB but only {free / 1024**3:.1f} GB "
                "is free."
            )
        download_id = self.downloads.queue(info, actor)
        self.db.audit(actor, "ai.download", info.repo, info.revision)
        return download_id

    def cancel_download(self, actor: str, download_id: int) -> None:
        """Cancel a waiting or running download.

        Args:
            actor: Name recorded in the audit log.
            download_id: The download.

        Raises:
            ValidationError: When the download is not waiting or running.
        """
        repo = self.downloads.cancel(download_id)
        self.db.audit(actor, "ai.download.cancel", repo)

    def delete_model(self, actor: str, repo: str) -> None:
        """Delete a downloaded model.

        Args:
            actor: Name recorded in the audit log.
            repo: Model id.

        Raises:
            ValidationError: When the id is not valid or the model is being downloaded.
        """
        repo = models_mod.validate_repo(repo)
        if self.downloads.busy(repo):
            raise ValidationError(f"{repo} is being downloaded. Cancel the download first.")
        state = self.ai.state()
        if state.repo == repo and state.status in ("starting", "running"):
            raise ValidationError(f"{repo} is running in the AI session. Stop the session first.")
        self.models.delete(repo)
        self.db.audit(actor, "ai.model.delete", repo)

    def model_gpu(self) -> fit_mod.Gpu | None:
        """Return the GPU a model would run on: the one with the most memory.

        Returns:
            Its name, total and free memory, or None without NVML (no NVIDIA GPU).
        """
        stats = self.gpus.stats()
        if not stats:
            return None
        best = max(stats, key=lambda gpu: gpu.mem_total)
        return fit_mod.Gpu(best.name, float(best.mem_total), float(best.mem_total - best.mem_used))

    def fit_for_download(self, info: RepoInfo) -> tuple[fit_mod.Estimate, fit_mod.Verdict]:
        """Estimate whether an inspected model fits on the GPU.

        Args:
            info: The inspected model.

        Returns:
            The memory estimate and the verdict for the GPU.
        """
        weights = sum(size for path, size in info.files if path.endswith(".safetensors"))
        config = self.hub.config(info.repo, info.revision, self.hf_token())
        estimate = fit_mod.estimate(weights, config)
        return estimate, fit_mod.verdict(estimate.total, self.model_gpu())

    def fit_for_local(self, model: LocalModel) -> tuple[fit_mod.Estimate, fit_mod.Verdict]:
        """Estimate whether a downloaded model fits on the GPU.

        Args:
            model: The downloaded model.

        Returns:
            The memory estimate and the verdict for the GPU.
        """
        estimate = fit_mod.estimate(model.weights, model.config())
        return estimate, fit_mod.verdict(estimate.total, self.model_gpu())

    def model_suggestions(self) -> list[Suggestion]:
        """Return llmfit's model suggestions for the GPU.

        Returns:
            Suggestions, best first.

        Raises:
            ValidationError: Without GPU information, or when llmfit fails.
        """
        gpu = self.model_gpu()
        if gpu is None:
            raise ValidationError("Suggestions need the GPU's memory, which needs an NVIDIA GPU.")
        try:
            return self.llmfit.suggest(gpu.total)
        except SuggestionError as exc:
            raise ValidationError(str(exc)) from exc

    # --- AI: session ------------------------------------------------------------------------

    def ai_api_key(self) -> str:
        """Return the AI session's API key, creating it on first use.

        Returns:
            The key (``ow-`` and 43 random characters).
        """
        path = self.settings.ai_api_key_path
        try:
            key = path.read_text().strip()
        except FileNotFoundError:
            key = ""
        if not key:
            key = self._write_ai_api_key()
        return key

    def _write_ai_api_key(self) -> str:
        """Generate a new API key and store it (mode 600).

        Returns:
            The new key.
        """
        key = "ow-" + secrets.token_urlsafe(32)
        path = self.settings.ai_api_key_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(key + "\n")
        return key

    def new_ai_api_key(self, actor: str) -> None:
        """Replace the AI session's API key (it takes effect at the next session start).

        Args:
            actor: Name recorded in the audit log (the key itself is never logged).
        """
        self._write_ai_api_key()
        self.db.audit(actor, "ai.api_key", detail="replaced")

    def ai_api_public(self) -> bool:
        """Tell whether the session's API is reachable from outside the server (``/v1``).

        Returns:
            The setting; off by default.
        """
        return self.db.get_setting("ai_api_public") == "true"

    def set_ai_api_public(self, actor: str, public: bool) -> None:
        """Make the session's API reachable from outside the server, or not (next start).

        Args:
            actor: Name recorded in the audit log.
            public: True to route ``/v1`` to the session through Traefik.
        """
        self.db.set_setting("ai_api_public", "true" if public else "false")
        self.db.audit(actor, "ai.api_public", detail="on" if public else "off")

    def ai_api_url(self) -> str:
        """Return the address of the session's OpenAI-compatible API.

        Returns:
            ``https://<host>/v1``.
        """
        return f"https://{self.public_hostname()}/v1"

    def ai_session(self) -> tuple[SessionState, str]:
        """Return the AI session's state and, while it starts, the end of its log.

        Returns:
            The state and the log tail (empty unless starting).
        """
        state = self.ai.state()
        return state, self.ai.log_tail() if state.status == "starting" else ""

    def session_defaults(self, repo: str, context: int = fit_mod.DEFAULT_CONTEXT) -> dict[str, Any]:
        """Suggest session settings for a downloaded model.

        The GPU share covers the estimate with a little room to spare (more cache means more
        people can chat at once), between 30 % and 90 %.

        Args:
            repo: Model id.
            context: Context length to plan for.

        Returns:
            ``context`` (capped at the model's maximum), ``max_context``, ``gpu_share``, the
            ``estimate`` and the ``verdict``.

        Raises:
            ValidationError: If the model is not downloaded.
        """
        model = self._local_model(repo)
        estimate = fit_mod.estimate(model.weights, model.config(), context)
        gpu = self.model_gpu()
        share = 0.9
        if gpu is not None:
            share = min(max(round((estimate.total / gpu.total + 0.1) * 20) / 20, 0.3), 0.9)
        return {
            "context": estimate.context,
            "max_context": estimate.shape.max_context if estimate.shape else None,
            "gpu_share": share,
            "estimate": estimate,
            "verdict": fit_mod.verdict(estimate.total, gpu),
        }

    def _local_model(self, repo: str) -> LocalModel:
        """Return a downloaded model.

        Args:
            repo: Model id.

        Returns:
            The model.

        Raises:
            ValidationError: If it is not downloaded (or still downloading).
        """
        repo = models_mod.validate_repo(repo)
        model = next((m for m in self.models.models() if m.repo == repo), None)
        if model is None or self.downloads.busy(repo):
            raise ValidationError(f"{repo} is not downloaded.")
        return model

    def start_ai_session(
        self, actor: str, repo: str, gpu_index: Any, context: Any, gpu_share: Any
    ) -> int:
        """Start the AI session with a downloaded model.

        Args:
            actor: Name recorded in the audit log.
            repo: Model id.
            gpu_index: NVIDIA GPU number.
            context: Context length in tokens.
            gpu_share: Share of the GPU's memory vLLM may use (0.1–0.95).

        Returns:
            The session id.

        Raises:
            ValidationError: For a model that isn't downloaded, an unknown GPU, values out of range,
                or when a session is already running.
        """
        model = self._local_model(repo)
        gpus = self.gpus.stats()
        if not gpus:
            raise ValidationError("The AI session needs an NVIDIA GPU (NVML is not available).")
        index = _int_in(gpu_index, 0, max(g.index for g in gpus), "GPU")
        if index not in {g.index for g in gpus}:
            raise ValidationError("Unknown GPU.")
        shape = fit_mod.shape_from_config(model.config() or {})
        top = shape.max_context if shape and shape.max_context else 1_048_576
        tokens = _int_in(context, MIN_CONTEXT, top, "Context length")
        try:
            share = round(float(gpu_share), 2)
        except TypeError, ValueError:
            raise ValidationError("The GPU share must be a number.") from None
        if not 0.1 <= share <= 0.95:
            raise ValidationError("The GPU share must be between 10 % and 95 %.")
        request = SessionRequest(model.repo, index, tokens, share, self.ai_api_public())
        session_id = self.ai.start(request, self.ai_api_key(), actor)
        # Open WebUI starts alongside (its image may need pulling: don't hold up the page).
        threading.Thread(target=self.sync_chat, name="ow-chat", daemon=True).start()
        self.db.audit(
            actor, "ai.session.start", model.repo, f"gpu={index} context={tokens} share={share}"
        )
        return session_id

    def stop_ai_session(self, actor: str) -> None:
        """Stop the AI session (frees the GPU memory).

        Args:
            actor: Name recorded in the audit log.
        """
        repo = self.ai.stop()
        self.sync_chat()
        self.db.audit(actor, "ai.session.stop", repo or "")

    def sync_chat(self) -> None:
        """Run the chat (Open WebUI) while the AI session starts or runs; remove it otherwise.

        Problems are logged, not raised: this also runs from the reconcile loop.
        """
        try:
            wanted = self.ai.state().status in ("starting", "running")
            self.chat.ensure(wanted, self.ai_api_key(), self.public_hostname())
        except docker.errors.DockerException as exc:
            log.warning("AI chat: %s", exc)

    def chat_url(self) -> str:
        """Return the address of the chat.

        Returns:
            ``https://<host>:<chat port>/``.
        """
        return f"https://{self.public_hostname()}:{self.settings.chat_port}/"

    def chat_ready(self) -> bool:
        """Tell whether the chat can be opened (Open WebUI is up and the model is running).

        Returns:
            True when both are ready.
        """
        try:
            return self.ai.state().status == "running" and self.chat.ready()
        except docker.errors.DockerException:
            return False

    def model_library(self) -> tuple[list[LocalModel], Space, list[dict[str, Any]]]:
        """Return what the AI page shows: downloaded models, disk space and downloads.

        Returns:
            The models (without those still downloading), the disk space and recent downloads.
        """
        downloads = self.downloads.recent()
        active = {row["repo"] for row in downloads if row["status"] in ("queued", "running")}
        models = [m for m in self.models.models() if m.repo not in active]
        return models, self.models.space(), downloads

    def set_limits(
        self, actor: str, user_id: int, cpus: float | str | None, mem_gb: float | str | None
    ) -> None:
        """Set a user's CPU and RAM limits.

        None/empty/0 means unlimited. They apply at the next start.

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user.
            cpus: CPU limit.
            mem_gb: RAM limit in GB.

        Raises:
            ValidationError: When the user no longer exists or the limits are not valid.
        """
        user = self._require_user(user_id)
        cpus_v, mem_v = _validate_limits(cpus, mem_gb)
        self.db.execute(
            "UPDATE users SET cpus = ?, mem_gb = ? WHERE id = ?", [cpus_v, mem_v, user_id]
        )
        self.db.audit(actor, "user.limits", user["name"], f"cpus={cpus_v} mem_gb={mem_v}")

    def set_quota(self, actor: str, user_id: int, quota_gb: Any) -> None:
        """Set the soft disk quota for a user's home folder (empty = none).

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user.
            quota_gb: Quota in GB, or empty for none.

        Raises:
            ValidationError: When the user no longer exists or the quota is not valid.
        """
        user = self._require_user(user_id)
        quota = _parse_quota(quota_gb)
        self.db.execute("UPDATE users SET quota_gb = ? WHERE id = ?", [quota, user_id])
        self.db.audit(actor, "user.quota", user["name"], str(quota))

    def set_display_name(self, actor: str, user_id: int, display_name: str) -> None:
        """Change a user's display name.

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user.
            display_name: The new name (1–80 characters after trimming).

        Raises:
            ValidationError: When the user no longer exists or the name is empty or too long.
        """
        user = self._require_user(user_id)
        display_name = display_name.strip()
        if not display_name or len(display_name) > 80:
            raise ValidationError("The name must be 1–80 characters.")
        self.db.execute("UPDATE users SET display_name = ? WHERE id = ?", [display_name, user_id])
        self.db.audit(actor, "user.rename", user["name"], display_name)

    # --- file manager ------------------------------------------------------------------------

    def ensure_files(self, user_id: int, *, recreate: bool = False) -> list[str]:
        """Start (or recreate) the user's file manager.

        Returns warnings instead of raising. Does nothing for disabled users.

        Args:
            user_id: Id of the user.
            recreate: Recreate the file manager data and container even if they exist.

        Returns:
            Warnings; empty when all went well.

        Raises:
            ValidationError: When the user no longer exists.
        """
        user = self._require_user(user_id)
        if user["disabled"]:
            return []
        try:
            if recreate or not self._files_exists(user["name"]):
                self.hostops.create_files_data(user["name"], user["uid_gid"])
            self.files.ensure(user, groups_mod.groups_of(self.db, user_id), recreate=recreate)
        except docker.errors.DockerException as exc:
            log.exception("file manager for %s", user["name"])
            return [f"The web file manager could not be started: {exc}"]
        return []

    def _files_exists(self, name: str) -> bool:
        """Return whether the user's file manager container exists.

        Args:
            name: User name.

        Returns:
            True when the container exists.
        """
        try:
            self.client.containers.get(files_container_name(name))
        except docker.errors.NotFound:
            return False
        return True

    # --- SSH keys ----------------------------------------------------------------------------

    def ssh_keys(self, user_id: int) -> list[dict[str, Any]]:
        """Return a user's SSH public keys, oldest first.

        Args:
            user_id: Id of the user.

        Returns:
            Key rows with id, label, fingerprint, public key and when it was added.
        """
        return self.db.all(
            "SELECT id, label, fingerprint, pubkey, added_at FROM ssh_keys "
            "WHERE user_id = ? ORDER BY added_at",
            [user_id],
        )

    def add_ssh_key(self, actor: str, user_id: int, label: str, text: str) -> dict[str, Any]:
        """Add an SSH public key to a user.

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user.
            label: Label for the key.
            text: The public key line as pasted.

        Returns:
            The stored ``label`` and the key's ``fingerprint``.

        Raises:
            ValidationError: When the key or label is not valid, the user has too many keys or
                already has this key.
        """
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
        """Remove one of a user's SSH keys.

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user.
            key_id: Id of the key.

        Raises:
            ValidationError: When the user or the key no longer exists.
        """
        user = self._require_user(user_id)
        row = self.db.one("SELECT * FROM ssh_keys WHERE id = ? AND user_id = ?", [key_id, user_id])
        if row is None:
            raise ValidationError("That key no longer exists.")
        self.db.execute("DELETE FROM ssh_keys WHERE id = ?", [key_id])
        detail = f"{row['label']} {row['fingerprint']}"
        self.db.audit(actor, "ssh.key.remove", user["name"], detail)

    def _ssh_access(self, user: dict[str, Any]) -> SshAccess:
        """Build the SSH gateway settings for starting a user's desktop.

        Args:
            user: The user row.

        Returns:
            The sshpiper labels for the user's keys and the path of the upstream public key.
        """
        private, _ = ssh_mod.ensure_upstream_key(self.settings.state_dir, user["name"])
        keys = [k["pubkey"] for k in self.ssh_keys(user["id"])]
        return SshAccess(
            ssh_mod.sshpiper_labels(user["name"], keys, private),
            self.settings.state_dir / "ssh" / f"{user['name']}.pub",
        )

    def ssh_restart_needed(self, user_id: int) -> bool:
        """Return whether the desktop needs a restart for SSH key changes.

        Args:
            user_id: Id of the user.

        Returns:
            True when the running desktop has other SSH keys than the user has now.

        Raises:
            ValidationError: When the user no longer exists.
        """
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
        """Return the ``ssh`` command a user runs to reach their desktop.

        Args:
            user_name: User name.

        Returns:
            The command line with the SSH port and public host name.
        """
        return f"ssh -p {self.settings.ssh_port} {user_name}@{self.public_hostname()}"

    # --- groups ------------------------------------------------------------------------------

    def list_groups(self) -> list[dict[str, Any]]:
        """Return all groups.

        Returns:
            Group rows.
        """
        return groups_mod.list_groups(self.db)

    def groups_of(self, user_id: int) -> list[dict[str, Any]]:
        """Return the groups a user is a member of.

        Args:
            user_id: Id of the user.

        Returns:
            Group rows.
        """
        return groups_mod.groups_of(self.db, user_id)

    def group_members(self, group_id: int) -> list[dict[str, Any]]:
        """Return the members of a group.

        Args:
            group_id: Id of the group.

        Returns:
            User rows of the members.
        """
        return groups_mod.members(self.db, group_id)

    def create_group(
        self, actor: str, name: str, description: str = "", quota_gb: Any = None
    ) -> Result:
        """Create a group with its shared folder, and update Samba.

        Args:
            actor: Name recorded in the audit log.
            name: Group name.
            description: Free-text description.
            quota_gb: Soft quota in GB, or empty for none.

        Returns:
            Warnings about the folder or Samba.

        Raises:
            ValidationError: When the name or quota is not valid.
        """
        quota = _parse_quota(quota_gb)
        group = groups_mod.insert_group(self.db, name, description, quota)
        self.db.audit(actor, "group.create", group["name"], f"gid={group['gid']}")
        result = Result()
        try:
            self.hostops.create_group_dir(group["name"], group["gid"])
        except docker.errors.DockerException as exc:
            result.warnings.append(f"The group folder could not be created: {exc}")
        result.warnings += self._sync_samba()
        return result

    def set_group_quota(self, actor: str, group_id: int, quota_gb: Any) -> None:
        """Set the soft disk quota of a group folder (empty = none).

        Args:
            actor: Name recorded in the audit log.
            group_id: Id of the group.
            quota_gb: Quota in GB, or empty for none.

        Raises:
            ValidationError: When the group no longer exists or the quota is not valid.
        """
        group = self._require_group(group_id)
        quota = _parse_quota(quota_gb)
        self.db.execute("UPDATE groups SET quota_gb = ? WHERE id = ?", [quota, group_id])
        self.db.audit(actor, "group.quota", group["name"], str(quota))

    def set_group_members(self, actor: str, group_id: int, user_ids: list[int]) -> Result:
        """Replace the member list of a group.

        Samba and file managers follow at once; running desktops see the change after a restart
        (the dashboard says so).

        Args:
            actor: Name recorded in the audit log.
            group_id: Id of the group.
            user_ids: Ids of all users who should be members.

        Returns:
            Warnings about Samba or file managers.

        Raises:
            ValidationError: When the group or some of the users no longer exist.
        """
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
        """Delete a group and update Samba and the members' file managers.

        Args:
            actor: Name recorded in the audit log.
            group_id: Id of the group.
            delete_folder: Also remove the group folder; otherwise it is kept.

        Returns:
            Warnings, including where the folder was kept.

        Raises:
            ValidationError: When the group no longer exists.
        """
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
            except docker.errors.DockerException as exc:
                result.warnings.append(f"The folder {folder} could not be removed: {exc}")
        else:
            result.warnings.append(f"The group folder was kept: {folder}")
        detail = "folder deleted" if delete_folder else "folder kept"
        self.db.audit(actor, "group.delete", group["name"], detail)
        return result

    def group_restart_needed(self, user_id: int) -> bool:
        """Return whether the desktop needs a restart for group changes.

        Args:
            user_id: Id of the user.

        Returns:
            True when the running desktop was started with other groups than the user has now.

        Raises:
            ValidationError: When the user no longer exists.
        """
        user = self._require_user(user_id)
        running = self.desktops.groups_in_use(user["name"])
        if running is None:
            return False
        return running != {g["name"] for g in groups_mod.groups_of(self.db, user_id)}

    def _require_group(self, group_id: int) -> dict[str, Any]:
        """Return a group row or fail.

        Args:
            group_id: Id of the group.

        Returns:
            The group row.

        Raises:
            ValidationError: When the group no longer exists.
        """
        group = groups_mod.get_group(self.db, group_id)
        if group is None:
            raise ValidationError("That group no longer exists.")
        return group

    def _sync_samba(self) -> list[str]:
        """Sync Samba users and shares with the database, turning failures into warnings.

        Returns:
            A warning when Samba was not updated; empty otherwise.
        """
        try:
            self.samba.sync(self.db)
        except (SambaError, docker.errors.DockerException) as exc:
            log.exception("samba sync")
            return [f"Windows file sharing (Samba) was not updated: {exc}"]
        return []

    # --- disk --------------------------------------------------------------------------------

    def disk_usage_for(self, user_id: int) -> DiskUsage:
        """Return the disk usage of a user's home folder.

        Args:
            user_id: Id of the user.

        Returns:
            The last measured usage and quota.

        Raises:
            ValidationError: When the user no longer exists.
        """
        return disk.usage_for_user(self.db, self._require_user(user_id))

    def disk_overview(self) -> tuple[list[DiskUsage], HostSpace]:
        """Return disk usage of all homes and group folders, and free host space.

        Returns:
            The usage rows and the space on the file system holding the data root.
        """
        return disk.all_usage(self.db), disk.host_space(self.settings.root)

    def measure_disk(self) -> int:
        """Measure disk usage of homes, group folders and scratch, and store it.

        Returns:
            The number of folders measured.
        """
        return disk.measure(self.db, self.hostops)

    def clean_scratch(self) -> int:
        """Remove scratch items older than the configured retention.

        Returns:
            The number of items removed.
        """
        days = int(self.db.get_setting("scratch_retention_days") or 30)
        removed = disk.clean_scratch(self.hostops, days)
        detail = f"{removed} item(s), older than {days} days"
        self.db.audit("system", "scratch.clean", detail=detail)
        return removed

    # --- settings ----------------------------------------------------------------------------

    def get_settings(self) -> dict[str, str]:
        """Return all stored settings.

        Returns:
            Setting values by key.
        """
        rows = self.db.all("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in rows}

    def public_hostname(self) -> str:
        """Return the host name users connect to.

        Returns:
            The ``hostname`` setting, or the configured host name when it is not set.
        """
        return self.db.get_setting("hostname") or self.settings.hostname

    def update_settings(self, actor: str, values: dict[str, Any]) -> None:
        """Validate and store admin-editable settings (only the keys given).

        Args:
            actor: Name recorded in the audit log.
            values: New values by setting key.

        Raises:
            ValidationError: When a value is not valid.
        """
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
        """Return audit entries, newest first.

        Args:
            search: Text to look for in actor, action, target or detail; empty for all.
            limit: Maximum number of entries.

        Returns:
            Audit rows.
        """
        like = f"%{search.strip()}%"
        return self.db.all(
            "SELECT * FROM audit WHERE ? = '%%' OR actor ILIKE ? OR action ILIKE ? "
            "OR target ILIKE ? OR detail ILIKE ? ORDER BY ts DESC LIMIT ?",
            [like, like, like, like, like, limit],
        )

    def _managed_ids(self) -> dict[str, str]:
        """Return full container ID → name of every OrbitalWindow container (one Docker call).

        Returns:
            Container names keyed by full container ID.
        """
        containers = self.client.containers.list(filters={"label": "ow.managed=true"}, sparse=True)
        return {c.id: c.attrs.get("Names", [c.id])[0].lstrip("/") for c in containers}

    def usage_all(self) -> dict[str, Usage]:
        """Return CPU/RAM per container name (empty if cAdvisor is unreachable).

        Returns:
            Usage keyed by container name.
        """
        return self.metrics.usage(self._managed_ids())

    def usage_for(self, name: str) -> Usage | None:
        """Return the CPU/RAM use of a user's desktop.

        Args:
            name: User name.

        Returns:
            The usage, or None when the desktop does not exist or has no data.
        """
        try:
            cid = self.client.containers.get(container_name(name)).id
        except docker.errors.NotFound:
            return None
        return self.metrics.usage({cid: container_name(name)}).get(container_name(name))

    def system_overview(self) -> list[dict[str, Any]]:
        """Return one row per user: desktop state, image, CPU/RAM use and limits.

        Returns:
            User rows with ``state``, ``cpu_cores`` and ``mem_bytes`` added.
        """
        states = self.desktops.states()
        usage = self.usage_all()
        rows = []
        for user in self.list_users():
            use = usage.get(container_name(user["name"]))
            rows.append(
                user
                | {
                    "state": states.get(user["name"], DesktopState.STOPPED).value,
                    "cpu_cores": use.cpu_cores if use else None,
                    "mem_bytes": use.mem_bytes if use else None,
                }
            )
        return rows

    # --- desktops ----------------------------------------------------------------------------

    def desktop_state(self, user_id: int) -> DesktopState:
        """Return the state of a user's desktop.

        A stopped desktop is reported as building while its desktop image or the user's software
        is being built for the first time.

        Args:
            user_id: Id of the user.

        Returns:
            The desktop state.

        Raises:
            ValidationError: When the user no longer exists.
        """
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
        """Return whether a per-user image is built and based on the user's current desktop.

        Args:
            user: The user row.
            row: The user's per-user image row, or None.

        Returns:
            True when the per-user image can be used to start the desktop.
        """
        return bool(row and row["built_at"] and row["built_from_image_id"] == user["image_id"])

    def image_ref_for(self, user: dict[str, Any]) -> str:
        """Return the image a start uses.

        The per-user image if the user needs one, else the desktop.

        Args:
            user: The user row.

        Returns:
            The Docker image reference.

        Raises:
            DesktopError: When no usable desktop or per-user image is available yet.
        """
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
        """Return whether the desktop needs a restart for software changes.

        Args:
            user_id: Id of the user.

        Returns:
            True when the running desktop was started with other software than it would get now.

        Raises:
            ValidationError: When the user no longer exists.
        """
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
        """Start a user's desktop with their add-ons, groups and SSH access.

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user.

        Raises:
            ValidationError: When the user no longer exists.
            DesktopError: When the desktop cannot be started.
        """
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
        """Stop a user's desktop.

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user.
            by_admin: Mark the desktop as stopped by an administrator.

        Raises:
            ValidationError: When the user no longer exists.
        """
        user = self._require_user(user_id)
        self.desktops.stop(user)
        if by_admin:
            self.db.execute("UPDATE users SET stopped_by_admin = true WHERE id = ?", [user_id])
        self.db.audit(actor, "desktop.stop", user["name"], "by admin" if by_admin else "")

    def choose_image(self, actor: str, user_id: int, image_id: int) -> None:
        """Switch a user to another desktop image.

        Packages that do not exist for the new desktop are dropped, and a per-user build is
        queued when needed.

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user.
            image_id: Id of the desktop image.

        Raises:
            ValidationError: When the user no longer exists, the desktop is not available or the
                user's desktop is not stopped.
        """
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
        """Remove the user's packages that the desktop's package index does not know.

        Nothing is removed when the desktop has no package index.

        Args:
            user_id: Id of the user.
            image_id: Id of the desktop image.

        Returns:
            Names of the removed packages.
        """
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
        """Return the enabled desktop images, sorted by label.

        Returns:
            Image rows.
        """
        return self.db.all("SELECT * FROM images WHERE enabled ORDER BY label")

    # --- software (add-ons and packages) -----------------------------------------------------

    def set_addon(self, actor: str, user_id: int, key: str, on: bool) -> None:
        """Switch an add-on on or off for a user.

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user.
            key: Add-on key.
            on: True to switch it on.

        Raises:
            ValidationError: When the user no longer exists.
        """
        user = self._require_user(user_id)
        self.software.set_addon(user_id, key, on)
        self.db.audit(actor, "addon.on" if on else "addon.off", user["name"], key)

    def add_package(self, actor: str, user_id: int, name: str) -> str:
        """Add a package to a user's software selection.

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user.
            name: Package name as typed.

        Returns:
            The package name as stored.

        Raises:
            ValidationError: When the user no longer exists.
        """
        user = self._require_user(user_id)
        name = self.software.add_package(user_id, user["image_id"], name)
        self.db.audit(actor, "package.add", user["name"], name)
        return name

    def remove_package(self, actor: str, user_id: int, name: str) -> None:
        """Remove a package from a user's software selection.

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user.
            name: Package name.

        Raises:
            ValidationError: When the user no longer exists.
        """
        user = self._require_user(user_id)
        self.software.remove_package(user_id, name)
        self.db.audit(actor, "package.remove", user["name"], name)

    def software_pending(self, user_id: int) -> bool:
        """Return whether the user's software selection needs a new build.

        Args:
            user_id: Id of the user.

        Returns:
            True when the add-on/package selection differs from the last good per-user image.

        Raises:
            ValidationError: When the user no longer exists.
        """
        user = self._require_user(user_id)
        if user["image_id"] is None:
            return False
        if self.software.plan(user).hash is None:
            return self.software.user_image(user_id) is not None
        return not self.software.up_to_date(user)

    def apply_software(self, actor: str, user_id: int) -> int | None:
        """Queue a per-user build if anything changed.

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user.

        Returns:
            The build id, or None when nothing changed.

        Raises:
            ValidationError: When the user no longer exists or the desktop is still being prepared.
        """
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
        """Return a build row, including its log.

        Args:
            build_id: Id of the build.

        Returns:
            The build row, or None when it does not exist.
        """
        return self.db.one("SELECT * FROM builds WHERE id = ?", [build_id])

    def _rebuild_users_of(self, image_id: int) -> None:
        """After a desktop is rebuilt, rebuild the per-user images built on it.

        Args:
            image_id: Id of the rebuilt desktop image.
        """
        for user in self.db.all("SELECT * FROM users WHERE image_id = ?", [image_id]):
            if self.software.plan(user).hash is not None:
                self.builder.queue_user(user["id"])

    # --- add-on administration ---------------------------------------------------------------

    def reload_addons(self, actor: str) -> list[str]:
        """Reload the add-on catalog and rebuild per-user images that changed.

        Args:
            actor: Name recorded in the audit log.

        Returns:
            One ``key: error`` line per add-on that failed to load.
        """
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
        """Enable or disable an add-on for everyone and rebuild affected images.

        Args:
            actor: Name recorded in the audit log.
            key: Add-on key.
            enabled: True to enable.
        """
        self.db.execute("UPDATE addon_settings SET enabled = ? WHERE key = ?", [enabled, key])
        self.db.audit(actor, "addon.enable" if enabled else "addon.disable", key)
        self._rebuild_stale()

    def set_addon_restricted(self, actor: str, key: str, restricted: bool | None) -> None:
        """Override whether an add-on needs a grant.

        Args:
            actor: Name recorded in the audit log.
            key: Add-on key.
            restricted: True or False to override, None to use the add-on's own setting.
        """
        self.db.execute(
            "UPDATE addon_settings SET restricted_override = ? WHERE key = ?", [restricted, key]
        )
        self.db.audit(actor, "addon.restricted", key, str(restricted))

    def grant_addon(self, actor: str, user_id: int, key: str) -> None:
        """Allow a user to use a restricted add-on.

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user.
            key: Add-on key.

        Raises:
            ValidationError: When the user no longer exists.
        """
        user = self._require_user(user_id)
        self.db.execute(
            "INSERT INTO addon_grants VALUES (?, ?) ON CONFLICT DO NOTHING", [user_id, key]
        )
        self.db.audit(actor, "addon.grant", user["name"], key)

    def revoke_addon(self, actor: str, user_id: int, key: str) -> Result:
        """Revoke a grant: switch the add-on off, and stop the desktop if it uses its mounts.

        Args:
            actor: Name recorded in the audit log.
            user_id: Id of the user.
            key: Add-on key.

        Returns:
            A warning when the desktop was stopped.

        Raises:
            ValidationError: When the user no longer exists.
        """
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
        """Return the restricted add-ons a user has been granted.

        Args:
            user_id: Id of the user.

        Returns:
            Add-on keys.
        """
        rows = self.db.all("SELECT key FROM addon_grants WHERE user_id = ?", [user_id])
        return {r["key"] for r in rows}

    # --- images ------------------------------------------------------------------------------

    def add_image(self, actor: str, base_tag: str, label: str | None = None) -> int:
        """Add a desktop image and queue its build.

        Args:
            actor: Name recorded in the audit log.
            base_tag: Image tag, for example ``ubuntu-xfce``.
            label: Display label; derived from the tag when empty.

        Returns:
            The id of the new image.

        Raises:
            ValidationError: When the tag is not valid or the desktop already exists.
        """
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
        """Return all desktop images, sorted by label.

        Returns:
            Image rows with the number of ``users`` and indexed ``packages`` added.
        """
        return self.db.all(
            "SELECT i.*, (SELECT count(*) FROM users u WHERE u.image_id = i.id) AS users, "
            "(SELECT count(*) FROM pkg_index p WHERE p.image_id = i.id) AS packages "
            "FROM images i ORDER BY i.label"
        )

    def rebuild_image(self, actor: str, image_id: int) -> int:
        """Queue a rebuild of a desktop image.

        Args:
            actor: Name recorded in the audit log.
            image_id: Id of the image.

        Returns:
            The build id.

        Raises:
            ValidationError: When the image no longer exists or is already being built.
        """
        image = self._require_image(image_id)
        if image["status"] == "building":
            raise ValidationError("This desktop is already being built.")
        self.db.audit(actor, "image.rebuild", image["base_tag"])
        return self.builder.queue_base(image_id)

    def set_image_enabled(self, actor: str, image_id: int, enabled: bool) -> None:
        """Enable or disable a desktop image for users to choose.

        Args:
            actor: Name recorded in the audit log.
            image_id: Id of the image.
            enabled: True to enable.

        Raises:
            ValidationError: When the image no longer exists.
        """
        image = self._require_image(image_id)
        self.db.execute("UPDATE images SET enabled = ? WHERE id = ?", [enabled, image_id])
        self.db.audit(actor, "image.enable" if enabled else "image.disable", image["base_tag"])

    def remove_image(self, actor: str, image_id: int) -> None:
        """Remove a desktop image that no user uses, including its Docker image.

        Args:
            actor: Name recorded in the audit log.
            image_id: Id of the image.

        Raises:
            ValidationError: When the image no longer exists, users still use it or it is being
                built.
        """
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
        except docker.errors.APIError as exc:
            log.warning("could not remove %s: %s", image["image_ref"], exc)
        self.db.audit(actor, "image.remove", image["base_tag"])

    def _require_image(self, image_id: int) -> dict[str, Any]:
        """Return an image row or fail.

        Args:
            image_id: Id of the image.

        Returns:
            The image row.

        Raises:
            ValidationError: When the image no longer exists.
        """
        image = self.db.one("SELECT * FROM images WHERE id = ?", [image_id])
        if image is None:
            raise ValidationError("That desktop no longer exists.")
        return image


_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
HOSTNAME_RE = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})*$")


def _parse_quota(value: Any) -> float | None:
    """Parse a quota in GB.

    Args:
        value: Quota as entered; empty for none.

    Returns:
        The quota, or None for none.

    Raises:
        ValidationError: When the value is not a number or is less than 1 GB.
    """
    try:
        quota = users_mod.parse_limit(value)
    except ValueError:
        raise ValidationError("The quota must be a number of GB (empty for none).") from None
    if quota is not None and quota < 1:
        raise ValidationError("The quota must be at least 1 GB (empty for none).")
    return quota


def _int_in(value: Any, low: int, high: int, what: str) -> int:
    """Parse a whole number within a range.

    Args:
        value: Value as entered.
        low: Smallest allowed number.
        high: Largest allowed number.
        what: Name of the value for error messages.

    Returns:
        The number.

    Raises:
        ValidationError: When the value is not a whole number or is out of range.
    """
    try:
        number = int(str(value).strip())
    except ValueError:
        raise ValidationError(f"{what} must be a whole number.") from None
    if not low <= number <= high:
        raise ValidationError(f"{what} must be between {low} and {high}.")
    return number


def _validate_limits(cpus: Any, mem_gb: Any) -> tuple[float | None, float | None]:
    """Parse and check CPU and RAM limits.

    Args:
        cpus: CPU limit; empty for unlimited.
        mem_gb: RAM limit in GB; empty for unlimited.

    Returns:
        The CPU and RAM limits, each None for unlimited.

    Raises:
        ValidationError: When a limit is not a number, CPUs are outside 0.25 to the host's
            count, or RAM is below 2 GB.
    """
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
    """Return a display label for an image tag: 'ubuntu-xfce' → 'Ubuntu XFCE'.

    Args:
        base_tag: Image tag.

    Returns:
        The label.
    """
    words = base_tag.replace("_", "-").split("-")
    return " ".join(w.upper() if len(w) <= 4 and w != "ubuntu" else w.capitalize() for w in words)
