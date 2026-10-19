# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""GGUF files (llama.cpp's model format): which files make up a model, and its attention shape.

A GGUF repository on Hugging Face usually holds one file per quantisation (``Q4_K_M``, ``Q8_0``,
…); large ones are split into ``…-00001-of-00003.gguf`` parts, sometimes in a folder per
quantisation. The fit check needs the KV cache per token, which is in the metadata at the start
of the (first) file: the reader parses only that part, so a few MB fetched from the Hub suffice.
"""

import re
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .fit import Shape

MAGIC = b"GGUF"
SPLIT_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$")
# Vision projectors, importance matrices and multi-token-prediction drafts (MTP) are helper files,
# not models of their own.
SKIP_RE = re.compile(r"(^|/)(mmproj|imatrix|mtp)[^/]*$|(^|/)mtp/", re.IGNORECASE)
QUANT_RE = re.compile(r"(?:^|[-_.])((?:I?Q\d[A-Z0-9_]*)|BF16|F16|F32)(?:[-_.]|$)", re.IGNORECASE)
PREFERRED = "Q4_K_M"  # the usual balance of size and quality
KV_BYTES = 2  # llama.cpp keeps the KV cache in float16 by default
# llama.cpp's sliding-window cache: one window per request slot (aisession.LLAMACPP_SLOTS) plus a
# batch (n_ubatch), rounded up to 256 cells. gpt-oss-20b: 128 × 4 + 512 = 1,024 cells.
SWA_SEQS = 4
SWA_BATCH = 512
# llama.cpp's built-in sliding-window periods for files without sliding_window_pattern.
SWA_PATTERNS = {"gpt-oss": 2, "gemma2": 2, "gemma3": 6, "cohere2": 4}
# Metadata value types: struct format and size of the fixed-size ones.
SCALARS = {
    0: ("<B", 1),
    1: ("<b", 1),
    2: ("<H", 2),
    3: ("<h", 2),
    4: ("<I", 4),
    5: ("<i", 4),
    6: ("<f", 4),
    7: ("<?", 1),
    10: ("<Q", 8),
    11: ("<q", 8),
    12: ("<d", 8),
}
STRING, ARRAY = 8, 9


class GgufError(Exception):
    """The data is not a GGUF file, or it ends before the needed metadata."""


@dataclass(frozen=True)
class GgufModel:
    name: str  # the file to load: the only file, or the first part of a split model
    quant: str  # e.g. Q4_K_M; empty when the name doesn't say
    files: tuple[str, ...]  # every file of the model
    bytes: int


def group_files(files: list[tuple[str, int]]) -> list[GgufModel]:
    """Group a repository's GGUF files into models (one per quantisation).

    Args:
        files: (path, bytes) of the repository's files.

    Returns:
        The models, smallest first; helper files (vision projectors) are left out.
    """
    groups: dict[str, list[tuple[str, int]]] = {}
    for path, size in files:
        if not path.endswith(".gguf") or SKIP_RE.search(path):
            continue
        groups.setdefault(SPLIT_RE.sub(".gguf", path), []).append((path, size))
    models = []
    for key, parts in groups.items():
        parts.sort()
        match = QUANT_RE.search(PurePosixPath(key).stem.split(".")[-1]) or QUANT_RE.search(key)
        models.append(
            GgufModel(
                name=parts[0][0],
                quant=match.group(1).upper() if match else "",
                files=tuple(path for path, _ in parts),
                bytes=sum(size for _, size in parts),
            )
        )
    return sorted(models, key=lambda model: (model.bytes, model.name))


def preferred(models: list[GgufModel]) -> GgufModel | None:
    """Pick the model to offer first: ``PREFERRED`` if there is one, else the smallest over 4 bits.

    Args:
        models: The repository's models, from ``group_files``.

    Returns:
        The model, or None for an empty list.
    """
    for model in models:
        if model.quant == PREFERRED:
            return model
    return next((m for m in models if not m.quant.startswith(("Q2", "Q3", "IQ"))), None) or (
        models[0] if models else None
    )


class _Reader:
    """Reads values from a buffer that ``more`` can extend (for example by an HTTP range)."""

    def __init__(self, data: bytes, more: Callable[[int], bytes] | None) -> None:
        """Create the reader.

        Args:
            data: The bytes at the start of the file.
            more: Returns the bytes from a given offset on (b"" at the end), or None.
        """
        self.data = data
        self.pos = 0
        self._more = more

    def take(self, size: int) -> bytes:
        """Return the next bytes, fetching more if needed.

        Args:
            size: How many bytes.

        Returns:
            The bytes.

        Raises:
            GgufError: If the data ends first.
        """
        while self.pos + size > len(self.data):
            extra = self._more(len(self.data)) if self._more else b""
            if not extra:
                raise GgufError("the file ends inside its metadata")
            self.data += extra
        chunk = self.data[self.pos : self.pos + size]
        self.pos += size
        return chunk

    def unpack(self, fmt: str, size: int) -> Any:
        """Read one fixed-size value.

        Args:
            fmt: ``struct`` format.
            size: Its size in bytes.

        Returns:
            The value.
        """
        return struct.unpack(fmt, self.take(size))[0]

    def string(self) -> str:
        """Read a length-prefixed string.

        Returns:
            The string.
        """
        return self.take(self.unpack("<Q", 8)).decode("utf-8", errors="replace")

    def value(self, kind: int, keep: bool) -> Any:
        """Read (or skip) one metadata value.

        Args:
            kind: The value type.
            keep: False to skip it (arrays of the tokenizer are large).

        Returns:
            The value, or None when skipped.

        Raises:
            GgufError: For an unknown value type.
        """
        if kind in SCALARS:
            return self.unpack(*SCALARS[kind])
        if kind == STRING:
            return self.string()
        if kind == ARRAY:
            item, count = self.unpack("<I", 4), self.unpack("<Q", 8)
            if item in SCALARS and not keep:
                self.take(SCALARS[item][1] * count)
                return None
            values = [self.value(item, keep) for _ in range(count)]
            return values if keep else None
        raise GgufError(f"unknown metadata type {kind}")


def read_metadata(
    data: bytes,
    more: Callable[[int], bytes] | None = None,
    wanted: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Parse the metadata at the start of a GGUF file.

    Args:
        data: The first bytes of the file.
        more: Returns further bytes from an offset (b"" at the end); None if ``data`` is all.
        wanted: Tells which keys to keep; None keeps all but the ``tokenizer.`` arrays.

    Returns:
        The kept metadata by key.

    Raises:
        GgufError: If it is not a GGUF file or the data ends early.
    """
    reader = _Reader(data, more)
    if reader.take(4) != MAGIC:
        raise GgufError("not a GGUF file")
    version = reader.unpack("<I", 4)
    if version < 2:
        raise GgufError(f"GGUF version {version} is too old")
    reader.unpack("<Q", 8)  # tensor count
    count = reader.unpack("<Q", 8)
    metadata: dict[str, Any] = {}
    for _ in range(count):
        key = reader.string()
        kind = reader.unpack("<I", 4)
        keep = wanted(key) if wanted else not key.startswith("tokenizer.")
        value = reader.value(kind, keep)
        if keep:
            metadata[key] = value
    return metadata


