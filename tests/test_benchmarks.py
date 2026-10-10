# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import json
from types import SimpleNamespace

import docker
import pytest
from conftest import ORIGIN, login

from orbitalwindow.aisession import SessionState
from orbitalwindow.benchmarks import EVALUATE, GENERATE, Benchmarker, scores, task_results
from orbitalwindow.users import ValidationError

RESULTS = {
    "eval": {
        "HumanEval/0": [{"base_status": "pass", "plus_status": "pass", "solution": "def a(): 1"}],
        "HumanEval/1": [{"base_status": "pass", "plus_status": "fail", "solution": "def b(): 2"}],
        "HumanEval/10": [{"base_status": "fail", "plus_status": "fail", "solution": "x"}],
        "HumanEval/2": [{"base_status": "pass", "plus_status": "pass", "solution": "def c(): 3"}],
    }
}


def test_scores_count_base_and_plus():
    """Plus counts only tasks that pass both the base and the extra tests."""
    assert scores(RESULTS) == (4, 3, 2)


def test_task_results_sorted_by_number(tmp_path):
    """The report lists tasks in numeric order with their status and code."""
    (tmp_path / "samples_eval_results.json").write_text(json.dumps(RESULTS))
    rows = task_results(tmp_path)
    assert [row["task_id"] for row in rows] == [
        "HumanEval/0",
        "HumanEval/1",
        "HumanEval/2",
        "HumanEval/10",
    ]
    assert rows[1]["base"] == "pass" and rows[1]["plus"] == "fail"
    assert task_results(tmp_path / "missing") == []


class FakeContainer:
    def __init__(self, name, kwargs):
        """Remember the run arguments; start running."""
        self.name, self.kwargs, self.status, self.removed = name, kwargs, "running", False
        self.attrs = {"State": {"ExitCode": 0}}
        self.log = b""

    def reload(self):
        """Keep the scripted state."""

    def logs(self, tail=40):
        """Return the scripted log."""
        return self.log

    def remove(self, force=False):
        """Mark as removed."""
        self.removed = True


class FakeContainers:
    def __init__(self):
        """Start with no containers."""
        self.by_name, self.runs = {}, []

    def get(self, name):
        """Return a live container or raise NotFound."""
        container = self.by_name.get(name)
        if container is None or container.removed:
            raise docker.errors.NotFound(name)
        return container

    def run(self, image, command, **kwargs):
        """Record a run."""
        container = FakeContainer(kwargs["name"], kwargs | {"command": command, "image": image})
        self.by_name[kwargs["name"]] = container
        self.runs.append(container)
        return container


class FakeSession:
    def __init__(self):
        """Start with no session."""
        self.state = SessionState("stopped")
        self.started = []

    def start(self, repo):
        """Record a start and report the model as starting."""
        self.started.append(repo)
        self.state = SessionState("starting", repo=repo)


@pytest.fixture
def bench(service, tmp_path):
    """A Benchmarker with fake Docker and a fake session."""
    session = FakeSession()
    images = SimpleNamespace(get=lambda image: None)
    client = SimpleNamespace(containers=FakeContainers(), images=images)
    worker = Benchmarker(
        client,
        service.db,
        tmp_path,
        10001,
        session=lambda: session.state,
        start_session=session.start,
        api_key=lambda: "ow-key",
    )
    worker.fake_session = session
    return worker


def running_state(repo):
    """Return the state of a session running ``repo``."""
    return SessionState("running", repo=repo, gpu_index=0, context=1, gpu_share=0.5)


def status(bench, bench_id):
    """Return the stored status of a benchmark."""
    return bench._db.scalar("SELECT status FROM benchmarks WHERE id = ?", [bench_id])


def test_queue_checks(bench):
    """Unknown suites and duplicates are refused."""
    with pytest.raises(ValidationError, match="Unknown"):
        bench.queue("Qwen/Qwen3-0.6B", "gsm8k", "boss")
    bench.queue("Qwen/Qwen3-0.6B", "humaneval", "boss")
    with pytest.raises(ValidationError, match="already"):
        bench.queue("Qwen/Qwen3-0.6B", "humaneval", "boss")
    bench.queue("Qwen/Qwen3-0.6B", "mbpp", "boss")  # another suite is fine


def test_full_run(bench, tmp_path):
    """Start the model, generate on the session network, evaluate offline, store the scores."""
    bench_id = bench.queue("Qwen/Qwen3-0.6B", "humaneval", "boss")
    bench.step()
    assert bench.fake_session.started == ["Qwen/Qwen3-0.6B"]
    assert status(bench, bench_id) == "starting"
    bench.step()  # still loading
    assert not bench._client.containers.runs

    bench.fake_session.state = SessionState(
        "running", repo="Qwen/Qwen3-0.6B", gpu_index=0, context=8192, gpu_share=0.35
    )
    bench.step()
    generate = bench._client.containers.by_name[GENERATE]
    assert generate.kwargs["network"] == "ow-ai" and generate.kwargs["user"] == "10001:10001"
    assert generate.kwargs["environment"]["OPENAI_API_KEY"] == "ow-key"
    assert generate.kwargs["command"][2:4] == ["humaneval", "Qwen/Qwen3-0.6B"]
    assert generate.kwargs["cap_drop"] == ["ALL"]
    assert status(bench, bench_id) == "generating"

    folder = tmp_path / str(bench_id)
    (folder / "progress.json").write_text('{"phase": "generate", "done": 40, "total": 164}')
    bench.step()
    assert bench._db.scalar("SELECT done FROM benchmarks WHERE id = ?", [bench_id]) == 40

    (folder / "samples.jsonl").write_text("{}\n")
    generate.status = "exited"
    bench.step()
    evaluate = bench._client.containers.by_name[EVALUATE]
    assert evaluate.kwargs["network_mode"] == "none" and evaluate.kwargs["cap_drop"] == ["ALL"]
    assert evaluate.kwargs["pids_limit"] and evaluate.kwargs["mem_limit"]
    assert status(bench, bench_id) == "evaluating" and generate.removed

    (folder / "samples_eval_results.json").write_text(json.dumps(RESULTS))
    evaluate.status = "exited"
    bench.step()
    row = bench._db.one("SELECT * FROM benchmarks WHERE id = ?", [bench_id])
    assert row["status"] == "done"
    assert (row["tasks"], row["base_pass"], row["plus_pass"]) == (4, 3, 2)
    assert row["plus_score"] == 0.5 and row["gpu_share"] == 0.35 and row["finished_at"]


