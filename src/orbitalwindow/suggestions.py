# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Model suggestions from llmfit (https://github.com/AlexsJones/llmfit), run as its own image.

llmfit releases often, so it is not bundled: a short-lived container of the image in
``OW_LLMFIT_IMAGE`` runs ``llmfit recommend`` for the GPU's memory, without network access.
llmfit scores models for llama.cpp at a quantisation that fits, so its sizes don't apply to vLLM;
the suggestions are ideas, and Inspect gives the real fit. Only models of well-known publishers
are kept: the full list is mostly re-uploads and community fine-tunes.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import docker

from . import fit

log = logging.getLogger(__name__)

PUBLISHERS = frozenset(
    {
        "Qwen",
        "meta-llama",
        "google",
        "mistralai",
        "microsoft",
        "deepseek-ai",
        "ibm-granite",
        "allenai",
        "nvidia",
        "HuggingFaceTB",
        "openai",
        "CohereLabs",
        "tiiuae",
        "LiquidAI",
        "zai-org",
        "moonshotai",
        "swiss-ai",
        "utter-project",
    }
)
SKIP_WORDS = ("gguf", "mlx", "bnb")  # formats vLLM can't load (or loads poorly)
# Bytes per parameter by name hint; plain repos hold 16-bit weights.
QUANT_HINTS = (("fp8", 1.0), ("awq", 0.55), ("gptq", 0.55), ("int4", 0.55), ("nvfp4", 0.55))
CACHE_SECONDS = 3600
LIMIT = 20


class SuggestionError(Exception):
    """llmfit could not be run or gave an answer that can't be read."""


@dataclass(frozen=True)
class Suggestion:
    repo: str
    params_b: float | None
    category: str
    use_case: str
    license: str
    score: float


def rough_bytes(name: str, params_b: float) -> float:
    """Roughly estimate the GPU memory a model needs in vLLM, from its name and size.

    Args:
        name: Model id; quantisation hints such as ``FP8`` or ``AWQ`` lower the bytes per weight.
        params_b: Parameters in billions.

    Returns:
        Bytes: weights, plus the same overhead as ``fit.estimate`` and about 1 GB of cache.
    """
    per_param = next((size for hint, size in QUANT_HINTS if hint in name.lower()), 2.0)
    weights = params_b * 1e9 * per_param
    return weights + fit.OVERHEAD_BYTES + fit.OVERHEAD_SHARE * weights + 1e9


def pick(data: dict[str, Any], gpu_bytes: float, limit: int = LIMIT) -> list[Suggestion]:
    """Keep the best-scored models of well-known publishers that can run on the GPU.

    llmfit sizes models for llama.cpp at a quantisation that fits; vLLM loads the weights as they
    are, so models whose own weights are too big for the GPU are dropped.

    Args:
        data: The decoded ``llmfit --json recommend`` output.
        gpu_bytes: Memory of the GPU.
        limit: Maximum number of suggestions.

    Returns:
        Suggestions, best score first, one per model name.
    """
    seen: set[str] = set()
    picked: list[Suggestion] = []
    models = sorted(data.get("models") or [], key=lambda item: -float(item.get("score") or 0))
    for model in models:
        name = str(model.get("name") or "")
        publisher = name.split("/", 1)[0]
        if (
            publisher not in PUBLISHERS
            or name in seen
            or any(word in name.lower() for word in SKIP_WORDS)
        ):
            continue
        seen.add(name)
        params = model.get("params_b")
        if params is not None and rough_bytes(name, float(params)) > gpu_bytes * fit.GPU_SHARE:
            continue
        picked.append(
            Suggestion(
                repo=name,
                params_b=float(params) if params is not None else None,
                category=str(model.get("category") or ""),
                use_case=str(model.get("use_case") or ""),
                license=str(model.get("license") or ""),
                score=float(model.get("score") or 0),
            )
        )
        if len(picked) >= limit:
            break
    return picked


class LlmFit:
    def __init__(self, client: docker.DockerClient, image: str) -> None:
        """Create the runner.

        Args:
            client: Docker client.
            image: llmfit image with tag, e.g. ``ghcr.io/alexsjones/llmfit:1.1.15``.
        """
        self._client = client
        self.image = image
        self._lock = threading.Lock()
        self._cache: dict[int, tuple[float, list[Suggestion]]] = {}

    def suggest(self, gpu_bytes: float) -> list[Suggestion]:
        """Return suggestions for a GPU size, from the cache if they are less than an hour old.

        Args:
            gpu_bytes: Memory of the GPU the model would run on.

        Returns:
            The suggestions.

        Raises:
            SuggestionError: If llmfit can't be run or its answer can't be read.
        """
        gigabytes = max(round(gpu_bytes / 1024**3), 1)
        with self._lock:
            cached = self._cache.get(gigabytes)
            if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
                return cached[1]
            picked = pick(self._run(gigabytes), gpu_bytes)
            self._cache[gigabytes] = (time.monotonic(), picked)
            return picked

    def _run(self, gigabytes: int) -> dict[str, Any]:
        """Run ``llmfit recommend`` in a throw-away container.

        Args:
            gigabytes: GPU memory to score for.

        Returns:
            The decoded JSON answer.

        Raises:
            SuggestionError: If the image can't be pulled, llmfit fails or answers badly.
        """
        command = [
            "--json", "--memory", f"{gigabytes}G",
            "recommend", "-n", "2000", "--runtime", "llamacpp",
        ]  # fmt: skip
        try:
            try:
                self._client.images.get(self.image)
            except docker.errors.ImageNotFound:
                log.info("pulling %s", self.image)
                self._client.images.pull(self.image)
            output = self._client.containers.run(
                self.image,
                command,
                remove=True,
                network_mode="none",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                labels={"ow.managed": "true", "ow.role": "llmfit"},
            )
        except docker.errors.DockerException as exc:
            raise SuggestionError(f"llmfit could not run: {exc}") from exc
        try:
            return json.loads(output)
        except (ValueError, TypeError) as exc:
            raise SuggestionError("llmfit gave an answer that can't be read") from exc