def shape_from_metadata(metadata: dict[str, Any]) -> Shape | None:
    """Read the attention shape that the KV cache size depends on, layer by layer.

    Full-attention layers cache every token of the context. Sliding-window layers keep only
    ``SWA_SEQS`` windows and a batch, as llama.cpp sizes that cache. Linear-attention layers of
    hybrid models (Qwen3.5) and layers that reuse another layer's cache keep nothing that grows.

    Args:
        metadata: The file's metadata.

    Returns:
        The shape, or None if the metadata lacks the needed keys.
    """
    arch = metadata.get("general.architecture")

    def key(name: str, default: Any = None) -> Any:
        """Return an architecture key of the metadata.

        Args:
            name: The key without the architecture, e.g. ``attention.key_length``.
            default: Returned when the key is missing.

        Returns:
            The value.
        """
        return metadata.get(f"{arch}.{name}", default)

    try:
        count = int(key("block_count"))
        heads = key("attention.head_count")
        if isinstance(heads, list):
            heads = max(int(value) for value in heads)
        kv_heads = key("attention.head_count_kv", heads)
        k_full = int(key("attention.key_length") or int(key("embedding_length")) // int(heads))
        v_full = int(key("attention.value_length") or k_full)
        k_swa = int(key("attention.key_length_swa") or k_full)
        v_swa = int(key("attention.value_length_swa") or v_full)
        window = int(key("attention.sliding_window") or 0)
        pattern = key("attention.sliding_window_pattern", SWA_PATTERNS.get(arch))
        interval = int(key("full_attention_interval") or 0)
        shared = int(key("attention.shared_kv_layers") or 0)
        context = key("context_length")
    except KeyError, TypeError, ValueError, ZeroDivisionError:
        return None
    full_layers = full_bytes = swa_bytes = 0
    full_heads = 0
    for layer in range(count - shared):
        if interval > 1 and (layer + 1) % interval:
            continue  # linear attention (hybrid models)
        if isinstance(kv_heads, list):
            layer_heads = int(kv_heads[layer]) if layer < len(kv_heads) else 0
        else:
            layer_heads = int(kv_heads)
        if not layer_heads:
            continue
        if window and _is_sliding(pattern, layer):
            swa_bytes += layer_heads * (k_swa + v_swa) * KV_BYTES
        else:
            full_layers += 1
            full_heads = max(full_heads, layer_heads)
            full_bytes += layer_heads * (k_full + v_full) * KV_BYTES
    cells = -(-(window * SWA_SEQS + SWA_BATCH) // 256) * 256 if swa_bytes else 0
    return Shape(
        layers=full_layers,
        kv_heads=full_heads,
        head_dim=k_full,
        kv_bytes=KV_BYTES,
        max_context=int(context) if context else None,
        full_bytes=full_bytes,
        swa_bytes=swa_bytes,
        swa_tokens=cells,
    )


def _is_sliding(pattern: Any, layer: int) -> bool:
    """Tell whether a layer uses the sliding window, as llama.cpp decides it.

    Args:
        pattern: ``sliding_window_pattern`` from the file (one bool per layer), or the period
            ``n`` of llama.cpp's built-in pattern (layer ``i`` slides when ``i % n < n - 1``), or
            None (every layer slides, as in Mistral 7B).
        layer: The layer number.

    Returns:
        True for a sliding-window layer.
    """
    if isinstance(pattern, list):
        return bool(pattern[layer]) if layer < len(pattern) else False
    if pattern:
        return layer % int(pattern) < int(pattern) - 1
    return True


def shape_of_file(path: str) -> Shape | None:
    """Read the shape of a local GGUF file.

    Args:
        path: The file (the first part of a split model).

    Returns:
        The shape, or None if the file can't be read or lacks the keys.
    """
    try:
        with open(path, "rb") as file:
            return shape_from_metadata(
                read_metadata(file.read(4 << 20), lambda offset: _read_at(file, offset))
            )
    except OSError, GgufError:
        return None


def _read_at(file: Any, offset: int) -> bytes:
    """Read the next 4 MB of an open file.

    Args:
        file: The open file.
        offset: Where to read from.

    Returns:
        The bytes (b"" at the end).
    """
    file.seek(offset)
    return file.read(4 << 20)
