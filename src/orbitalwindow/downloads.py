# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Model downloads: one at a time, in a background container, so other work carries on.

The container (``ow-model-dl``) runs ``hf download --local-dir`` as the service's UID into the
model's folder. It keeps running if the service restarts; the worker picks it up again. ``hf``
resumes partial files, so a cancelled or failed download can simply be queued again. Progress is
the size of the model folder against the expected total from the Hub.
"""

import io
import logging
import re
import threading
from collections.abc import Callable
from typing import Any

import docker

from .db import Database
from .models import EXCLUDE, INCLUDE, ModelLibrary, RepoInfo, folder_bytes
from .users import ValidationError

log = logging.getLogger(__name__)

CONTAINER = "ow-model-dl"
HF_IMAGE = "ow-hf:1"  # bump the tag when HF_DOCKERFILE changes
HF_DOCKERFILE = """FROM python:3.13-slim
RUN pip install --no-cache-dir "huggingface_hub[hf_xet]==2.1.1"
LABEL ow.managed="true" ow.role="hf-image"
"""
MOUNT = "/models"
POLL_SECONDS = 2.0
ACTIVE = ("queued", "running", "cancelling")


def download_command(repo: str, revision: str) -> list[str]:
    """Build the ``hf download`` command for a model.

    Args:
        repo: Validated model id.
        revision: Commit to download.

    Returns:
        The argv for the download container.
    """
    cmd = ["hf", "download", repo, "--revision", revision, "--local-dir", f"{MOUNT}/{repo}"]
    for pattern in INCLUDE:
        cmd += ["--include", pattern]
    for pattern in EXCLUDE:
        cmd += ["--exclude", pattern]
    return [*cmd, "--format", "quiet"]


# Terminal colour codes in hf's output.
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _log_tail(text: str, lines: int = 15) -> str:
    """Return the end of a container log without ``hf``'s update hints and colour codes.

    Args:
        text: The container log.
        lines: How many lines to keep.

    Returns:
        The last lines that are not hints.
    """
    kept = [
        line
        for line in ANSI_RE.sub("", text).splitlines()
        if line.strip() and "Hint:" not in line and "hf update" not in line
    ]
    return "\n".join(kept[-lines:])


class Downloader:
    def __init__(
        self,
        client: docker.DockerClient,
        db: Database,
        library: ModelLibrary,
        uid: int,
        token: Callable[[], str | None],
    ) -> None:
        """Create the downloader; call ``start`` to run the worker.

        Args:
            client: Docker client.
            db: Service database.
            library: Where the models go (host path; the service sees the same path).
            uid: UID the download runs as (the service's own, so it can delete models).
            token: Returns the Hugging Face token, or None.
        """
        self._client = client
        self._db = db
        self._library = library
        self._uid = uid
        self._token = token
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._image_ready = False

    # --- requests ----------------------------------------------------------------------------

    def queue(self, info: RepoInfo, actor: str) -> int:
        """Queue a download of an inspected model.

        Args:
            info: The model's details from ``HubClient.inspect`` (pins the commit).
            actor: Who asked for it.

        Returns:
            The download id.

        Raises:
            ValidationError: If the model has no safetensors weights or is already being downloaded.
        """
        if not info.has_safetensors:
            raise ValidationError(
                f"{info.repo} has no weights in safetensors format, which the AI session needs."
            )
        with self._db.transaction():
            if self._db.scalar(
                "SELECT count(*) FROM model_downloads WHERE repo = ? AND status IN "
                "('queued', 'running', 'cancelling')",
                [info.repo],
            ):
                raise ValidationError(f"{info.repo} is already being downloaded.")
            download_id = self._db.scalar(
                "INSERT INTO model_downloads (repo, revision, status, bytes_total, requested_by) "
                "VALUES (?, ?, 'queued', ?, ?) RETURNING id",
                [info.repo, info.revision, info.total_bytes, actor],
            )
        self._wake.set()
        return download_id

    def cancel(self, download_id: int) -> str:
        """Cancel a queued or running download (the files so far are kept for a later resume).

        Args:
            download_id: The download.

        Returns:
            The model id.

        Raises:
            ValidationError: If the download is not queued or running.
        """
        with self._db.transaction():
            row = self._db.one("SELECT * FROM model_downloads WHERE id = ?", [download_id])
            if row is None or row["status"] not in ("queued", "running"):
                raise ValidationError("That download is not waiting or running.")
            new = "cancelled" if row["status"] == "queued" else "cancelling"
            self._db.execute(
                "UPDATE model_downloads SET status = ?, finished_at = "
                "CASE WHEN ? = 'cancelled' THEN now() ELSE NULL END WHERE id = ?",
                [new, new, download_id],
            )
        self._wake.set()
        return row["repo"]

    def busy(self, repo: str) -> bool:
        """Tell whether a model is queued or downloading.

        Args:
            repo: Model id.

        Returns:
            True while a download of it is active.
        """
        return bool(
            self._db.scalar(
                "SELECT count(*) FROM model_downloads WHERE repo = ? AND status IN "
                "('queued', 'running', 'cancelling')",
                [repo],
            )
        )

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the active downloads and the latest finished ones.

        Args:
            limit: Maximum number of rows.

        Returns:
            Download rows, active ones first, then newest first.
        """
        return self._db.all(
            "SELECT * FROM model_downloads ORDER BY "
            "status IN ('queued', 'running', 'cancelling') DESC, id DESC LIMIT ?",
            [limit],
        )

    # --- worker ------------------------------------------------------------------------------

    def start(self) -> None:
        """Start the worker thread (it also adopts a download left running by a restart)."""
        self._thread = threading.Thread(target=self._run, name="ow-downloads", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the worker thread to stop; a running download container keeps going."""
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        """Process downloads one at a time until stopped (the worker thread)."""
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("model download worker failed")
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    def step(self) -> None:
        """Do one round of work: follow the running download, or start the next queued one."""
        container = self._container()
        running = self._db.one(
            "SELECT * FROM model_downloads WHERE status IN ('running', 'cancelling') "
            "ORDER BY id LIMIT 1"
        )
        if container is not None:
            self._follow(container, running)
            return
        if running is not None:
            # Its container is gone (removed by hand or with the host): run it again; hf resumes.
            self._db.execute(
                "UPDATE model_downloads SET status = CASE WHEN status = 'cancelling' "
                "THEN 'cancelled' ELSE 'queued' END WHERE id = ?",
                [running["id"]],
            )
            return
        queued = self._db.one(
            "SELECT * FROM model_downloads WHERE status = 'queued' ORDER BY id LIMIT 1"
        )
        if queued is not None:
            self._launch(queued)

    def _container(self) -> Any | None:
        """Return the download container, if there is one.

        Returns:
            The container, or None.
        """
        try:
            return self._client.containers.get(CONTAINER)
        except docker.errors.NotFound:
            return None

    def _follow(self, container: Any, row: dict[str, Any] | None) -> None:
        """Update progress of the running download; finish it when its container has exited.

        Args:
            container: The download container.
            row: The download it belongs to (None if the database lost track of it).
        """
        if row is None or str(row["id"]) != container.labels.get("ow.download"):
            log.warning("removing stray model download container")
            container.remove(force=True)
            return
        path = self._library.path_for(row["repo"])
        if row["status"] == "cancelling":
            container.remove(force=True)
            self._db.execute(
                "UPDATE model_downloads SET status = 'cancelled', bytes_done = ?, "
                "finished_at = now() WHERE id = ?",
                [folder_bytes(path), row["id"]],
            )
            return
        container.reload()
        if container.status in ("created", "running"):
            done = min(folder_bytes(path), row["bytes_total"])
            self._db.execute(
                "UPDATE model_downloads SET bytes_done = ? WHERE id = ?", [done, row["id"]]
            )
            return
        code = container.attrs.get("State", {}).get("ExitCode", -1)
        output = container.logs(tail=60).decode(errors="replace")
        container.remove(force=True)
        if code == 0:
            self._db.execute(
                "UPDATE model_downloads SET status = 'done', bytes_done = bytes_total, "
                "finished_at = now() WHERE id = ?",
                [row["id"]],
            )
            log.info("downloaded %s", row["repo"])
        else:
            self._db.execute(
                "UPDATE model_downloads SET status = 'failed', error = ?, bytes_done = ?, "
                "finished_at = now() WHERE id = ?",
                [_log_tail(output) or f"exit code {code}", folder_bytes(path), row["id"]],
            )
            log.warning("download of %s failed (exit %s)", row["repo"], code)

    def _launch(self, row: dict[str, Any]) -> None:
        """Start the container for a queued download.

        Args:
            row: The download.
        """
        try:
            self._ensure_image()
        except docker.errors.DockerException as exc:
            self._db.execute(
                "UPDATE model_downloads SET status = 'failed', error = ?, finished_at = now() "
                "WHERE id = ?",
                [f"Could not prepare the download tool: {exc}", row["id"]],
            )
            return
        # The hf version is pinned in the image: no update check (its notice ends up in errors).
        environment = {
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_DISABLE_UPDATE_CHECK": "1",
            "HOME": "/tmp",
        }
        token = self._token()
        if token:
            environment["HF_TOKEN"] = token
        self._client.containers.run(
            HF_IMAGE,
            download_command(row["repo"], row["revision"]),
            name=CONTAINER,
            detach=True,
            user=f"{self._uid}:{self._uid}",
            environment=environment,
            volumes={str(self._library.root): {"bind": MOUNT, "mode": "rw"}},
            labels={
                "ow.managed": "true",
                "ow.role": "model-download",
                "ow.download": str(row["id"]),
            },
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            pids_limit=256,
        )
        self._db.execute(
            "UPDATE model_downloads SET status = 'running', started_at = now(), error = '' "
            "WHERE id = ?",
            [row["id"]],
        )
        log.info("downloading %s @ %s", row["repo"], row["revision"])

    def _ensure_image(self) -> None:
        """Build the download tool image unless it already exists."""
        if self._image_ready:
            return
        try:
            self._client.images.get(HF_IMAGE)
        except docker.errors.ImageNotFound:
            log.info("building %s", HF_IMAGE)
            for _ in self._client.api.build(
                fileobj=io.BytesIO(HF_DOCKERFILE.encode()),
                tag=HF_IMAGE,
                rm=True,
                pull=True,
                decode=True,
            ):
                pass
            self._client.images.get(HF_IMAGE)  # raises if the build failed
        self._image_ready = True
