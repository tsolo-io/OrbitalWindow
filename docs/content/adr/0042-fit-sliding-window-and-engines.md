<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0042 Fit check: sliding-window layers, and overhead and share per engine

- Status: accepted
- Date: 2026-10-19

## Context
The fit check (ADR 0026) planned a full KV cache in every attention layer, and a vLLM-sized
overhead and GPU share for every model (#60). On Wired (Arc A770, 15.9 GiB) that made
gpt-oss-20b Q4_K_M at 131,072 tokens "about 18.8 GB, too big", while llama.cpp ran it in
13.8 GiB. gpt-oss uses a 128-token sliding window in every other layer, and Gemma 4 a 1,024-token
window in 5 of every 6. llama.cpp (verbose log, gpt-oss-20b, `--parallel 4 --kv-unified`) keeps
3,072 MiB for the 12 full layers (131,072 cells) and 24 MiB for the 12 sliding layers
(1,024 cells = 128 × 4 + 512).

## Decision
- `Shape` has the bytes per token of the full-attention layers, the bytes per cached token of the
  sliding-window layers, and how many tokens those keep. The cache for a context is
  full × context + sliding × min(context, kept tokens).
- GGUF: the reader works per layer, as llama.cpp does. A layer slides by the file's
  `sliding_window_pattern`, or by llama.cpp's built-in period for the architecture (gpt-oss 2,
  Gemma 2 2, Gemma 3 6, Command R7B 4: layer *i* slides when *i* mod *n* < *n* − 1). Heads and
  key/value sizes may differ per layer (Gemma 4) and between full and sliding layers. The sliding
  layers keep one window per request slot (4) plus a 512-token batch, rounded up to 256.
  Linear-attention layers (hybrid models, #59) and layers that reuse another layer's cache keep
  nothing that grows.
- `config.json` (vLLM): `sliding_attention` layers in `layer_types` keep `sliding_window` tokens
  for each of the 16 requests vLLM serves at once.
- The engine sets the overhead and the share: vLLM 1.5 GB + 5 % of the weights within 90 % of the
  GPU, as before; llama.cpp 0.5 GB (its compute buffers) within 95 %.

## Consequences
- gpt-oss-20b at 131,072 tokens: 3,096 MiB of cache (llama.cpp: 3,072 + 24 MiB) and 14.3 GiB in
  all against 13.8 GiB measured; it fits. Qwen3.5-9B at 131,072: 9.8 GiB against 9.3 GiB.
- The estimate follows llama.cpp's sizing; a llama.cpp release that sizes the sliding cache
  differently needs the constants (`SWA_SEQS`, `SWA_BATCH`) checked.
- New sliding-window architectures without `sliding_window_pattern` need their period in
  `SWA_PATTERNS`; without one, every layer of a model with a window is taken as sliding.
