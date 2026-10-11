# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Model library: Hugging Face models downloaded to ``$OW_ROOT/shared/models/<org>/<name>``.

Each model is a plain folder (``hf download --local-dir``), not the Hub cache: its size is one
walk, deleting it is one ``rmtree``, and vLLM or Python code load it by path. Only the files an
inference engine needs are downloaded (``INCLUDE``); duplicate original checkpoints are skipped.
"""

import contextlib
import fnmatch
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

from .users import ValidationError

HUB_URL = "https://huggingface.co"
INCLUDE = ("*.json", "*.safetensors", "*.txt", "*.model", "*.tiktoken", "*.jinja")
EXCLUDE = ("original/*",)
# Folder hf keeps resume metadata in, inside each model folder.
HF_META_DIR = ".cache"
NAME_PART = r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
REPO_RE = re.compile(rf"^{NAME_PART}/{NAME_PART}$")
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
TOKEN_RE = re.compile(r"^hf_[A-Za-z0-9]{20,100}$")


def validate_repo(repo: str) -> str:
    """Check a Hugging Face model id.

    Args:
        repo: The id as typed, e.g. ``Qwen/Qwen3-8B`` (surrounding spaces are ignored).

    Returns:
        The stripped id.

    Raises:
        ValidationError: If it is not of the form ``<organisation>/<model>``.
    """
    repo = repo.strip()
    if not REPO_RE.fullmatch(repo) or ".." in repo:
        raise ValidationError(
            "Enter a Hugging Face model id like Qwen/Qwen3-8B (organisation/model)."
        )
    return repo


def validate_revision(revision: str) -> str:
    """Check a branch, tag or commit name.

    Args:
        revision: The revision as typed; empty means ``main``.

    Returns:
        The stripped revision, ``main`` if it was empty.

    Raises:
        ValidationError: If it contains characters a revision can't have.
    """
    revision = revision.strip() or "main"
    if not REVISION_RE.fullmatch(revision) or ".." in revision:
        raise ValidationError("The revision may contain letters, digits, '.', '_', '-' and '/'.")
    return revision


def wanted(path: str) -> bool:
    """Tell whether a repo file is downloaded.

    Args:
        path: File path inside the repo, e.g. ``model-00001-of-00004.safetensors``.

    Returns:
        True if it matches ``INCLUDE`` and not ``EXCLUDE``.
    """
    if any(fnmatch.fnmatch(path, pattern) for pattern in EXCLUDE):
        return False
    return any(fnmatch.fnmatch(path, pattern) for pattern in INCLUDE)


@dataclass(frozen=True)
class RepoInfo:
    repo: str
    revision: str  # commit sha the inspection saw
    gated: bool
    architecture: str
    parameters: int | None
    license: str
    files: tuple[tuple[str, int], ...]  # (path, bytes) of the files that will be downloaded

    @property
    def total_bytes(self) -> int:
        """Return the size of the files that will be downloaded.

        Returns:
            The sum of their sizes in bytes.
        """
        return sum(size for _, size in self.files)

    @property
    def has_safetensors(self) -> bool:
        """Tell whether the repo has weights in safetensors format (what vLLM loads).

        Returns:
            True if at least one ``.safetensors`` file will be downloaded.
        """
        return any(path.endswith(".safetensors") for path, _ in self.files)


def parse_repo_info(data: dict[str, Any]) -> RepoInfo:
    """Turn a Hub API model response (``?blobs=true``) into a ``RepoInfo``.

    Args:
        data: The decoded JSON of ``/api/models/<repo>/revision/<rev>?blobs=true``.

    Returns:
        The model's details and the files that would be downloaded.
    """
    config = data.get("config") or {}
    architectures = config.get("architectures") or []
    safetensors = data.get("safetensors") or {}
    card = data.get("cardData") or {}
    files = tuple(
        (sibling["rfilename"], int(sibling.get("size") or 0))
        for sibling in data.get("siblings") or []
        if wanted(sibling.get("rfilename", ""))
    )
    licence = card.get("license") or ""
    return RepoInfo(
        repo=data.get("id", ""),
        revision=data.get("sha", ""),
        gated=bool(data.get("gated")),
        architecture=architectures[0] if architectures else config.get("model_type", ""),
        parameters=safetensors.get("total"),
        license=licence if isinstance(licence, str) else ", ".join(licence),
        files=files,
    )


class HubClient:
    """Reads model details from the Hugging Face Hub API."""

    def __init__(self, base_url: str = HUB_URL, client: httpx2.Client | None = None) -> None:
        """Create the client.

        Args:
            base_url: Hub address.
            client: HTTP client to use (tests pass one with a mock transport).
        """
        self._client = client or httpx2.Client(base_url=base_url, timeout=15.0)

    def inspect(self, repo: str, revision: str, token: str | None) -> RepoInfo:
        """Look up a model and the files a download would fetch.

        Args:
            repo: Model id, already validated.
            revision: Branch, tag or commit, already validated.
            token: Hugging Face token, or None.

        Returns:
            The model's details.

        Raises:
            ValidationError: If the model doesn't exist, needs a token, or the Hub can't be reached.
        """
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = self._client.get(
                f"/api/models/{repo}/revision/{revision}",
                params={"blobs": "true"},
                headers=headers,
            )
        except httpx2.HTTPError as exc:
            raise ValidationError(f"Could not reach Hugging Face: {exc}") from exc
        if response.status_code in (401, 403, 404):
            raise ValidationError(
                f"Hugging Face has no model '{repo}' at '{revision}', or it is private. "
                "Check the name; private models need a token."
            )
        if response.status_code != 200:
            raise ValidationError(f"Hugging Face answered with HTTP {response.status_code}.")
        try:
            return parse_repo_info(response.json())
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError(f"Unexpected answer from Hugging Face: {exc}") from exc

    def config(self, repo: str, revision: str, token: str | None) -> dict[str, Any] | None:
        """Fetch a model's ``config.json``.

        Args:
            repo: Model id, already validated.
            revision: Commit, branch or tag, already validated.
            token: Hugging Face token, or None (gated models need one).

        Returns:
            The parsed configuration, or None if it can't be read.
        """
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = self._client.get(
                f"/{repo}/resolve/{revision}/config.json", headers=headers, follow_redirects=True
            )
            data = response.json() if response.status_code == 200 else None
        except httpx2.HTTPError, ValueError:
            return None
        return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class LocalModel:
    repo: str
    path: Path
    bytes: int
    modified: datetime

    @property
    def weights(self) -> int:
        """Return the size of the model's safetensors files.

        Returns:
            Bytes.
        """
        return sum(file.stat().st_size for file in self.path.rglob("*.safetensors"))

    def config(self) -> dict[str, Any] | None:
        """Read the model's ``config.json``.

        Returns:
            The parsed configuration, or None if it can't be read.
        """
        try:
            data = json.loads((self.path / "config.json").read_text())
        except OSError, ValueError:
            return None
        return data if isinstance(data, dict) else None

    def chat_template(self) -> str:
        """Read the model's chat template (``chat_template.jinja`` or ``tokenizer_config.json``).

        Returns:
            The template, or an empty string if there is none.
        """
        with contextlib.suppress(OSError):
            return (self.path / "chat_template.jinja").read_text()
        try:
            data = json.loads((self.path / "tokenizer_config.json").read_text())
        except OSError, ValueError:
            return ""
        template = data.get("chat_template") if isinstance(data, dict) else None
        return template if isinstance(template, str) else ""


@dataclass(frozen=True)
class Space:
    total: int
    used: int
    free: int


def folder_bytes(path: Path) -> int:
    """Return the size of every file under a folder, without following symlinks.

    Args:
        path: The folder; a missing folder counts as 0.

    Returns:
        Total bytes.
    """
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            with contextlib.suppress(OSError):  # removed while walking
                total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


class ModelLibrary:
    """The model folders under ``$OW_ROOT/shared/models``."""

    def __init__(self, root: Path) -> None:
        """Create the library.

        Args:
            root: The models folder.
        """
        self.root = root

    def path_for(self, repo: str) -> Path:
        """Return the folder a model is (or will be) downloaded to.

        Args:
            repo: Validated model id.

        Returns:
            ``<root>/<org>/<name>``.
        """
        return self.root / repo

    def models(self) -> list[LocalModel]:
        """List the downloaded models: folders ``<org>/<name>`` that contain a ``config.json``.

        Returns:
            The models, sorted by id.
        """
        found = []
        if not self.root.is_dir():
            return found
        for org in sorted(self.root.iterdir()):
            if not org.is_dir() or org.name.startswith("."):
                continue
            for folder in sorted(org.iterdir()):
                config = folder / "config.json"
                if folder.is_dir() and config.is_file():
                    found.append(
                        LocalModel(
                            repo=f"{org.name}/{folder.name}",
                            path=folder,
                            bytes=folder_bytes(folder),
                            modified=datetime.fromtimestamp(config.stat().st_mtime, UTC),
                        )
                    )
        return found

    def space(self) -> Space:
        """Return the size and free space of the disk the models are on.

        Returns:
            Total, used and free bytes.
        """
        usage = shutil.disk_usage(self.root if self.root.exists() else self.root.parent)
        return Space(usage.total, usage.used, usage.free)

    def delete(self, repo: str) -> None:
        """Delete a model folder, and its organisation folder when that is left empty.

        Args:
            repo: Validated model id.
        """
        path = self.path_for(repo)
        shutil.rmtree(path, ignore_errors=True)
        with contextlib.suppress(OSError):  # other models of the organisation remain
            path.parent.rmdir()
