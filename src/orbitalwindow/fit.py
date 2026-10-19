# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Will a model fit on the GPU? A GPU memory estimate for serving it with vLLM.

Needed memory = weights (the safetensors files, loaded as they are) + KV cache for the context
length + a fixed overhead (CUDA context, activations, CUDA graphs). vLLM uses at most
``GPU_SHARE`` of a GPU by default, so a model "fits" when the total is within that share.
The KV cache ignores sliding-window attention, so the estimate errs on the large side.
"""

from dataclasses import dataclass
from typing import Any

# The fit check plans for the least coding tools need: OpenCode's first request alone is about
# 7,500 tokens, and the conversation grows from there (ADR 0031).
CODING_CONTEXT = 32_768
DEFAULT_CONTEXT = CODING_CONTEXT
SUGGESTED_CONTEXT = 65_536  # the session's default when it fits
GPU_SHARE = 0.9  # vLLM's default --gpu-memory-utilization
OVERHEAD_BYTES = 1.5e9
OVERHEAD_SHARE = 0.05  # of the weights: activations grow with the model
DTYPE_BYTES = {"float32": 4, "float16": 2, "bfloat16": 2}
GIB = 1024**3  # GPU memory is shown in binary units, as nvidia-smi and the admin page do


@dataclass(frozen=True)
class Shape:
    """What the KV cache size depends on, from the model's ``config.json``."""

    layers: int  # layers with a KV cache (hybrid models: only the full attention ones)
    kv_heads: int
    head_dim: int
    kv_bytes: int  # per value
    max_context: int | None

    @property
    def kv_per_token(self) -> int:
        """Return the KV cache size of one token (keys and values, all layers).

        Returns:
            Bytes per token.
        """
        return 2 * self.layers * self.kv_heads * self.head_dim * self.kv_bytes


def shape_from_config(config: dict[str, Any]) -> Shape | None:
    """Read the attention shape from a model configuration.

    Multimodal models keep the language model's settings in ``text_config``. Layers listed as
    anything but ``full_attention`` in ``layer_types`` have no KV cache.

    Args:
        config: The parsed ``config.json``.

    Returns:
        The shape, or None if the configuration lacks the needed fields.
    """
    text = config.get("text_config") or config
    try:
        layers = int(text["num_hidden_layers"])
        # Hybrid models (Qwen3.5, Qwen3-Next) mix full attention with linear attention layers,
        # whose state doesn't grow with the context: only the full attention layers count.
        kinds = text.get("layer_types")
        if isinstance(kinds, list) and "full_attention" in kinds:
            layers = kinds.count("full_attention")
        heads = int(text["num_attention_heads"])
        kv_heads = int(text.get("num_key_value_heads") or heads)
        head_dim = int(text.get("head_dim") or int(text["hidden_size"]) // heads)
    except KeyError, TypeError, ValueError, ZeroDivisionError:
        return None
    dtype = str(text.get("torch_dtype") or text.get("dtype") or config.get("torch_dtype") or "")
    max_context = text.get("max_position_embeddings")
    return Shape(
        layers=layers,
        kv_heads=kv_heads,
        head_dim=head_dim,
        kv_bytes=DTYPE_BYTES.get(dtype, 2),
        max_context=int(max_context) if max_context else None,
    )


@dataclass(frozen=True)
class Estimate:
    weights: float
    kv_cache: float | None  # None when the model's shape is unknown
    overhead: float
    context: int
    shape: Shape | None

    @property
    def total(self) -> float:
        """Return the GPU memory needed.

        Returns:
            Bytes; without a known shape, weights and overhead only.
        """
        return self.weights + (self.kv_cache or 0) + self.overhead

    def max_context(self, gpu_bytes: float) -> int | None:
        """Return the longest context whose KV cache fits next to the weights.

        Args:
            gpu_bytes: Total memory of the GPU.

        Returns:
            Tokens (capped at the model's maximum), 0 if not even the weights fit, or None
            without a known shape.
        """
        if self.shape is None:
            return None
        room = gpu_bytes * GPU_SHARE - self.weights - self.overhead
        tokens = max(int(room // self.shape.kv_per_token), 0)
        return min(tokens, self.shape.max_context) if self.shape.max_context else tokens


def estimate(
    weights: float, config: dict[str, Any] | None, context: int = DEFAULT_CONTEXT
) -> Estimate:
    """Estimate the GPU memory for serving a model.

    Args:
        weights: Size of the weight files in bytes.
        config: The model's ``config.json``, or None if it couldn't be read.
        context: Context length to plan the KV cache for (capped at the model's maximum).

    Returns:
        The estimate.
    """
    shape = shape_from_config(config) if config else None
    if shape is not None and shape.max_context:
        context = min(context, shape.max_context)
    kv_cache = shape.kv_per_token * context if shape else None
    overhead = OVERHEAD_BYTES + OVERHEAD_SHARE * weights
    return Estimate(weights, kv_cache, overhead, context, shape)


def suggest_context(estimate: Estimate, gpu_total: float | None) -> int:
    """Return the context a session should start with.

    ``SUGGESTED_CONTEXT``, or less when the model allows less or the cache would not fit in
    ``GPU_SHARE`` of the GPU next to the weights. Rounded down to whole thousands (1,024).

    Args:
        estimate: The model's estimate (its shape gives the cache per token and the maximum).
        gpu_total: Memory of the GPU, or None when unknown.

    Returns:
        Tokens; ``DEFAULT_CONTEXT`` when nothing better is known or not even the weights fit.
    """
    if estimate.shape is None:
        return DEFAULT_CONTEXT
    tokens = SUGGESTED_CONTEXT
    if estimate.shape.max_context:
        tokens = min(tokens, estimate.shape.max_context)
    if gpu_total:
        longest = estimate.max_context(gpu_total) or 0
        if longest < 1024:
            return min(DEFAULT_CONTEXT, tokens)
        tokens = min(tokens, longest // 1024 * 1024)
    return tokens


@dataclass(frozen=True)
class Gpu:
    name: str
    total: float
    free: float


@dataclass(frozen=True)
class Verdict:
    level: str  # fits | busy | tight | too-big | unknown
    text: str


def verdict(needed: float, gpu: Gpu | None) -> Verdict:
    """Compare an estimate with a GPU.

    Args:
        needed: Bytes the model needs.
        gpu: The GPU to run it on, or None if there is no GPU information.

    Returns:
        The level and a short explanation.
    """
    if gpu is None:
        return Verdict("unknown", "No GPU information (NVIDIA only).")
    usable = gpu.total * GPU_SHARE
    if needed <= usable:
        if needed <= gpu.free:
            return Verdict("fits", f"Fits on the {gpu.name}.")
        return Verdict(
            "busy",
            f"Fits on the {gpu.name}, but only {gpu.free / GIB:.1f} GB is free right now.",
        )
    if needed <= gpu.total:
        return Verdict(
            "tight",
            f"Tight: needs more than the {GPU_SHARE:.0%} of the {gpu.name} that vLLM uses by "
            "default. A shorter context may help.",
        )
    return Verdict("too-big", f"Too big for the {gpu.name} ({gpu.total / GIB:.0f} GB).")
