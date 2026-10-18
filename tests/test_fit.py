# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import json
from types import SimpleNamespace

import docker
import httpx2
import pytest

from orbitalwindow import fit
from orbitalwindow.metrics import GpuStat
from orbitalwindow.models import HubClient, LocalModel
from orbitalwindow.suggestions import LlmFit, SuggestionError, pick
from orbitalwindow.users import ValidationError

# config.json of Qwen/Qwen3-8B (the fields that matter), October 2026.
QWEN3_8B = {
    "architectures": ["Qwen3ForCausalLM"],
    "num_hidden_layers": 36,
    "num_attention_heads": 32,
    "num_key_value_heads": 8,
    "head_dim": 128,
    "hidden_size": 4096,
    "max_position_embeddings": 40960,
    "torch_dtype": "bfloat16",
}
GPU_16 = fit.Gpu("RTX 5080", 16e9, 15e9)


def test_shape_and_kv_cache_per_token():
    """Qwen3-8B: 2 (K and V) x 36 layers x 8 KV heads x 128 dims x 2 bytes per token."""
    shape = fit.shape_from_config(QWEN3_8B)
    assert shape.kv_per_token == 2 * 36 * 8 * 128 * 2 == 147456
    assert shape.max_context == 40960


def test_shape_from_nested_text_config_and_defaults():
    """Multimodal configs keep the language model in text_config; head_dim and KV heads default."""
    config = {
        "architectures": ["Mistral3ForConditionalGeneration"],
        "text_config": {"num_hidden_layers": 4, "num_attention_heads": 8, "hidden_size": 512},
    }
    shape = fit.shape_from_config(config)
    assert (shape.layers, shape.kv_heads, shape.head_dim, shape.kv_bytes) == (4, 8, 64, 2)
    assert fit.shape_from_config({"architectures": ["X"]}) is None


def test_hybrid_models_count_only_full_attention_layers():
    """Linear attention layers (Qwen3.5) have no KV cache."""
    kinds = ["linear_attention"] * 3 + ["full_attention"]
    config = {
        "model_type": "qwen3_5",
        "text_config": {
            "num_hidden_layers": 32,
            "num_attention_heads": 16,
            "num_key_value_heads": 4,
            "head_dim": 256,
            "layer_types": kinds * 8,
        },
    }
    shape = fit.shape_from_config(config)
    assert shape.layers == 8 and shape.kv_per_token == 2 * 8 * 4 * 256 * 2


def test_estimate_parts_and_total():
    """Weights + cache for 32k tokens + overhead; the context is capped at the model's maximum."""
    estimate = fit.estimate(16.4e9, QWEN3_8B)
    assert estimate.context == 32768
    assert estimate.kv_cache == 147456 * 32768
    assert estimate.overhead == pytest.approx(1.5e9 + 0.05 * 16.4e9)
    assert estimate.total == pytest.approx(16.4e9 + 147456 * 32768 + 1.5e9 + 0.82e9)
    assert fit.estimate(1e9, QWEN3_8B, context=10**6).context == 40960


QWEN35_4B = {
    "text_config": {
        "num_hidden_layers": 32,
        "num_attention_heads": 16,
        "num_key_value_heads": 4,
        "head_dim": 256,
        "layer_types": ["linear_attention"] * 3 * 8 + ["full_attention"] * 8,
        "max_position_embeddings": 262144,
    }
}


@pytest.mark.parametrize(
    ("weights", "config", "gpu_gib", "expected"),
    [
        (9.3e9, QWEN35_4B, 16, 65536),  # plenty of room: the suggested 64k
        (9.3e9, QWEN35_4B, None, 65536),  # no GPU information
        (1e9, QWEN3_8B, 80, 40960),  # the model's own maximum
        (16.4e9, QWEN3_8B, 24, 29696),  # what fits, in whole thousands
        (16.4e9, QWEN3_8B, 16, 32768),  # not even the weights fit: plan for the default
        (4e9, None, 16, 32768),  # unknown shape
    ],
)
def test_suggest_context(weights, config, gpu_gib, expected):
    """Sessions start with 64k tokens, or less when the model or the GPU allows less."""
    estimate = fit.estimate(weights, config)
    gpu = gpu_gib * fit.GIB if gpu_gib else None
    assert fit.suggest_context(estimate, gpu) == expected


