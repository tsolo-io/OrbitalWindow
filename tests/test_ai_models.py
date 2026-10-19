# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import os
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock

import docker
import httpx2
import pytest
from fastapi.responses import RedirectResponse

from orbitalwindow import downloads as dl
from orbitalwindow.models import (
    HubClient,
    ModelLibrary,
    RepoInfo,
    Space,
    parse_repo_info,
    validate_repo,
    validate_revision,
    wanted,
)
from orbitalwindow.ui import common
from orbitalwindow.users import ValidationError

# Trimmed answers of /api/models/<repo>/revision/main?blobs=true (October 2026).
QWEN = {
    "id": "Qwen/Qwen3-0.6B",
    "sha": "c1899de289a04d12100db370d81485cdf75e47ca",
    "gated": False,
    "config": {"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3"},
    "safetensors": {"parameters": {"BF16": 751632384}, "total": 751632384},
    "cardData": {"license": "apache-2.0"},
    "siblings": [
        {"rfilename": ".gitattributes", "size": 1570},
        {"rfilename": "LICENSE", "size": 11343},
        {"rfilename": "README.md", "size": 13965},
        {"rfilename": "config.json", "size": 726},
        {"rfilename": "generation_config.json", "size": 239},
        {"rfilename": "merges.txt", "size": 1671853},
        {"rfilename": "model.safetensors", "size": 1503300328},
        {"rfilename": "tokenizer.json", "size": 11422654},
        {"rfilename": "tokenizer_config.json", "size": 9732},
        {"rfilename": "vocab.json", "size": 2776833},
    ],
}
LLAMA = {
    "id": "meta-llama/Llama-3.1-8B-Instruct",
    "sha": "0e9e39f249a16976918f6564b8830bc894c89659",
    "gated": "manual",
    "config": {"architectures": ["LlamaForCausalLM"]},
    "safetensors": {"total": 8030261248},
    "cardData": {"license": "llama3.1"},
    "siblings": [
        {"rfilename": "config.json", "size": 855},
        {"rfilename": "model-00001-of-00004.safetensors", "size": 4976698672},
        {"rfilename": "original/consolidated.00.pth", "size": 16060617592},
        {"rfilename": "original/params.json", "size": 199},
        {"rfilename": "original/tokenizer.model", "size": 2183982},
    ],
}


def info(repo="Qwen/Qwen3-0.6B", files=(("model.safetensors", 1000),)) -> RepoInfo:
    """Return a RepoInfo with the given files."""
    return RepoInfo(repo, "abc123", False, "Qwen3ForCausalLM", None, "", tuple(files))


# --- names and file selection -----------------------------------------------------------------


@pytest.mark.parametrize("repo", ["Qwen/Qwen3-8B", " meta-llama/Llama-3.1-8B-Instruct ", "a/b"])
def test_valid_repo_ids(repo):
    """Organisation/model ids are accepted (spaces stripped)."""
    assert validate_repo(repo) == repo.strip()


@pytest.mark.parametrize("repo", ["Qwen3-8B", "a/b/c", "../etc/x", "a/..", "a/b c", "", "/x"])
def test_invalid_repo_ids(repo):
    """Ids without one slash, with path tricks or spaces are refused."""
    with pytest.raises(ValidationError):
        validate_repo(repo)


def test_revision_defaults_to_main_and_refuses_tricks():
    """An empty revision is main; '..' and spaces are refused."""
    assert validate_revision("") == "main"
    assert validate_revision("refs/pr/1") == "refs/pr/1"
    for bad in ("../x", "a b", "-x"):
        with pytest.raises(ValidationError):
            validate_revision(bad)


def test_only_inference_files_are_downloaded():
    """Weights, configs and tokenizers are wanted; docs, .bin and original/ are not."""
    assert wanted("model-00001-of-00004.safetensors")
    assert wanted("tokenizer.json") and wanted("tokenizer.model") and wanted("merges.txt")
    assert not wanted("README.md") and not wanted("pytorch_model.bin")
    assert not wanted("original/tokenizer.model") and not wanted("original/params.json")


