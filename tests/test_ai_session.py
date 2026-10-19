# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import json
import os
import stat
from types import SimpleNamespace

import docker
import pytest

from orbitalwindow import aisession
from orbitalwindow.aisession import (
    AiSessions,
    SessionRequest,
    clean_log,
    reasoning_parser,
    session_spec,
    tool_parser,
)
from orbitalwindow.config import Settings
from orbitalwindow.metrics import GpuStat
from orbitalwindow.users import ValidationError

CONFIG = {
    "model_type": "qwen3",
    "num_hidden_layers": 28,
    "num_attention_heads": 16,
    "num_key_value_heads": 8,
    "head_dim": 128,
    "max_position_embeddings": 40960,
}
REQUEST = SessionRequest("Qwen/Qwen3-0.6B", 0, 8192, 0.5, False)


def test_spec_private_session():
    """A private session has no Traefik labels; the key is in the environment, models read-only."""
    from orbitalwindow.config import Settings

    settings = Settings(root="/srv/ow", hostname="h")
    spec = session_spec(REQUEST, settings, "ow-key", 7)
    assert spec["name"] == "ow-ai-model" and spec["network"] == "ow-ai"
    command = spec["command"]
    assert command[:3] == ["/models/Qwen/Qwen3-0.6B", "--served-model-name", "Qwen/Qwen3-0.6B"]
    assert "--api-key" not in spec["command"] and spec["environment"]["VLLM_API_KEY"] == "ow-key"
    assert spec["command"][-6:] == [
        "--max-model-len",
        "8192",
        "--gpu-memory-utilization",
        "0.50",
        "--max-num-seqs",
        "16",
    ]
    assert spec["volumes"]["/srv/ow/shared/models"] == {"bind": "/models", "mode": "ro"}
    assert spec["device_requests"][0]["DeviceIDs"] == ["0"]
    assert spec["environment"]["HF_HUB_OFFLINE"] == "1"
    assert not any(key.startswith("traefik") for key in spec["labels"])
    assert spec["labels"]["ow.ai.session"] == "7"


def test_spec_public_session_routes_v1():
    """A public session is routed under /v1 on the TLS entrypoint."""
    from orbitalwindow.config import Settings

    spec = session_spec(
        SessionRequest("a/b", 1, 4096, 0.9, True), Settings(root="/x", hostname="h"), "k", 1
    )
    labels = spec["labels"]
    assert labels["traefik.http.routers.ow-ai-api.rule"] == "PathPrefix(`/v1`)"
    assert labels["traefik.docker.network"] == "ow-ai"
    assert labels["traefik.http.services.ow-ai-api.loadbalancer.server.port"] == "8000"
    assert spec["device_requests"][0]["DeviceIDs"] == ["1"]


def test_tool_parser_by_model_family():
    """Known families get vLLM's tool call parser; others none (and no tool flags)."""
    assert tool_parser({"model_type": "qwen3"}, "Qwen/Qwen3-8B") == "hermes"
    assert tool_parser({"model_type": "qwen3_moe"}, "Qwen/Qwen3-Coder-30B-A3B") == "qwen3_coder"
    assert tool_parser({"model_type": "llama"}, "meta-llama/Llama-3.1-8B-Instruct") == "llama3_json"
    assert tool_parser({"model_type": "gpt_oss"}, "openai/gpt-oss-20b") == "openai"
    assert tool_parser({"model_type": "qwen3_5"}, "Qwen/Qwen3.5-4B") == "qwen3_coder"
    xml = "<tool_call>\n<function=example_function_name>"
    assert tool_parser({"model_type": "qwen3"}, "someone/qwen-tuned", xml) == "qwen3_coder"
    assert tool_parser({"model_type": "phi3"}, "microsoft/Phi-3") == ""
    assert tool_parser({}, "a/b") == ""
    assert (
        "--enable-auto-tool-choice"
        not in session_spec(REQUEST, Settings(root="/x"), "k", 1)["command"]
    )
    request = SessionRequest("a/b", 0, 4096, 0.5, False, "hermes")
    command = session_spec(request, Settings(root="/x"), "k", 1)["command"]
    assert command[-3:] == ["--enable-auto-tool-choice", "--tool-call-parser", "hermes"]


def test_reasoning_parser_for_thinking_qwen_models():
    """Qwen3 models with <think> in their template get the qwen3 reasoning parser."""
    assert reasoning_parser({"model_type": "qwen3_5"}, "{{ '<think>\\n' }}") == "qwen3"
    assert reasoning_parser({"model_type": "qwen3"}, "no thinking here") == ""
    assert reasoning_parser({"model_type": "llama"}, "<think>") == ""
    request = SessionRequest("a/b", 0, 4096, 0.5, False, "", "qwen3")
    command = session_spec(request, Settings(root="/x"), "k", 1)["command"]
    assert command[-2:] == ["--reasoning-parser", "qwen3"] and "--tool-call-parser" not in command


