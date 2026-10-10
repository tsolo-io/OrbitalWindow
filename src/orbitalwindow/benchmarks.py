# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Coding benchmarks (EvalPlus HumanEval+ and MBPP+) against the AI session.

One benchmark runs at a time, in three steps:
1. *starting*: the model is started in the AI session if it isn't running.
2. *generating*: a container on the session network asks the model for one solution per task
   (``bench/runner.py``, EvalPlus's own prompt; progress in ``progress.json``).
3. *evaluating*: ``evalplus.evaluate`` runs the solutions against the tests in a container with no
   network, no capabilities and memory and process limits: the code is written by the model.

Each benchmark keeps its files in ``$OW_ROOT/state/benchmarks/<id>/`` and its scores in the
database. Containers keep running across service restarts; the worker picks them up again.
"""

import io
import json
import logging
import tarfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import docker

from . import aisession
from .db import Database
from .users import ValidationError

log = logging.getLogger(__name__)

BENCH_IMAGE = "ow-bench:1"  # bump the tag when the Dockerfile or runner.py change
BENCH_DOCKERFILE = """FROM python:3.11-slim
ENV XDG_CACHE_HOME=/opt/cache PIP_NO_CACHE_DIR=1
RUN pip install "evalplus==0.3.1"
# The datasets are baked in: benchmarks run without internet.
RUN python -c "from evalplus.data import get_human_eval_plus, get_mbpp_plus; \\
get_human_eval_plus(); get_mbpp_plus()" && chmod -R a+rwX /opt/cache
COPY runner.py /opt/bench/runner.py
LABEL ow.managed="true" ow.role="bench-image"
"""
GENERATE = "ow-bench-generate"
EVALUATE = "ow-bench-evaluate"
MOUNT = "/out"
POLL_SECONDS = 3.0
START_TIMEOUT_SECONDS = 45 * 60


@dataclass(frozen=True)
class Suite:
    key: str
    label: str
    tasks: int


SUITES = {
    "humaneval": Suite("humaneval", "HumanEval+", 164),
    "mbpp": Suite("mbpp", "MBPP+", 378),
}
FILES = ("samples.jsonl", "raw.jsonl", "samples_eval_results.json")
ACTIVE = ("queued", "starting", "generating", "evaluating", "cancelling")


def scores(results: dict[str, Any]) -> tuple[int, int, int]:
    """Count passed tasks in EvalPlus's ``*_eval_results.json``.

    Args:
        results: The parsed file.

    Returns:
        Tasks, tasks passing the base tests, tasks passing base and extra ("plus") tests.
    """
    tasks = base = plus = 0
    for entries in results.get("eval", {}).values():
        if not entries:
            continue
        tasks += 1
        first = entries[0]
        if first.get("base_status") == "pass":
            base += 1
            if first.get("plus_status") == "pass":
                plus += 1
    return tasks, base, plus


def task_results(folder: Path) -> list[dict[str, Any]]:
    """Return the per-task results of a finished benchmark for its report.

    Args:
        folder: The benchmark's folder.

    Returns:
        One entry per task: id, base and plus status, and the extracted solution.
    """
    try:
        results = json.loads((folder / "samples_eval_results.json").read_text())
    except OSError, ValueError:
        return []
    rows = []
    for task_id, entries in results.get("eval", {}).items():
        first = entries[0] if entries else {}
        rows.append(
            {
                "task_id": task_id,
                "base": first.get("base_status", "–"),
                "plus": first.get("plus_status", "–"),
                "solution": first.get("solution", ""),
            }
        )
    return sorted(rows, key=lambda row: int(row["task_id"].rsplit("/", 1)[-1]))


class Benchmarker:
    def __init__(
        self,
        client: docker.DockerClient,
        db: Database,
        root: Path,
        uid: int,
        session: Callable[[], aisession.SessionState],
        start_session: Callable[[str], None],
        api_key: Callable[[], str],
    ) -> None:
        """Create the benchmark worker; call ``start`` to run it.

        Args:
            client: Docker client.
            db: Service database.
            root: Folder for the benchmark files (``$OW_ROOT/state/benchmarks``).
            uid: UID the containers run as (the service's own, so it can read the files).
            session: Returns the AI session's state.
            start_session: Starts the AI session with a model (suggested settings).
            api_key: Returns the AI session's API key.
        """
        self._client = client
        self._db = db
        self.root = root
        self._uid = uid
        self._session = session
        self._start_session = start_session
        self._api_key = api_key
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._image_ready = False

    # --- requests ----------------------------------------------------------------------------

    def queue(self, repo: str, suite: str, actor: str) -> int:
        """Queue a benchmark of a downloaded model.

        Args:
            repo: Model id.
            suite: ``humaneval`` or ``mbpp``.
            actor: Who asked for it.

        Returns:
            The benchmark id.

        Raises:
            ValidationError: For an unknown suite, or when this benchmark is already waiting.
        """
        if suite not in SUITES:
            raise ValidationError("Unknown benchmark.")
        with self._db.transaction():
            if self._db.scalar(
                "SELECT count(*) FROM benchmarks WHERE repo = ? AND suite = ? AND status IN "
                "('queued', 'starting', 'generating', 'evaluating', 'cancelling')",
                [repo, suite],
            ):
                raise ValidationError(
                    f"{SUITES[suite].label} of {repo} is already waiting or running."
                )
            bench_id = self._db.scalar(
                "INSERT INTO benchmarks (repo, suite, status, total, started_by) "
                "VALUES (?, ?, 'queued', ?, ?) RETURNING id",
                [repo, suite, SUITES[suite].tasks, actor],
            )
        self._wake.set()
        return bench_id

    def cancel(self, bench_id: int) -> str:
        """Cancel a waiting or running benchmark.

        Args:
            bench_id: The benchmark.

        Returns:
            The model id.

        Raises:
            ValidationError: If it isn't waiting or running.
        """
        with self._db.transaction():
            row = self._db.one("SELECT * FROM benchmarks WHERE id = ?", [bench_id])
            if row is None or row["status"] not in ACTIVE[:-1]:
                raise ValidationError("That benchmark is not waiting or running.")
            new = "cancelled" if row["status"] == "queued" else "cancelling"
            self._db.execute(
                "UPDATE benchmarks SET status = ?, finished_at = "
                "CASE WHEN ? = 'cancelled' THEN now() ELSE NULL END WHERE id = ?",
                [new, new, bench_id],
            )
        self._wake.set()
        return row["repo"]

    def folder(self, bench_id: int) -> Path:
        """Return a benchmark's folder.

        Args:
            bench_id: The benchmark.

        Returns:
            ``<root>/<id>``.
        """
        return self.root / str(bench_id)

    # --- worker ------------------------------------------------------------------------------

    def start(self) -> None:
        """Start the worker thread."""
        threading.Thread(target=self._run, name="ow-benchmarks", daemon=True).start()

    def stop(self) -> None:
        """Ask the worker to stop; running containers keep going."""
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        """Process benchmarks one at a time until stopped (the worker thread)."""
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("benchmark worker failed")
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    def step(self) -> None:
        """Do one round of work on the current benchmark, or start the next queued one."""
        row = self._db.one(
            "SELECT * FROM benchmarks WHERE status IN "
            "('starting', 'generating', 'evaluating', 'cancelling') ORDER BY id LIMIT 1"
        ) or self._db.one("SELECT * FROM benchmarks WHERE status = 'queued' ORDER BY id LIMIT 1")
        if row is None:
            return
        if row["status"] == "cancelling":
            self._remove_containers()
            self._finish(row["id"], "cancelled")
        elif row["status"] == "queued":
            self._set(row["id"], status="starting", started_at="now")
            self._starting(row | {"status": "starting"})
        elif row["status"] == "starting":
            self._starting(row)
        elif row["status"] == "generating":
            self._generating(row)
        else:
            self._evaluating(row)

    def _starting(self, row: dict[str, Any]) -> None:
        """Make sure the model runs in the AI session, then start generating.

        Args:
            row: The benchmark.
        """
        state = self._session()
        if state.status == "running" and state.repo == row["repo"]:
            self._set(
                row["id"],
                gpu_index=state.gpu_index,
                context=state.context,
                gpu_share=state.gpu_share,
            )
            try:
                self._launch_generate(row)
            except docker.errors.DockerException as exc:
                self._fail(row["id"], f"The benchmark could not start: {exc}")
            return
        if state.status == "starting" and state.repo == row["repo"]:
            if self._seconds_since_start(row) > START_TIMEOUT_SECONDS:
                self._fail(row["id"], "The model didn't become ready in time.")
            return
        if state.status in ("starting", "running"):
            self._fail(
                row["id"],
                f"Another model ({state.repo}) is running. Stop it, or start the benchmark again "
                "and let it replace that model.",
            )
            return
        if state.status == "failed" and state.repo == row["repo"]:
            self._fail(row["id"], f"The model couldn't start: {state.error[-500:]}")
            return
        try:
            self._start_session(row["repo"])
        except ValidationError as exc:
            self._fail(row["id"], f"The model couldn't start: {exc}")

    def _seconds_since_start(self, row: dict[str, Any]) -> float:
        """Return how long ago a benchmark started.

        Args:
            row: The benchmark.

        Returns:
            Seconds since ``started_at``.
        """
        return float(
            self._db.scalar(
                "SELECT epoch(now()) - epoch(started_at) FROM benchmarks WHERE id = ?", [row["id"]]
            )
            or 0
        )

    def _launch_generate(self, row: dict[str, Any]) -> None:
        """Start the generating container.

        Args:
            row: The benchmark.
        """
        folder = self.folder(row["id"])
        folder.mkdir(parents=True, exist_ok=True)
        self._ensure_image()
        base_url = f"http://{aisession.CONTAINER}:{aisession.PORT}/v1"
        self._client.containers.run(
            BENCH_IMAGE,
            ["python", "/opt/bench/runner.py", row["suite"], row["repo"], base_url, MOUNT],
            name=GENERATE,
            detach=True,
            user=f"{self._uid}:{self._uid}",
            environment={"OPENAI_API_KEY": self._api_key(), "HOME": "/tmp"},
            volumes={str(folder): {"bind": MOUNT, "mode": "rw"}},
            network=aisession.NETWORK,
            labels={"ow.managed": "true", "ow.role": "bench", "ow.bench": str(row["id"])},
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            pids_limit=256,
            mem_limit="2g",
        )
        self._set(row["id"], status="generating")

    def _generating(self, row: dict[str, Any]) -> None:
        """Follow generation; when it is done, start evaluating.

        Args:
            row: The benchmark.
        """
        container = self._container(GENERATE)
        folder = self.folder(row["id"])
        if container is None:
            self._fail(row["id"], "The generating step stopped unexpectedly.")
            return
        try:
            progress = json.loads((folder / "progress.json").read_text())
            self._set(row["id"], done=int(progress.get("done", 0)))
        except OSError, ValueError:
            pass
        container.reload()
        if container.status in ("created", "running"):
            return
        code = container.attrs.get("State", {}).get("ExitCode", -1)
        output = container.logs(tail=40).decode(errors="replace")
        container.remove(force=True)
        if code != 0 or not (folder / "samples.jsonl").exists():
            self._fail(row["id"], output.strip()[-1500:] or f"exit code {code}")
            return
        self._client.containers.run(
            BENCH_IMAGE,
            ["evalplus.evaluate", "--dataset", row["suite"], "--samples", f"{MOUNT}/samples.jsonl"],
            name=EVALUATE,
            detach=True,
            user=f"{self._uid}:{self._uid}",
            environment={"HOME": "/tmp"},
            volumes={str(folder): {"bind": MOUNT, "mode": "rw"}},
            network_mode="none",
            labels={"ow.managed": "true", "ow.role": "bench", "ow.bench": str(row["id"])},
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            pids_limit=1024,
            mem_limit="8g",
        )
        self._set(row["id"], status="evaluating", done=row["total"])

    def _evaluating(self, row: dict[str, Any]) -> None:
        """Follow evaluation; when it is done, store the scores.

        Args:
            row: The benchmark.
        """
        container = self._container(EVALUATE)
        if container is None:
            self._fail(row["id"], "The evaluating step stopped unexpectedly.")
            return
        container.reload()
        if container.status in ("created", "running"):
            return
        code = container.attrs.get("State", {}).get("ExitCode", -1)
        output = container.logs(tail=40).decode(errors="replace")
        container.remove(force=True)
        try:
            results = json.loads((self.folder(row["id"]) / "samples_eval_results.json").read_text())
        except OSError, ValueError:
            self._fail(row["id"], output.strip()[-1500:] or f"exit code {code}")
            return
        tasks, base, plus = scores(results)
        self._set(
            row["id"],
            tasks=tasks,
            base_pass=base,
            plus_pass=plus,
            base_score=base / tasks if tasks else 0.0,
            plus_score=plus / tasks if tasks else 0.0,
        )
        self._finish(row["id"], "done")
        log.info("benchmark %s of %s: %s/%s", row["suite"], row["repo"], plus, tasks)

    # --- helpers -----------------------------------------------------------------------------

    def _container(self, name: str) -> Any | None:
        """Return a benchmark container, if there is one.

        Args:
            name: Container name.

        Returns:
            The container, or None.
        """
        try:
            return self._client.containers.get(name)
        except docker.errors.NotFound:
            return None

    def _remove_containers(self) -> None:
        """Remove the generating and evaluating containers."""
        for name in (GENERATE, EVALUATE):
            container = self._container(name)
            if container is not None:
                container.remove(force=True)

    def _set(self, bench_id: int, **values: Any) -> None:
        """Update columns of a benchmark.

        Args:
            bench_id: The benchmark.
            **values: Column values; the string ``"now"`` for a timestamp column means now.
        """
        columns = ", ".join(
            f"{name} = now()" if value == "now" else f"{name} = ?" for name, value in values.items()
        )
        params = [value for value in values.values() if value != "now"]
        self._db.execute(f"UPDATE benchmarks SET {columns} WHERE id = ?", [*params, bench_id])

    def _finish(self, bench_id: int, status: str) -> None:
        """Mark a benchmark as finished.

        Args:
            bench_id: The benchmark.
            status: ``done``, ``failed`` or ``cancelled``.
        """
        self._set(bench_id, status=status, finished_at="now")

    def _fail(self, bench_id: int, error: str) -> None:
        """Mark a benchmark as failed and remove its containers.

        Args:
            bench_id: The benchmark.
            error: Why it failed.
        """
        self._remove_containers()
        self._set(bench_id, error=error)
        self._finish(bench_id, "failed")
        log.warning("benchmark %s failed: %s", bench_id, error[:200])

    def _ensure_image(self) -> None:
        """Build the benchmark image unless it exists (Dockerfile and runner.py as context)."""
        if self._image_ready:
            return
        try:
            self._client.images.get(BENCH_IMAGE)
        except docker.errors.ImageNotFound:
            log.info("building %s", BENCH_IMAGE)
            context = io.BytesIO()
            runner = resources.files(__package__).joinpath("bench/runner.py").read_bytes()
            files = {"Dockerfile": BENCH_DOCKERFILE.encode(), "runner.py": runner}
            with tarfile.open(fileobj=context, mode="w") as tar:
                for name, data in files.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            context.seek(0)
            for _ in self._client.api.build(
                fileobj=context,
                custom_context=True,
                tag=BENCH_IMAGE,
                rm=True,
                pull=True,
                decode=True,
            ):
                pass
            self._client.images.get(BENCH_IMAGE)  # raises if the build failed
        self._image_ready = True