def test_parse_repo_info_public_model():
    """The Qwen answer gives architecture, parameters, licence and the wanted files' size."""
    parsed = parse_repo_info(QWEN)
    assert parsed.repo == "Qwen/Qwen3-0.6B" and parsed.revision.startswith("c1899de")
    assert not parsed.gated and parsed.has_safetensors
    assert parsed.architecture == "Qwen3ForCausalLM" and parsed.parameters == 751632384
    assert parsed.license == "apache-2.0"
    assert parsed.total_bytes == 1519182365
    assert "README.md" not in dict(parsed.files)


def test_parse_repo_info_gated_model_skips_original_checkpoint():
    """Gated is a flag, and the 16 GB original/ checkpoint is not counted."""
    parsed = parse_repo_info(LLAMA)
    assert parsed.gated
    assert parsed.total_bytes == 855 + 4976698672


# --- Hub client -------------------------------------------------------------------------------


def hub(handler) -> HubClient:
    """Return a HubClient whose requests go to ``handler``."""
    transport = httpx2.MockTransport(handler)
    return HubClient(client=httpx2.Client(base_url="https://hub.test", transport=transport))


def test_inspect_sends_token_and_parses():
    """Inspect calls the revision endpoint with blobs and the token, and parses the answer."""
    seen = {}

    def handler(request):
        """Record the request and answer with the Qwen data."""
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx2.Response(200, json=QWEN)

    result = hub(handler).inspect("Qwen/Qwen3-0.6B", "main", "hf_secret")
    assert seen["url"] == "https://hub.test/api/models/Qwen/Qwen3-0.6B/revision/main?blobs=true"
    assert seen["auth"] == "Bearer hf_secret"
    assert result.repo == "Qwen/Qwen3-0.6B"


@pytest.mark.parametrize("status", [401, 403, 404])
def test_inspect_unknown_or_private_model(status):
    """The Hub answers 401 for unknown models without a token; all mean 'not found'."""
    with pytest.raises(ValidationError, match="no model"):
        hub(lambda request: httpx2.Response(status)).inspect("a/b", "main", None)


def test_inspect_network_error():
    """A connection failure becomes a readable error."""

    def handler(request):
        """Fail like an unreachable host."""
        raise httpx2.ConnectError("no route", request=request)

    with pytest.raises(ValidationError, match="Could not reach"):
        hub(handler).inspect("a/b", "main", None)


# --- library ----------------------------------------------------------------------------------


def make_model(root, repo, size=100):
    """Create a model folder with a config and one weights file of ``size`` bytes."""
    folder = root / repo
    folder.mkdir(parents=True)
    (folder / "config.json").write_text("{}")
    (folder / "model.safetensors").write_bytes(b"x" * size)
    return folder


def test_library_lists_models_with_sizes(tmp_path):
    """Folders <org>/<name> with a config.json are models; others are ignored."""
    make_model(tmp_path, "Qwen/Qwen3-0.6B", 100)
    make_model(tmp_path, "Qwen/Qwen3-8B", 50)
    (tmp_path / "junk" / "folder").mkdir(parents=True)
    (tmp_path / ".hidden" / "x").mkdir(parents=True)
    found = ModelLibrary(tmp_path).models()
    assert [m.repo for m in found] == ["Qwen/Qwen3-0.6B", "Qwen/Qwen3-8B"]
    assert found[0].bytes == 102  # weights + "{}"


def test_library_delete_removes_empty_org(tmp_path):
    """Deleting the last model of an organisation removes its folder too."""
    make_model(tmp_path, "a/one")
    make_model(tmp_path, "a/two")
    library = ModelLibrary(tmp_path)
    library.delete("a/one")
    assert (tmp_path / "a").is_dir()
    library.delete("a/two")
    assert not (tmp_path / "a").exists()


# --- downloads --------------------------------------------------------------------------------


class FakeContainer:
    def __init__(self, labels, status="running", code=0, log=b""):
        """Remember the container's labels, state, exit code and log."""
        self.labels = labels
        self.status = status
        self.attrs = {"State": {"ExitCode": code}}
        self._log = log
        self.removed = False

    def reload(self):
        """Keep the scripted state."""

    def logs(self, tail=60):
        """Return the scripted log."""
        return self._log

    def remove(self, force=False):
        """Mark the container as removed."""
        self.removed = True