def test_clean_log_drops_progress_bars():
    """Progress bars and blank lines are dropped from the log shown on the page."""
    text = "INFO loading\nCapturing CUDA graphs: 10%|█| 5/51 [00:01<00:09, 4.8it/s]\n\nERROR oom\n"
    assert clean_log(text) == "INFO loading\nERROR oom"


class FakeContainer:
    def __init__(self, labels, status="running", health="starting", log=b""):
        """Remember labels, status, health and log."""
        self.labels, self.status, self._log, self.removed = labels, status, log, False
        self.attrs = {"State": {"Health": {"Status": health}}}

    def reload(self):
        """Keep the scripted state."""

    def logs(self, tail=200):
        """Return the scripted log."""
        return self._log

    def remove(self, force=False):
        """Mark as removed."""
        self.removed = True


class FakeNetwork:
    def __init__(self):
        """Start without members."""
        self.name, self.containers, self.connected = "ow-ai", [], []

    def reload(self):
        """Nothing to reload."""

    def connect(self, name):
        """Record a connection."""
        self.connected.append(name)
        self.containers.append(SimpleNamespace(name=name))


class FakeClient:
    def __init__(self, fail=None):
        """Script an optional run failure."""
        self.current, self.runs, self.fail, self.network = None, [], fail, None
        self.containers = SimpleNamespace(get=self._get, run=self._run)
        self.networks = SimpleNamespace(list=self._list, create=self._create)
        self.images = SimpleNamespace(get=lambda image: None, pull=lambda image: None)

    def _get(self, name):
        """Return the session container or raise NotFound."""
        if self.current is None or self.current.removed:
            raise docker.errors.NotFound("none")
        return self.current

    def _run(self, image, **kwargs):
        """Record the run and create a starting container."""
        if self.fail:
            raise self.fail
        self.runs.append((image, kwargs))
        self.current = FakeContainer(kwargs["labels"])
        return self.current

    def _list(self, names=None):
        """Return the session network if it exists."""
        return [self.network] if self.network else []

    def _create(self, name, **kwargs):
        """Create the session network and remember that it is internal."""
        self.network = FakeNetwork()
        self.network.internal = kwargs.get("internal")
        return self.network


@pytest.fixture
def sessions(service):
    """AiSessions with a fake Docker client on the test database."""
    return AiSessions(FakeClient(), service.db, service.settings)


def test_session_lifecycle(sessions):
    """Stopped → starting (loading) → running (healthy) → stopped, with a row per start."""
    assert sessions.state().status == "stopped"
    session_id = sessions.start(REQUEST, "ow-key", "boss", wait=True)
    client = sessions._client
    assert client.network.internal is True and client.network.connected == []
    image, kwargs = client.runs[0]
    assert image == "vllm/vllm-openai:v0.30.0" and kwargs["labels"]["ow.ai.session"] == str(
        session_id
    )
    state = sessions.state()
    assert state.status == "starting" and state.repo == "Qwen/Qwen3-0.6B" and state.gpu_share == 0.5
    client.current.attrs["State"]["Health"]["Status"] = "healthy"
    assert sessions.state().status == "running"
    with pytest.raises(ValidationError, match="already running"):
        sessions.start(REQUEST, "ow-key", "boss", wait=True)
    assert sessions.stop() == "Qwen/Qwen3-0.6B" and client.current.removed
    assert sessions.state().status == "stopped"
    row = sessions._db.one("SELECT * FROM ai_sessions WHERE id = ?", [session_id])
    assert row["status"] == "stopped" and row["stopped_at"] is not None


def test_public_session_connects_traefik(sessions):
    """With the API public, Traefik joins the session network."""
    sessions.start(SessionRequest("a/b", 0, 4096, 0.5, True), "k", "boss", wait=True)
    assert sessions._client.network.connected == ["ow-traefik"]


def test_crashed_session_is_failed_with_log(sessions):
    """A container that exits shows as failed with the end of its log, also after removal."""
    sessions.start(REQUEST, "k", "boss", wait=True)
    container = sessions._client.current
    container.status = "exited"
    container._log = b"INFO start\nValueError: not enough GPU memory\n"
    state = sessions.state()
    assert state.status == "failed" and "not enough GPU memory" in state.error
    container.removed = True
    assert sessions.state().status == "failed"


def test_launch_failure_is_recorded(service):
    """If Docker can't run the container, the session is failed with the reason."""
    sessions = AiSessions(
        FakeClient(fail=docker.errors.APIError("could not select device driver")),
        service.db,
        service.settings,
    )
    sessions.start(REQUEST, "k", "boss", wait=True)
    state = sessions.state()
    assert state.status == "failed" and "could not select device driver" in state.error