def test_failures(bench, tmp_path):
    """A failing step records its log; another running model or a failed start fail the run."""
    first = bench.queue("a/model", "humaneval", "boss")
    bench.fake_session.state = running_state("a/model")
    bench.step()
    generate = bench._client.containers.by_name[GENERATE]
    generate.status, generate.attrs["State"]["ExitCode"] = "exited", 1
    generate.log = b"openai.APIConnectionError: Connection error."
    bench.step()
    row = bench._db.one("SELECT * FROM benchmarks WHERE id = ?", [first])
    assert row["status"] == "failed" and "Connection error" in row["error"]

    second = bench.queue("b/model", "humaneval", "boss")
    bench.step()
    assert status(bench, second) == "failed"
    error = bench._db.scalar("SELECT error FROM benchmarks WHERE id = ?", [second])
    assert "Another model" in error

    bench.fake_session.state = SessionState("failed", repo="c/model", error="CUDA out of memory")
    third = bench.queue("c/model", "humaneval", "boss")
    bench.step()
    assert "out of memory" in bench._db.scalar("SELECT error FROM benchmarks WHERE id = ?", [third])


def test_cancel(bench):
    """Waiting benchmarks are cancelled at once; running ones after their containers are removed."""
    bench.fake_session.state = running_state("a/m")
    running = bench.queue("a/m", "humaneval", "boss")
    waiting = bench.queue("a/m", "mbpp", "boss")
    bench.step()
    bench.cancel(waiting)
    assert status(bench, waiting) == "cancelled"
    bench.cancel(running)
    bench.step()
    assert status(bench, running) == "cancelled"
    assert bench._client.containers.by_name[GENERATE].removed
    with pytest.raises(ValidationError):
        bench.cancel(running)


# --- service and pages ------------------------------------------------------------------------


@pytest.fixture
def downloaded(service):
    """Two downloaded models."""
    for repo in ("a/one", "b/two"):
        folder = service.settings.models_dir / repo
        folder.mkdir(parents=True)
        (folder / "config.json").write_text("{}")
    return service


def test_queue_asks_before_replacing_a_running_model(downloaded, monkeypatch):
    """With another model running the benchmark is refused unless it may replace it."""
    service = downloaded
    stopped = []
    monkeypatch.setattr(
        service.ai, "state", lambda: SessionState("running", repo="a/one", gpu_index=0)
    )
    monkeypatch.setattr(service, "stop_ai_session", lambda actor: stopped.append(actor))
    with pytest.raises(ValidationError, match="would replace it"):
        service.queue_benchmark("boss", "b/two", "humaneval")
    service.queue_benchmark("boss", "b/two", "humaneval", replace=True)
    assert stopped == ["boss"]
    with pytest.raises(ValidationError, match="being benchmarked"):
        service.delete_model("boss", "b/two")
    with pytest.raises(ValidationError, match="not downloaded"):
        service.queue_benchmark("boss", "c/three", "humaneval")


def test_latest_benchmarks_and_list(downloaded):
    """The headline is each model's newest finished result per suite."""
    service = downloaded
    for score, minute in ((0.3, 1), (0.4, 2)):
        service.db.execute(
            "INSERT INTO benchmarks (repo, suite, status, total, started_by, plus_score, "
            "started_at, finished_at) VALUES ('a/one', 'humaneval', 'done', 164, 'boss', ?, "
            "now(), now() + to_minutes(?))",
            [score, minute],
        )
    latest = service.latest_benchmarks()
    assert [row["plus_score"] for row in latest["a/one"]] == [0.4]
    assert latest["a/one"][0]["suite_label"] == "HumanEval+"
    rows = service.list_benchmarks()
    assert len(rows) == 2 and rows[0]["seconds"] is not None


def test_benchmark_files_need_login_and_known_names(client, users, service):
    """Report files are for logged-in users and only the known names are served."""
    folder = service.benchmarks.folder(7)
    folder.mkdir(parents=True)
    (folder / "samples.jsonl").write_text('{"task_id": "HumanEval/0"}\n')
    (folder / "secret.txt").write_text("no")
    client.cookies.clear()
    r = client.get("/benchmarks/7/files/samples.jsonl", follow_redirects=False)
    assert r.status_code == 303
    client.cookies.set("ow_session", login(client, "user1"))
    r = client.get("/benchmarks/7/files/samples.jsonl", headers={"Origin": ORIGIN})
    assert r.status_code == 200 and "HumanEval/0" in r.text
    assert client.get("/benchmarks/7/files/secret.txt").status_code == 404
    assert client.get("/benchmarks/7/files/..%2F..%2Fsecret.key").status_code == 404