class FakeContainers:
    def __init__(self):
        """Start without a download container."""
        self.current = None
        self.runs = []

    def get(self, name):
        """Return the download container or raise NotFound."""
        if self.current is None or self.current.removed:
            raise docker.errors.NotFound("none")
        return self.current

    def run(self, image, command, **kwargs):
        """Record the run and create a running container with its labels."""
        self.runs.append((image, command, kwargs))
        self.current = FakeContainer(kwargs["labels"])
        return self.current


@pytest.fixture
def downloader(service, tmp_path):
    """A Downloader with a fake Docker client, models under tmp_path and a token."""
    client = SimpleNamespace(containers=FakeContainers(), images=MagicMock())
    return dl.Downloader(client, service.db, ModelLibrary(tmp_path), 10001, lambda: "hf_tok")


def status(downloader, download_id):
    """Return the stored status of a download."""
    return downloader._db.scalar("SELECT status FROM model_downloads WHERE id = ?", [download_id])


def test_download_command_pins_revision_and_filters():
    """The command downloads the pinned commit into the model folder with the file filters."""
    cmd = dl.download_command("Qwen/Qwen3-8B", "abc")
    assert cmd[:5] == ["hf", "download", "Qwen/Qwen3-8B", "--revision", "abc"]
    assert cmd[5:7] == ["--local-dir", "/models/Qwen/Qwen3-8B"]
    assert cmd.count("--include") == 6 and "original/*" in cmd


def test_queue_refuses_models_without_safetensors(downloader):
    """vLLM needs safetensors weights, so .bin-only models are refused."""
    with pytest.raises(ValidationError, match="safetensors"):
        downloader.queue(info(files=(("pytorch_model.bin", 10),)), "boss")


def test_queue_refuses_a_second_active_download(downloader):
    """The same model can't be queued twice while one is active."""
    downloader.queue(info(), "boss")
    with pytest.raises(ValidationError, match="already"):
        downloader.queue(info(), "boss")


def test_download_runs_as_service_uid_with_token_and_finishes(downloader, tmp_path):
    """A queued download starts one locked-down container and is done when it exits 0."""
    first = downloader.queue(info(), "boss")
    second = downloader.queue(info("Qwen/Qwen3-8B"), "boss")
    downloader.step()
    containers = downloader._client.containers
    image, command, kwargs = containers.runs[0]
    assert image == dl.HF_IMAGE and command[2] == "Qwen/Qwen3-0.6B"
    assert kwargs["user"] == "10001:10001" and kwargs["environment"]["HF_TOKEN"] == "hf_tok"
    assert kwargs["volumes"] == {str(tmp_path): {"bind": "/models", "mode": "rw"}}
    assert kwargs["cap_drop"] == ["ALL"] and kwargs["labels"]["ow.download"] == str(first)
    assert status(downloader, first) == "running" and status(downloader, second) == "queued"

    make_model(tmp_path, "Qwen/Qwen3-0.6B", 600)
    downloader.step()  # still running: progress is the folder size, capped at the total
    assert (
        downloader._db.scalar("SELECT bytes_done FROM model_downloads WHERE id = ?", [first]) == 602
    )

    containers.current.status = "exited"
    downloader.step()
    assert status(downloader, first) == "done" and containers.current.removed
    downloader.step()  # next one starts
    assert status(downloader, second) == "running" and len(containers.runs) == 2


def test_failed_download_keeps_log_tail(downloader):
    """A non-zero exit marks the download failed with the log tail, without hf's hints."""
    download_id = downloader.queue(info(), "boss")
    downloader.step()
    container = downloader._client.containers.current
    container.status, container.attrs["State"]["ExitCode"] = "exited", 1
    container._log = (
        b"To update, run: hf update\x1b[0m\n"
        b"Hint: update hf\n\x1b[31m401 Client Error: gated repo\x1b[0m\n"
    )
    downloader.step()
    row = downloader._db.one("SELECT * FROM model_downloads WHERE id = ?", [download_id])
    assert row["status"] == "failed" and row["error"] == "401 Client Error: gated repo"


def test_cancel_queued_and_running(downloader):
    """A queued download is cancelled at once; a running one when its container is removed."""
    running = downloader.queue(info(), "boss")
    waiting = downloader.queue(info("Qwen/Qwen3-8B"), "boss")
    downloader.step()
    downloader.cancel(waiting)
    assert status(downloader, waiting) == "cancelled"
    downloader.cancel(running)
    assert status(downloader, running) == "cancelling"
    downloader.step()
    assert status(downloader, running) == "cancelled"
    assert downloader._client.containers.current.removed
    with pytest.raises(ValidationError):
        downloader.cancel(running)


