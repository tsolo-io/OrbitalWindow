<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0038 The AI session on Intel GPUs: vLLM's XPU image, with limits on Arc A-series

- Status: proposed (blocked: contexts over about 5,000 tokens hang the A770, see below)
- Date: 2026-10-18

## Context
The AI session (ADR 0027) runs vLLM's CUDA image on an NVIDIA GPU. Intel hosts must run it too
(ticket #50). vLLM publishes an Intel image, `vllm/vllm-openai-xpu`, from v0.26.0. Its tag
`v0.30.0` matches the CUDA image OrbitalWindow uses.

Tests on Wired (Arc A770, 16 GB, i915, no Resizable BAR) with Qwen3-0.6B:

1. vLLM refuses bfloat16: "Intel Arc A770 have bfloat16 accuracy known issue". It checks the
   device name for "a770".
2. With `--dtype half`, allocating the KV cache (one block of 7.25 GiB) fails although the GPU is
   free: Xe-HPG GPUs allocate at most 4 GB in one block.
3. `UR_L0_ENABLE_RELAXED_ALLOCATION_LIMITS=1` allows the block, but the Flash Attention kernels
   then stop: "Only XE2/XE3 cutlass kernel is supported currently".
4. With `--attention-backend TRITON_ATTN` the model runs, but answers are random tokens, in
   float16 and in float32: kernels can't address blocks over 4 GB.
5. Without relaxed limits and with `--kv-cache-memory-bytes` of 3 GiB the answers are correct
   ("The capital of France is Paris."; 17 × 23 = 391).
6. A context of more than about 5,100 tokens hangs the GPU: the request never ends and the kernel
   logs `Fence expiration time out` for the engine. 5,020 tokens work (in 13 s); 5,120, 5,500 and
   6,000 hang. It also hangs with `--enforce-eager`, with prefill chunks of 1,024 tokens, with
   KV block size 64, with a 2 GiB KV cache and with i915's preempt timeout raised to 60 s.
   Open WebUI sends its 35 built-in tools with every chat (about 6,000 tokens), so every chat
   hangs the session.

## Decision
- On Intel hosts the session runs `OW_VLLM_INTEL_IMAGE` (default `vllm/vllm-openai-xpu:v0.30.0`)
  with `/dev/dri` and no device request. `ZE_AFFINITY_MASK` selects the GPU by the exporter's
  index (ADR 0036). AMD hosts still have no AI session.
- On Arc A-series and Flex GPUs (Xe-HPG, recognised by name; the exporter names unknown DG2 PCI
  ids "Intel Arc A-series GPU"), the session adds `--dtype half --attention-backend TRITON_ATTN
  --kv-cache-memory-bytes 3.75 GiB`. Relaxed allocation limits stay off.
- The fit check, the suggested context and the start check take this KV limit into account:
  `fit.Gpu.kv_limit`. A context that needs a larger cache is refused with the longest that fits.
- Other Intel GPUs (Arc B-series and newer) run with vLLM's defaults.

## Consequences
- **Blocking on the A770:** with vLLM v0.30.0, a context over about 5,100 tokens hangs the GPU
  (finding 6), so the chat does not work. This needs a decision before the ADR is accepted.
- On the A770 the longest context is short: about 27,000 tokens for an 8B model (Qwen3-8B), below
  the 32,768 that coding tools need (ADR 0031). Smaller models get more.
- float16 instead of bfloat16 can lose accuracy for some models.
- The B-series rules are untested: no such GPU is available. If vLLM changes its XPU kernels, the
  Xe-HPG rules must be checked again when `OW_VLLM_INTEL_IMAGE` is updated.