def test_vanished_container_counts_as_stopped(sessions):
    """A session whose container disappeared without a stop is recorded as stopped."""
    session_id = sessions.start(REQUEST, "k", "boss", wait=True)
    sessions._client.current.removed = True
    assert sessions.state().status == "stopped"
    status = sessions._db.scalar("SELECT status FROM ai_sessions WHERE id = ?", [session_id])
    assert status == "stopped"


# --- service ----------------------------------------------------------------------------------


@pytest.fixture
def model_on_disk(service):
    """A downloaded Qwen3-0.6B-like model in the library and one 16 GB GPU."""
    folder = service.settings.models_dir / "Qwen" / "Qwen3-0.6B"
    folder.mkdir(parents=True)
    (folder / "config.json").write_text(json.dumps(CONFIG))
    (folder / "model.safetensors").write_bytes(b"x" * 1000)
    service.gpus.stats = lambda: [GpuStat(0, "RTX 5080", 0, 1024**3, 16 * 1024**3)]
    service.ai = AiSessions(FakeClient(), service.db, service.settings)
    original = service.ai.start
    service.ai.start = lambda request, key, actor: original(request, key, actor, wait=True)
    return folder


def test_start_session_validates_and_audits(service, model_on_disk):
    """Bad values are refused; a good start runs vLLM with the stored key and is audited."""
    for args, message in (
        (("Qwen/Qwen3-8B", 0, 8192, 0.5), "not downloaded"),
        (("Qwen/Qwen3-0.6B", 3, 8192, 0.5), "GPU"),
        (("Qwen/Qwen3-0.6B", 0, 100, 0.5), "Context length"),
        (("Qwen/Qwen3-0.6B", 0, 50000, 0.5), "Context length"),
        (("Qwen/Qwen3-0.6B", 0, 8192, 0.99), "between 10 % and 95 %"),
        (("Qwen/Qwen3-0.6B", 0, 8192, "lots"), "number"),
    ):
        with pytest.raises(ValidationError, match=message):
            service.start_ai_session("boss", *args)
    service.start_ai_session("boss", "Qwen/Qwen3-0.6B", "0", "8192", 0.5)
    _, kwargs = service.ai._client.runs[0]
    assert kwargs["environment"]["VLLM_API_KEY"] == service.ai_api_key()
    assert kwargs["command"][-2:] == ["--tool-call-parser", "hermes"]
    assert service._model_tools("Qwen/Qwen3-0.6B") and not service._model_tools("")
    assert service.db.scalar("SELECT count(*) FROM audit WHERE action = 'ai.session.start'") == 1
    with pytest.raises(ValidationError, match="running in the AI session"):
        service.delete_model("boss", "Qwen/Qwen3-0.6B")
    service.stop_ai_session("boss")
    assert service.ai.state().status == "stopped"


def test_start_needs_an_nvidia_gpu(service, model_on_disk):
    """Without NVML there is no AI session."""
    service.gpus.stats = lambda: []
    with pytest.raises(ValidationError, match="NVIDIA"):
        service.start_ai_session("boss", "Qwen/Qwen3-0.6B", 0, 8192, 0.5)


def test_session_defaults(service, model_on_disk):
    """Defaults cap the context at the model's maximum and size the GPU share from the estimate."""
    defaults = service.session_defaults("Qwen/Qwen3-0.6B", 100_000)
    assert defaults["context"] == 40960 and defaults["max_context"] == 40960
    assert 0.3 <= defaults["gpu_share"] <= 0.9 and defaults["verdict"].level == "fits"
    assert defaults["short_for_coding"] is False
    suggested = service.session_defaults("Qwen/Qwen3-0.6B")
    assert suggested["context"] == 40960  # longest the model allows, under 64k
    assert service.session_defaults("Qwen/Qwen3-0.6B", 8192)["short_for_coding"] is True


def test_api_key_and_public_setting(service):
    """The key is made once (mode 600), replaced on request and never logged; public is off."""
    key = service.ai_api_key()
    path = service.settings.ai_api_key_path
    assert key.startswith("ow-") and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert service.ai_api_key() == key
    service.new_ai_api_key("boss")
    assert service.ai_api_key() != key
    assert service.db.scalar("SELECT count(*) FROM audit WHERE detail LIKE '%ow-%'") == 0
    assert not service.ai_api_public()
    service.set_ai_api_public("boss", True)
    assert service.ai_api_public() and service.ai_api_url() == "https://desk.test/v1"


def test_reconcile_reconnects_traefik_for_public_session(service, model_on_disk):
    """After a restart Traefik is reconnected to the session network of a public session."""
    service.set_ai_api_public("boss", True)
    service.start_ai_session("boss", "Qwen/Qwen3-0.6B", 0, 8192, 0.5)
    network = service.ai._client.network
    network.containers.clear()
    network.connected.clear()
    service.ai.ensure_connected()
    assert network.connected == [service.settings.traefik_container]
    assert aisession.CONTAINER == "ow-ai-model"