def test_lost_container_is_queued_again(downloader):
    """If the container of a running download disappears, the download is queued again."""
    download_id = downloader.queue(info(), "boss")
    downloader.step()
    downloader._client.containers.current.removed = True
    downloader.step()
    assert status(downloader, download_id) == "queued"
    downloader.step()
    assert status(downloader, download_id) == "running"


def test_stray_container_is_removed(downloader):
    """A download container the database doesn't know is removed."""
    containers = downloader._client.containers
    containers.current = FakeContainer({"ow.download": "999"})
    downloader.step()
    assert containers.current.removed


# --- service ----------------------------------------------------------------------------------


def test_ai_manager_role(service, users):
    """The AI manager role is set and audited; admins can manage AI anyway."""
    uid = users["user1"]["id"]
    service.set_ai_manager("boss", uid, True)
    token = service.login("user1", "correct-horse-1", "1.2.3.4")
    assert service.sessions.resolve(token).can_manage_ai
    assert service.db.scalar("SELECT count(*) FROM audit WHERE action = 'user.ai_manager'") == 1
    service.set_ai_manager("boss", uid, False)
    assert not service.sessions.resolve(token).can_manage_ai
    boss = service.login("boss", "correct-horse-1", "1.2.3.4")
    assert service.sessions.resolve(boss).can_manage_ai


def test_ai_page_needs_the_role(service, users):
    """The AI page redirects plain users to the dashboard and opens for AI managers."""
    common.set_service(service)
    token = service.login("user1", "correct-horse-1", "1.2.3.4")
    request = SimpleNamespace(cookies={"ow_session": token}, url=SimpleNamespace(path="/ai"))
    denied = common.authenticate(request, ai=True)
    assert isinstance(denied, RedirectResponse) and denied.headers["location"] == "/"
    service.set_ai_manager("boss", users["user1"]["id"], True)
    assert isinstance(common.authenticate(request, ai=True), common.PageAuth)


def test_hf_token_is_private_and_not_logged(service):
    """The token is stored with mode 600, never written to the audit log, and can be cleared."""
    with pytest.raises(ValidationError):
        service.set_hf_token("boss", "not-a-token")
    token = "hf_" + "a1" * 15
    service.set_hf_token("boss", token)
    path = service.settings.hf_token_path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and service.hf_token() == token
    assert service.db.scalar("SELECT count(*) FROM audit WHERE detail LIKE '%hf_a1%'") == 0
    service.set_hf_token("boss", "")
    assert service.hf_token() is None and not path.exists()


def test_download_refused_when_disk_is_too_small(service, monkeypatch):
    """A download that doesn't fit on the disk is refused before it is queued."""
    monkeypatch.setattr(service.hub, "inspect", lambda repo, rev, token: info(repo))
    monkeypatch.setattr(service.models, "space", lambda: Space(2000, 1500, 500))
    with pytest.raises(ValidationError, match="only 0.0 GB is free"):
        service.download_model("boss", "Qwen/Qwen3-0.6B")
    monkeypatch.setattr(service.models, "space", lambda: Space(10**12, 0, 10**12))
    assert service.download_model("boss", "Qwen/Qwen3-0.6B") > 0


def test_delete_refused_while_downloading(service, monkeypatch, tmp_path):
    """A model can't be deleted while it is being downloaded."""
    monkeypatch.setattr(service.hub, "inspect", lambda repo, rev, token: info(repo))
    monkeypatch.setattr(service.models, "space", lambda: Space(10**12, 0, 10**12))
    service.download_model("boss", "Qwen/Qwen3-0.6B")
    with pytest.raises(ValidationError, match="being downloaded"):
        service.delete_model("boss", "Qwen/Qwen3-0.6B")


# --- GGUF (llama.cpp) -------------------------------------------------------------------------