def test_estimate_without_config_counts_weights_and_overhead():
    """Without a configuration the cache is unknown and left out."""
    estimate = fit.estimate(4e9, None)
    assert estimate.kv_cache is None and estimate.total == pytest.approx(4e9 + 1.5e9 + 0.2e9)
    assert estimate.max_context(16e9) is None


def test_max_context():
    """The longest context fills 90 % of the GPU after weights and overhead, up to the maximum."""
    small = fit.estimate(1.5e9, QWEN3_8B)
    assert small.max_context(16e9) == 40960
    big = fit.estimate(13e9, QWEN3_8B)
    room = 16e9 * 0.9 - 13e9 - (1.5e9 + 0.65e9)
    assert big.max_context(16e9) == max(int(room // 147456), 0) == 0


@pytest.mark.parametrize(
    ("needed", "gpu", "level"),
    [
        (10e9, GPU_16, "fits"),
        (10e9, fit.Gpu("RTX 5080", 16e9, 5e9), "busy"),
        (15e9, GPU_16, "tight"),
        (17e9, GPU_16, "too-big"),
        (1e9, None, "unknown"),
    ],
)
def test_verdicts(needed, gpu, level):
    """Fits within 90 % (busy if not free now), tight up to 100 %, else too big."""
    assert fit.verdict(needed, gpu).level == level


# --- suggestions ------------------------------------------------------------------------------

LLMFIT_ANSWER = {
    "models": [
        {"name": "Qwen/Qwen3-8B", "params_b": 8.19, "category": "General", "score": 90},
        {"name": "Qwen/Qwen3-8B", "params_b": 8.19, "category": "General", "score": 89},
        {"name": "mlx-community/Qwen3-8B-4bit", "score": 99},
        {"name": "random-user/uncensored-8B", "score": 98},
        {"name": "unsloth/Qwen3-8B-GGUF", "score": 97},
        {"name": "Qwen/Qwen3-8B-GGUF", "score": 96},
        {"name": "google/gemma-3-4b-it", "params_b": 4.3, "license": "gemma", "score": 95},
    ]
}


def test_pick_keeps_known_publishers_best_first():
    """Only well-known publishers, no GGUF/MLX, each name once, best score first."""
    picked = pick(LLMFIT_ANSWER, 32 * 1024**3)
    assert [item.repo for item in picked] == ["google/gemma-3-4b-it", "Qwen/Qwen3-8B"]
    assert picked[0].license == "gemma" and picked[1].params_b == 8.19
    assert len(pick(LLMFIT_ANSWER, 32 * 1024**3, limit=1)) == 1


def test_pick_drops_models_too_big_for_vllm():
    """An 8B model in 16-bit doesn't fit 16 GB; its FP8 version does."""
    answer = {
        "models": [
            {"name": "Qwen/Qwen3-8B", "params_b": 8.19, "score": 90},
            {"name": "Qwen/Qwen3-8B-FP8", "params_b": 8.19, "score": 89},
            {"name": "Qwen/Qwen3-4B", "params_b": 4.02, "score": 88},
        ]
    }
    picked = [item.repo for item in pick(answer, 16 * 1024**3)]
    assert picked == ["Qwen/Qwen3-8B-FP8", "Qwen/Qwen3-4B"]


class FakeLlmFitClient:
    def __init__(self, output=b"", fail=None, have_image=True):
        """Script the container output, an optional error and whether the image exists."""
        self.output, self.fail, self.have_image = output, fail, have_image
        self.runs, self.pulls = [], []
        self.containers = SimpleNamespace(run=self._run)
        self.images = SimpleNamespace(get=self._get, pull=self.pulls.append)

    def _get(self, image):
        """Raise ImageNotFound unless the image is there."""
        if not self.have_image:
            raise docker.errors.ImageNotFound("missing")

    def _run(self, image, command, **kwargs):
        """Record the run and return the scripted output."""
        self.runs.append((image, command, kwargs))
        if self.fail:
            raise self.fail
        return self.output


def test_llmfit_runs_offline_once_per_hour_and_pulls_when_missing():
    """llmfit runs without network for the GPU size; a second call within the hour is cached."""
    client = FakeLlmFitClient(json.dumps(LLMFIT_ANSWER).encode(), have_image=False)
    runner = LlmFit(client, "ghcr.io/alexsjones/llmfit:1.1.15")
    assert [item.repo for item in runner.suggest(32e9)][0] == "google/gemma-3-4b-it"
    runner.suggest(32.4e9)
    assert len(client.runs) == 1 and client.pulls == ["ghcr.io/alexsjones/llmfit:1.1.15"]
    image, command, kwargs = client.runs[0]
    assert command[:3] == ["--json", "--memory", "30G"] and kwargs["network_mode"] == "none"
    assert kwargs["cap_drop"] == ["ALL"]


def test_llmfit_errors():
    """Docker errors and unreadable output become SuggestionError."""
    with pytest.raises(SuggestionError, match="could not run"):
        LlmFit(FakeLlmFitClient(fail=docker.errors.APIError("boom")), "x").suggest(16e9)
    with pytest.raises(SuggestionError, match="can't be read"):
        LlmFit(FakeLlmFitClient(b"not json"), "x").suggest(16e9)


# --- Hub config, local models and the service ---------------------------------------------------


def test_hub_config_fetch():
    """config.json is read with the token; errors give None."""
    seen = {}

    def handler(request):
        """Answer the config request, or 401 for other repos."""
        seen["auth"] = request.headers.get("authorization")
        if request.url.path == "/Qwen/Qwen3-8B/resolve/abc/config.json":
            return httpx2.Response(200, json=QWEN3_8B)
        return httpx2.Response(401)

    hub = HubClient(
        client=httpx2.Client(base_url="https://hub.test", transport=httpx2.MockTransport(handler))
    )
    assert hub.config("Qwen/Qwen3-8B", "abc", "hf_x")["num_hidden_layers"] == 36
    assert seen["auth"] == "Bearer hf_x"
    assert hub.config("google/gemma-3-12b-it", "main", None) is None


def test_service_fit_for_local_model_and_gpu_choice(service, tmp_path, monkeypatch):
    """The largest GPU is used; a downloaded model's fit comes from its files and config."""
    monkeypatch.setattr(
        service.gpus,
        "stats",
        lambda: [GpuStat(0, "small", 0, 1_000_000_000, 8e9), GpuStat(1, "big", 0, 4e9, 32e9)],
    )
    gpu = service.model_gpu()
    assert gpu.name == "big" and gpu.total == 32e9 and gpu.free == 28e9
    folder = tmp_path / "Qwen" / "Qwen3-8B"
    folder.mkdir(parents=True)
    (folder / "config.json").write_text(json.dumps(QWEN3_8B))
    (folder / "model.safetensors").write_bytes(b"x" * 1000)
    model = LocalModel("Qwen/Qwen3-8B", folder, 1100, None)
    estimate, verdict = service.fit_for_local(model)
    assert estimate.weights == 1000 and estimate.shape.layers == 36
    assert verdict.level == "fits"


def test_suggestions_need_a_gpu(service, monkeypatch):
    """Without GPU information there are no suggestions."""
    monkeypatch.setattr(service.gpus, "stats", lambda: [])
    with pytest.raises(ValidationError, match="NVIDIA"):
        service.model_suggestions()


def test_chat_template_from_file_or_tokenizer_config(tmp_path):
    """The chat template comes from chat_template.jinja, else tokenizer_config.json."""
    model = LocalModel("a/b", tmp_path, 0, None)
    assert model.chat_template() == ""
    (tmp_path / "tokenizer_config.json").write_text(json.dumps({"chat_template": "{{ old }}"}))
    assert model.chat_template() == "{{ old }}"
    (tmp_path / "chat_template.jinja").write_text("{{ new }}")
    assert model.chat_template() == "{{ new }}"


def test_kv_limit_shortens_the_context_and_the_verdict():
    """An Arc A-series KV limit caps the suggested context and makes a long one only 'tight'."""
    from orbitalwindow.aisession import XE_HPG_KV_BYTES

    arc = fit.Gpu("Intel Arc A770", 17e9, 16e9, XE_HPG_KV_BYTES)
    planned = fit.estimate(8e9, QWEN3_8B)
    # 3.75 GiB / 147,456 bytes per token = 27,306 tokens, rounded down to 26,624.
    assert fit.suggest_context(planned, arc.total, arc.kv_limit) == 26 * 1024
    assert planned.max_context(arc.total, arc.kv_limit) == 27306
    long = fit.estimate(8e9, QWEN3_8B, 32768)
    assert fit.verdict(long.total, arc, long.kv_cache).level == "tight"
    short = fit.estimate(8e9, QWEN3_8B, 16384)
    assert fit.verdict(short.total, arc, short.kv_cache).level == "fits"
