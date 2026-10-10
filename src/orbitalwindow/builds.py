# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Image builds, run one at a time by a background worker. Progress is written to ``builds.log``."""

import logging
import queue
import re
import threading
import time
from pathlib import Path

import docker

from .db import Database

log = logging.getLogger(__name__)

TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
LOG_FLUSH_SECONDS = 1.0
MAX_LOG_CHARS = 400_000


def desktop_image_ref(base_tag: str) -> str:
    return f"ow-desktop:{base_tag}"


class Builder:
    def __init__(
        self, client: docker.DockerClient, db: Database, image_context: Path, webtop_repo: str
    ) -> None:
        self._client = client
        self._db = db
        self._context = image_context
        self._repo = webtop_repo
        self._queue: queue.Queue[int] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        # Builds interrupted by a restart are marked failed; the admin can rebuild.
        self._db.execute(
            "UPDATE builds SET status = 'failed', finished_at = now(), "
            "log = log || '\n[interrupted by a service restart]' "
            "WHERE status IN ('queued', 'running')"
        )
        self._db.execute(
            "UPDATE images SET status = CASE WHEN built_at IS NULL THEN 'failed' ELSE 'ready' END "
            "WHERE status = 'building'"
        )
        self._thread = threading.Thread(target=self._run, name="ow-builder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(0)

    def queue_base(self, image_id: int) -> int:
        image = self._db.one("SELECT * FROM images WHERE id = ?", [image_id])
        if image is None:
            raise ValueError(f"unknown image {image_id}")
        with self._db.transaction():
            self._db.execute("UPDATE images SET status = 'building' WHERE id = ?", [image_id])
            build_id = self._db.scalar(
                "INSERT INTO builds (kind, target, status) "
                "VALUES ('base', ?, 'queued') RETURNING id",
                [str(image_id)],
            )
        self._queue.put(build_id)
        return build_id

    def _run(self) -> None:
        while not self._stop.is_set():
            build_id = self._queue.get()
            if self._stop.is_set():
                return
            try:
                self._build(build_id)
            except Exception:
                log.exception("build %s crashed", build_id)

    def _build(self, build_id: int) -> None:
        build = self._db.one("SELECT * FROM builds WHERE id = ?", [build_id])
        if build is None or build["kind"] != "base":
            return
        image_id = int(build["target"])
        image = self._db.one("SELECT * FROM images WHERE id = ?", [image_id])
        if image is None:
            return
        self._db.execute(
            "UPDATE builds SET status = 'running', started_at = now() WHERE id = ?", [build_id]
        )
        writer = _LogWriter(self._db, build_id)
        ok = False
        try:
            base = f"{self._repo}:{image['base_tag']}"
            writer.write(f"Building {image['image_ref']} from {base}\n")
            for chunk in self._client.api.build(
                path=str(self._context),
                tag=image["image_ref"],
                buildargs={"BASE_IMAGE": base},
                pull=True,
                rm=True,
                forcerm=True,
                decode=True,
                labels={"ow.managed": "true", "ow.role": "desktop-image"},
            ):
                if "stream" in chunk:
                    writer.write(chunk["stream"])
                elif "status" in chunk:
                    # Pull progress is noisy; keep only the lines without a progress bar.
                    if "progress" not in chunk:
                        writer.write(f"{chunk.get('id', '')} {chunk['status']}\n".lstrip())
                elif "error" in chunk:
                    writer.write(f"ERROR: {chunk['error']}\n")
                    raise RuntimeError(chunk["error"])
            ok = True
        except Exception as e:
            writer.write(f"\nBuild failed: {e}\n")
        finally:
            writer.flush()
            status = "ok" if ok else "failed"
            self._db.execute(
                "UPDATE builds SET status = ?, finished_at = now() WHERE id = ?", [status, build_id]
            )
            if ok:
                self._db.execute(
                    "UPDATE images SET status = 'ready', built_at = now() WHERE id = ?", [image_id]
                )
            else:
                # Keep a previously good image usable.
                self._db.execute(
                    "UPDATE images SET status = CASE WHEN built_at IS NULL THEN 'failed' "
                    "ELSE 'ready' END WHERE id = ?",
                    [image_id],
                )


class _LogWriter:
    """Batches log lines into the DB so a chatty build doesn't write on every line."""

    def __init__(self, db: Database, build_id: int) -> None:
        self._db = db
        self._id = build_id
        self._buf: list[str] = []
        self._size = 0
        self._last = time.monotonic()

    def write(self, text: str) -> None:
        if self._size > MAX_LOG_CHARS:
            return
        self._buf.append(text)
        self._size += len(text)
        if self._size > MAX_LOG_CHARS:
            self._buf.append("\n[log truncated]\n")
        if time.monotonic() - self._last >= LOG_FLUSH_SECONDS:
            self.flush()

    def flush(self) -> None:
        if self._buf:
            self._db.execute(
                "UPDATE builds SET log = log || ? WHERE id = ?", ["".join(self._buf), self._id]
            )
            self._buf.clear()
        self._last = time.monotonic()
