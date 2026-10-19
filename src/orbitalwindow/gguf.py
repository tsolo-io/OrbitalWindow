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
# Vision projectors and other helper files are not models of their own.
SKIP_RE = re.compile(r"(^|/)(mmproj|imatrix)[^/]*$", re.IGNORECASE)
QUANT_RE = re.compile(r"(?:^|[-_.])((?:I?Q\d[A-Z0-9_]*)|BF16|F16|F32)(?:[-_.]|$)", re.IGNORECASE)
PREFERRED = "Q4_K_M"  # the usual balance of size and quality
KV_BYTES = 2  # llama.cpp keeps the KV cache in float16 by default
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
    """Read the attention shape that the KV cache size depends on.

    Args:
        metadata: The file's metadata.

    Returns:
        The shape, or None if the metadata lacks the needed keys.
    """
    arch = metadata.get("general.architecture")
    try:
        layers = int(metadata[f"{arch}.block_count"])
        heads = metadata[f"{arch}.attention.head_count"]
        kv_heads = metadata.get(f"{arch}.attention.head_count_kv", heads)
        # Hybrid models (Qwen3.5, Qwen3-Next) have full attention only in every n-th layer; the
        # others use linear attention, whose state doesn't grow with the context.
        interval = metadata.get(f"{arch}.full_attention_interval")
        if interval and int(interval) > 1:
            layers //= int(interval)
        # Some models list the heads per layer; layers without KV heads have no cache.
        if isinstance(kv_heads, list):
            with_cache = [int(count) for count in kv_heads if count]
            layers, kv_heads = len(with_cache), max(with_cache, default=0)
        if isinstance(heads, list):
            heads = max(int(count) for count in heads)
        head_dim = metadata.get(f"{arch}.attention.key_length") or (
            int(metadata[f"{arch}.embedding_length"]) // int(heads)
        )
        context = metadata.get(f"{arch}.context_length")
        return Shape(
            layers=layers,
            kv_heads=int(kv_heads),
            head_dim=int(head_dim),
            kv_bytes=KV_BYTES,
            max_context=int(context) if context else None,
        )
    except KeyError, TypeError, ValueError, ZeroDivisionError:
        return None


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
