# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import struct

import pytest

from orbitalwindow import gguf

# The metadata of Qwen/Qwen3-8B-GGUF Q4_K_M that the fit check needs (October 2026).
QWEN3_8B = {
    "general.architecture": "qwen3",
    "qwen3.block_count": 36,
    "qwen3.context_length": 40960,
    "qwen3.embedding_length": 4096,
    "qwen3.attention.head_count": 32,
    "qwen3.attention.head_count_kv": 8,
    "qwen3.attention.key_length": 128,
}


def encode(value) -> bytes:
    """Encode one metadata value with its type.

    Args:
        value: A str, int, or list of str or int.

    Returns:
        The type and the value as GGUF stores them.
    """
    if isinstance(value, str):
        raw = value.encode()
        return struct.pack("<IQ", 8, len(raw)) + raw
    if isinstance(value, int):
        return struct.pack("<II", 4, value)
    if all(isinstance(item, str) for item in value):
        items = b"".join(struct.pack("<Q", len(item)) + item.encode() for item in value)
        return struct.pack("<IIQ", 9, 8, len(value)) + items
    return struct.pack("<IIQ", 9, 4, len(value)) + b"".join(struct.pack("<I", v) for v in value)


def file_bytes(metadata: dict) -> bytes:
    """Build the start of a GGUF file (header and metadata, no tensors).

    Args:
        metadata: Values by key, in file order.

    Returns:
        The bytes.
    """
    out = b"GGUF" + struct.pack("<IQQ", 3, 0, len(metadata))
    for key, value in metadata.items():
        out += struct.pack("<Q", len(key)) + key.encode() + encode(value)
    return out


def test_shape_of_qwen3_8b_matches_its_config():
    """The GGUF metadata gives the same shape as the model's config.json."""
    shape = gguf.shape_from_metadata(gguf.read_metadata(file_bytes(QWEN3_8B)))
    assert shape.kv_per_token == 2 * 36 * 8 * 128 * 2
    assert shape.max_context == 40960


def test_metadata_is_read_in_parts_and_tokenizer_arrays_skipped():
    """A large tokenizer array is skipped; the reader asks for more bytes as it goes."""
    data = file_bytes(
        {"general.architecture": "qwen3", "tokenizer.ggml.tokens": ["tok"] * 5000} | QWEN3_8B
    )
    asked = []

    def more(offset):
        """Return the next 100 bytes of the file.

        Args:
            offset: Where to start.

        Returns:
            The bytes.
        """
        asked.append(offset)
        return data[offset : offset + 100]

    metadata = gguf.read_metadata(data[:100], more)
    assert "tokenizer.ggml.tokens" not in metadata
    assert metadata["qwen3.block_count"] == 36 and len(asked) > 100


def test_per_layer_kv_heads_count_only_layers_with_a_cache():
    """Models that list KV heads per layer have a cache only in the layers with heads."""
    metadata = QWEN3_8B | {"qwen3.attention.head_count_kv": [8, 0, 8, 0]}
    shape = gguf.shape_from_metadata(metadata)
    assert (shape.layers, shape.kv_heads) == (2, 8)


def test_hybrid_models_count_only_full_attention_layers():
    """Qwen3.5-9B: full attention in every 4th of 32 layers, so 8 layers have a KV cache."""
    metadata = {
        "general.architecture": "qwen35",
        "qwen35.block_count": 32,
        "qwen35.context_length": 262144,
        "qwen35.embedding_length": 4096,
        "qwen35.attention.head_count": 16,
        "qwen35.attention.head_count_kv": 4,
        "qwen35.attention.key_length": 256,
        "qwen35.full_attention_interval": 4,
    }
    shape = gguf.shape_from_metadata(metadata)
    assert shape.layers == 8 and shape.kv_per_token == 2 * 8 * 4 * 256 * 2


def test_errors():
    """Other files and cut-off metadata raise GgufError; missing keys give no shape."""
    with pytest.raises(gguf.GgufError, match="not a GGUF"):
        gguf.read_metadata(b"PK\x03\x04" + bytes(20))
    with pytest.raises(gguf.GgufError, match="ends"):
        gguf.read_metadata(file_bytes(QWEN3_8B)[:60])
    assert gguf.shape_from_metadata({"general.architecture": "qwen3"}) is None


def test_files_are_grouped_by_quantisation():
    """Split parts form one model; vision projectors are left out; Q4_K_M is preferred."""
    files = [
        ("Q8_0/Big-Q8_0-00002-of-00002.gguf", 4),
        ("Q8_0/Big-Q8_0-00001-of-00002.gguf", 5),
        ("Big-Q4_K_M.gguf", 6),
        ("Big-Q2_K.gguf", 3),
        ("mmproj-F16.gguf", 1),
        ("MTP/mtp-Big-F16.gguf", 2),
        ("mtp-Big.gguf", 2),
        ("README.md", 1),
    ]
    models = gguf.group_files(files)
    assert [(m.quant, m.name, m.bytes) for m in models] == [
        ("Q2_K", "Big-Q2_K.gguf", 3),
        ("Q4_K_M", "Big-Q4_K_M.gguf", 6),
        ("Q8_0", "Q8_0/Big-Q8_0-00001-of-00002.gguf", 9),
    ]
    assert models[2].files == (
        "Q8_0/Big-Q8_0-00001-of-00002.gguf",
        "Q8_0/Big-Q8_0-00002-of-00002.gguf",
    )
    assert gguf.preferred(models).quant == "Q4_K_M"
    assert gguf.preferred([models[0], models[2]]).quant == "Q8_0"  # skips 2- and 3-bit


def test_shape_of_a_local_file(tmp_path):
    """A downloaded file's shape is read from disk; a broken file gives None."""
    good = tmp_path / "m.gguf"
    good.write_bytes(file_bytes(QWEN3_8B) + bytes(1000))
    assert gguf.shape_of_file(str(good)).layers == 36
    bad = tmp_path / "bad.gguf"
    bad.write_bytes(b"GGUF")
    assert gguf.shape_of_file(str(bad)) is None
