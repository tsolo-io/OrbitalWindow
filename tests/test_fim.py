# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Code completion (FIM) support of a model (#79)."""

import json
from datetime import UTC, datetime

from test_gguf import file_bytes

from orbitalwindow import fim
from orbitalwindow.models import LocalModel

QWEN = ["<|endoftext|>", "<|fim_prefix|>", "<|fim_middle|>", "<|fim_suffix|>", "<|fim_pad|>"]


def test_states_from_name_and_vocabulary():
    """A coder model with FIM tokens, a general one with them, and one without."""
    assert fim.detect("Qwen/Qwen2.5-Coder-3B", set(QWEN)) == fim.FimSupport("coder", "qwen")
    assert fim.detect("google/codegemma-2b", set(QWEN)).state == "coder"
    assert fim.detect("Qwen/Qwen3-8B", set(QWEN)) == fim.FimSupport("tokens", "qwen")
    assert fim.detect("bigcode/starcoder2-3b", {"<fim_prefix>", "<fim_suffix>", "<fim_middle>"})\
        == fim.FimSupport("coder", "starcoder")  # fmt: skip
    assert fim.detect("unsloth/gpt-oss-20b", {"<|start|>", "<|fim_prefix|>"}).state == "none"
    prefix_suffix_middle = ("<|fim_prefix|>", "<|fim_suffix|>", "<|fim_middle|>")
    assert fim.FimSupport("coder", "qwen").tokens == prefix_suffix_middle
    assert fim.FimSupport("none").tokens is None


def test_vocabulary_of_a_gguf_file(tmp_path):
    """The tokens are read from the GGUF metadata."""
    path = tmp_path / "model.gguf"
    path.write_bytes(file_bytes({"general.architecture": "qwen2", "tokenizer.ggml.tokens": QWEN}))
    assert fim.gguf_vocabulary(path) == set(QWEN)
    assert fim.gguf_vocabulary(tmp_path / "missing.gguf") == set()


def test_vocabulary_of_a_hugging_face_model(tmp_path):
    """The added tokens of tokenizer_config.json and tokenizer.json both count."""
    (tmp_path / "tokenizer_config.json").write_text(
        json.dumps({"added_tokens_decoder": {"1": {"content": "<|fim_prefix|>"}}})
    )
    (tmp_path / "tokenizer.json").write_text(
        json.dumps({"added_tokens": [{"content": "<|fim_suffix|>"}, {"content": "<|fim_middle|>"}]})
    )
    assert fim.hf_vocabulary(tmp_path) == {"<|fim_prefix|>", "<|fim_suffix|>", "<|fim_middle|>"}
    assert fim.hf_vocabulary(tmp_path / "none") == set()


def test_service_detects_caches_and_lets_admins_override(service, tmp_path, monkeypatch):
    """The files decide unless an admin chose; the result is cached per file."""
    folder = tmp_path / "Qwen" / "Qwen3-8B-GGUF"
    folder.mkdir(parents=True)
    (folder / "Qwen3-8B-Q4_K_M.gguf").write_bytes(file_bytes({"tokenizer.ggml.tokens": QWEN}))
    model = LocalModel("Qwen/Qwen3-8B-GGUF", folder, 1, datetime.now(UTC))
    monkeypatch.setattr(service.models, "models", lambda: [model])
    reads = []
    real = fim.gguf_vocabulary
    monkeypatch.setattr(fim, "gguf_vocabulary", lambda path: reads.append(path) or real(path))
    assert service.fim_support("Qwen/Qwen3-8B-GGUF") == fim.FimSupport("tokens", "qwen")
    service.fim_support("Qwen/Qwen3-8B-GGUF")
    assert len(reads) == 1
    service.set_fim_support("boss", "Qwen/Qwen3-8B-GGUF", "coder")
    assert service.fim_support("Qwen/Qwen3-8B-GGUF") == fim.FimSupport("coder", "qwen", True)
    service.set_fim_support("boss", "Qwen/Qwen3-8B-GGUF", "auto")
    assert service.fim_support("Qwen/Qwen3-8B-GGUF").state == "tokens"
    assert service.fim_support("no/such-model").state == "none"
    actions = service.db.all("SELECT detail FROM audit WHERE action = 'ai.fim' ORDER BY id")
    assert [row["detail"] for row in actions] == ["coder", "auto"]