def gguf_info(repo="Qwen/Qwen3-8B-GGUF") -> RepoInfo:
    """Return a GGUF repository as the Hub describes it: no safetensors, two quantisations."""
    return parse_repo_info(
        {
            "id": repo,
            "sha": "def456",
            "gguf": {"total": 8190735360, "architecture": "qwen3", "context_length": 40960},
            "siblings": [
                {"rfilename": "README.md", "size": 7577},
                {"rfilename": "Qwen3-8B-Q4_K_M.gguf", "size": 5027783488},
                {"rfilename": "Qwen3-8B-Q8_0.gguf", "size": 8709518112},
            ],
        }
    )


def test_parse_repo_info_lists_gguf_models():
    """A GGUF repository has no safetensors files but one GGUF model per quantisation."""
    found = gguf_info()
    assert not found.has_safetensors and found.files == ()
    assert (found.architecture, found.parameters) == ("qwen3", 8190735360)
    sizes = [(m.quant, m.bytes) for m in found.gguf]
    assert sizes == [("Q4_K_M", 5027783488), ("Q8_0", 8709518112)]
    assert found.gguf_model("Qwen3-8B-Q8_0.gguf").quant == "Q8_0"
    with pytest.raises(ValidationError, match="no GGUF file"):
        found.gguf_model("other.gguf")


def test_library_lists_gguf_models(tmp_path):
    """A folder with only a GGUF file is a model too; partial files in .cache don't count."""
    folder = tmp_path / "Qwen" / "Qwen3-8B-GGUF"
    (folder / ".cache").mkdir(parents=True)
    (folder / ".cache" / "x.gguf").write_bytes(b"partial")
    (tmp_path / "Qwen" / "Empty" / ".cache").mkdir(parents=True)
    (tmp_path / "Qwen" / "Empty" / ".cache" / "y.gguf").write_bytes(b"partial")
    (folder / "Qwen3-8B-Q4_K_M.gguf").write_bytes(b"x" * 50)
    make_model(tmp_path, "Qwen/Qwen3-0.6B")
    found = {m.repo: m for m in ModelLibrary(tmp_path).models()}
    assert sorted(found) == ["Qwen/Qwen3-0.6B", "Qwen/Qwen3-8B-GGUF"]
    model = found["Qwen/Qwen3-8B-GGUF"]
    assert model.gguf.name == "Qwen3-8B-Q4_K_M.gguf" and not model.has_safetensors
    assert found["Qwen/Qwen3-0.6B"].has_safetensors and found["Qwen/Qwen3-0.6B"].gguf is None


def test_gguf_download_fetches_only_the_chosen_files(downloader):
    """A GGUF download includes only the chosen quantisation's files and counts their size."""
    cmd = dl.download_command("Qwen/Qwen3-8B-GGUF", "def456", ("Qwen3-8B-Q4_K_M.gguf",))
    assert cmd.count("--include") == 1 and "Qwen3-8B-Q4_K_M.gguf" in cmd and "original/*" not in cmd
    found = gguf_info()
    with pytest.raises(ValidationError, match="Choose one of its GGUF files"):
        downloader.queue(found, "boss")
    download_id = downloader.queue(found, "boss", found.gguf[0])
    row = downloader._db.one("SELECT * FROM model_downloads WHERE id = ?", [download_id])
    assert row["files"] == "Qwen3-8B-Q4_K_M.gguf" and row["bytes_total"] == 5027783488
    downloader.step()
    _, command, _ = downloader._client.containers.runs[0]
    assert command.count("--include") == 1


def test_a_model_folder_holds_one_gguf_file(service, monkeypatch, tmp_path):
    """Downloading another quantisation into a folder with a GGUF file is refused."""
    monkeypatch.setattr(service.hub, "inspect", lambda repo, rev, token: gguf_info(repo))
    monkeypatch.setattr(service.models, "space", lambda: Space(10**12, 0, 10**12))
    folder = service.models.path_for("Qwen/Qwen3-8B-GGUF")
    folder.mkdir(parents=True)
    (folder / "Qwen3-8B-Q4_K_M.gguf").write_bytes(b"x")
    with pytest.raises(ValidationError, match="already has Qwen3-8B-Q4_K_M.gguf"):
        service.download_model("boss", "Qwen/Qwen3-8B-GGUF", "", "Qwen3-8B-Q8_0.gguf")
    assert service.download_model("boss", "Qwen/Qwen3-8B-GGUF", "", "Qwen3-8B-Q4_K_M.gguf") > 0
