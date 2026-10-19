# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Will a model fit on the GPU? A GPU memory estimate for serving it with vLLM.

Needed memory = weights (the safetensors or GGUF files, loaded as they are) + KV cache for the
context length + overhead (vLLM: CUDA context, activations, CUDA graphs; llama.cpp: compute
buffers). vLLM uses at most ``GPU_SHARE`` of a GPU by default, so a model "fits" when the total is
within that share. Full-attention layers cache every token of the context; sliding-window layers
only their window (#60); linear-attention layers of hybrid models nothing that grows.
"""

from dataclasses import dataclass
from typing import Any

# The fit check plans for the least coding tools need: OpenCode's first request alone is about
# 7,500 tokens, and the conversation grows from there (ADR 0031).
CODING_CONTEXT = 32_768
DEFAULT_CONTEXT = CODING_CONTEXT
SUGGESTED_CONTEXT = 65_536  # the session's default when it fits
GPU_SHARE = 0.9  # vLLM's default --gpu-memory-utilization
LLAMACPP_SHARE = 0.95  # llama.cpp has no such limit; leave a little for the desktops' encoders
OVERHEAD_BYTES = 1.5e9
OVERHEAD_SHARE = 0.05  # of the weights: activations grow with the model
# llama.cpp needs only its compute buffers (measured on an A770: weights + KV cache are within a
# few hundred MB of the GPU memory used, for gpt-oss-20b and Qwen3.5-9B).
LLAMACPP_OVERHEAD_BYTES = 0.5e9
# vLLM serves up to 16 requests at once (aisession.MAX_SEQS); each keeps its own window.
VLLM_SEQS = 16
DTYPE_BYTES = {"float32": 4, "float16": 2, "bfloat16": 2}
GIB = 1024**3  # GPU memory is shown in binary units, as nvidia-smi and the admin page do


@dataclass(frozen=True)
class Shape:
    """What the KV cache size depends on, from the model's ``config.json``."""

    layers: int  # full-attention layers (hybrid models: not the linear-attention ones)
    kv_heads: int
    head_dim: int
    kv_bytes: int  # per value
    max_context: int | None
    full_bytes: int | None = None  # bytes per token of all full layers, when they differ per layer
    swa_bytes: int = 0  # bytes per cached token of all sliding-window layers
    swa_tokens: int = 0  # tokens the sliding-window layers keep (window × requests + a batch)

    @property
    def kv_per_token(self) -> int:
        """Return the KV cache size of one token of context (keys and values, full layers).

        Returns:
            Bytes per token.
        """
        if self.full_bytes is not None:
            return self.full_bytes
        return 2 * self.layers * self.kv_heads * self.head_dim * self.kv_bytes

    def kv_cache(self, context: int) -> int:
        """Return the KV cache for a context: full layers grow with it, sliding ones stop.

        Args:
            context: Context length in tokens.

        Returns:
            Bytes.
        """
        return self.kv_per_token * context + self.swa_bytes * min(context, self.swa_tokens)


def shape_from_config(config: dict[str, Any]) -> Shape | None:
    """Read the attention shape from a model configuration.

    Multimodal models keep the language model's settings in ``text_config``. In ``layer_types``,
    ``sliding_attention`` layers cache only their ``sliding_window`` (per request); other kinds
    (linear attention) have no KV cache that grows.

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
        sliding = 0
        if isinstance(kinds, list) and "full_attention" in kinds:
            layers = kinds.count("full_attention")
            sliding = kinds.count("sliding_attention")
        heads = int(text["num_attention_heads"])
        kv_heads = int(text.get("num_key_value_heads") or heads)
        head_dim = int(text.get("head_dim") or int(text["hidden_size"]) // heads)
        window = int(text.get("sliding_window") or 0) if sliding else 0
    except KeyError, TypeError, ValueError, ZeroDivisionError:
        return None
    dtype = str(text.get("torch_dtype") or text.get("dtype") or config.get("torch_dtype") or "")
    kv_bytes = DTYPE_BYTES.get(dtype, 2)
    max_context = text.get("max_position_embeddings")
    return Shape(
        layers=layers,
        kv_heads=kv_heads,
        head_dim=head_dim,
        kv_bytes=kv_bytes,
        max_context=int(max_context) if max_context else None,
        swa_bytes=2 * sliding * kv_heads * head_dim * kv_bytes if window else 0,
        swa_tokens=window * VLLM_SEQS,
    )


@dataclass(frozen=True)
class Estimate:
    weights: float
    kv_cache: float | None  # None when the model's shape is unknown
    overhead: float
    context: int
    shape: Shape | None
    share: float = GPU_SHARE  # of the GPU the engine may use

    @property
    def total(self) -> float:
        """Return the GPU memory needed.

        Returns:
            Bytes; without a known shape, weights and overhead only.
        """
        return self.weights + (self.kv_cache or 0) + self.overhead

    def max_context(self, gpu_bytes: float, kv_limit: float | None = None) -> int | None:
        """Return the longest context whose KV cache fits next to the weights.

        Args:
            gpu_bytes: Total memory of the GPU.
            kv_limit: Largest KV cache the GPU can hold, or None for no limit.

        Returns:
            Tokens (capped at the model's maximum), 0 if not even the weights fit, or None
            without a known shape.
        """
        if self.shape is None:
            return None
        room = gpu_bytes * self.share - self.weights - self.overhead
        if kv_limit:
            room = min(room, kv_limit)
        room -= self.shape.swa_bytes * self.shape.swa_tokens  # the windows, whatever the context
        if self.shape.kv_per_token == 0:  # only sliding windows: the context costs nothing more
            tokens = self.shape.max_context or 0 if room >= 0 else 0
        else:
            tokens = max(int(room // self.shape.kv_per_token), 0)
        return min(tokens, self.shape.max_context) if self.shape.max_context else tokens


def estimate(
    weights: float,
    config: dict[str, Any] | None,
    context: int = DEFAULT_CONTEXT,
    shape: Shape | None = None,
    engine: str = "vllm",
) -> Estimate:
    """Estimate the GPU memory for serving a model.

    Args:
        weights: Size of the weight files in bytes.
        config: The model's ``config.json``, or None if it couldn't be read.
        context: Context length to plan the KV cache for (capped at the model's maximum).
        shape: The attention shape when there is no ``config.json`` (a GGUF file's metadata).
        engine: ``vllm`` or ``llamacpp``; their overhead differs.

    Returns:
        The estimate.
    """
    shape = shape or (shape_from_config(config) if config else None)
    if shape is not None and shape.max_context:
        context = min(context, shape.max_context)
    kv_cache = shape.kv_cache(context) if shape else None
    if engine == "llamacpp":
        overhead, share = LLAMACPP_OVERHEAD_BYTES, LLAMACPP_SHARE
    else:
        overhead, share = OVERHEAD_BYTES + OVERHEAD_SHARE * weights, GPU_SHARE
    return Estimate(weights, kv_cache, overhead, context, shape, share)


def suggest_context(
    estimate: Estimate, gpu_total: float | None, kv_limit: float | None = None
) -> int:
    """Return the context a session should start with.

    ``SUGGESTED_CONTEXT``, or less when the model allows less, the cache would not fit in
    ``GPU_SHARE`` of the GPU next to the weights, or the cache would be larger than the GPU's
    limit. Rounded down to whole thousands (1,024).

    Args:
        estimate: The model's estimate (its shape gives the cache per token and the maximum).
        gpu_total: Memory of the GPU, or None when unknown.
        kv_limit: Largest KV cache the GPU can hold, or None for no limit.

    Returns:
        Tokens; ``DEFAULT_CONTEXT`` when nothing better is known or not even the weights fit.
    """
    if estimate.shape is None:
        return DEFAULT_CONTEXT
    tokens = SUGGESTED_CONTEXT
    if estimate.shape.max_context:
        tokens = min(tokens, estimate.shape.max_context)
    if gpu_total:
        longest = estimate.max_context(gpu_total, kv_limit) or 0
        if longest < 1024:
            return min(DEFAULT_CONTEXT, tokens)
        tokens = min(tokens, longest // 1024 * 1024)
    return tokens


@dataclass(frozen=True)
class Gpu:
    name: str
    total: float
    free: float
    kv_limit: float | None = None  # largest KV cache the GPU can hold in one block


@dataclass(frozen=True)
class Verdict:
    level: str  # fits | busy | tight | too-big | unknown
    text: str


def verdict(
    needed: float, gpu: Gpu | None, kv_cache: float | None = None, share: float = GPU_SHARE
) -> Verdict:
    """Compare an estimate with a GPU.

    Args:
        needed: Bytes the model needs.
        gpu: The GPU to run it on, or None if there is no GPU information.
        kv_cache: Bytes of KV cache in the estimate, checked against the GPU's limit.
        share: Share of the GPU the engine may use (``Estimate.share``).

    Returns:
        The level and a short explanation.
    """
    if gpu is None:
        return Verdict("unknown", "No GPU information (NVIDIA and Intel only).")
    usable = gpu.total * share
    if needed <= usable and gpu.kv_limit and kv_cache and kv_cache > gpu.kv_limit:
        return Verdict(
            "tight",
            f"Fits on the {gpu.name} with a shorter context: its context cache can be at most "
            f"{gpu.kv_limit / GIB:.2f} GB.",
        )
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
            f"Tight: needs more than the {share:.0%} of the {gpu.name} that the AI engine uses. "
            "A shorter context may help.",
        )
    return Verdict("too-big", f"Too big for the {gpu.name} ({gpu.total / GIB:.0f} GB).")
