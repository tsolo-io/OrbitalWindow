<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0026 Model fit: our own estimate, llmfit only for ideas

- Status: accepted
- Date: 2026-10-10

## Context
Ticket #16: before downloading or starting a model, show whether it fits on the server's GPU, and
use llmfit to suggest models. Martin asked whether bundling llmfit would force frequent
OrbitalWindow releases. llmfit releases several times a week. It scores models for llama.cpp
and MLX at whichever quantisation fits (e.g. Q8_0 GGUF), not for vLLM, which loads the
safetensors weights as they are. Its full list is mostly re-uploads and community fine-tunes.

## Decision
- **Fit estimate (`fit.py`, our own):**
  - The formula is weights + KV cache + overhead:
    - weights = the size of the safetensors files;
    - KV cache = 2 × layers × KV heads × head dim × 2 bytes per token (from `config.json`,
      including `text_config` of multimodal models), for 8,192 tokens or the model's maximum if
      that is lower. Hybrid models (Qwen3.5, LFM2) list their layers in `layer_types`; only the
      `full_attention` ones have a KV cache, so only they count;
    - overhead = 1.5 GB + 5 % of the weights.
  - It is compared with the GPU that has the most memory (NVML). The levels are:
    - **fits**: within the 90 % vLLM uses by default;
    - **fits (GPU busy)**: as fits, but not that much free right now;
    - **tight**: within 100 %;
    - **too big**.
  - The longest context that fits is shown too. The page shows the estimate under Inspect, and
    in a "GPU fit" column for downloaded models. Sliding-window attention is ignored, so the
    estimate errs on the large side. The constants are checked against vLLM's real use in #17.
- **llmfit is run, not bundled:** a short-lived container of `OW_LLMFIT_IMAGE` (default
  `ghcr.io/alexsjones/llmfit:1.1.15`) runs `llmfit --json --memory <GPU GB>G recommend`, without
  network access and with all capabilities dropped. A newer llmfit is a tag change in `.env`, not
  an OrbitalWindow release. The answer is cached per GPU size for an hour.
- **Suggestions are ideas:**
  - Only models of well-known publishers are kept (Qwen, Meta, Google, Mistral, Microsoft,
    DeepSeek, IBM, AllenAI, NVIDIA, …), without GGUF, MLX or bitsandbytes re-packs.
  - Models whose own weights can't fit are dropped. The check is a rough size from llmfit's
    parameter count: 2 bytes per parameter, 1 for FP8 names, about 0.55 for AWQ/GPTQ/INT4/NVFP4.
    Without it, llmfit suggests 8B models for a 16 GB card because it assumes Q8 GGUF.
  - On the GPU node this gives 3–4B models for 16 GB, 7–8B for 32 GB, and up to 15B (or 30B in
    FP8) for 48 GB. They are shown with their kind, licence and llmfit score; **Inspect** gives the real
  size and fit for vLLM.

## Consequences
- **No NVIDIA GPU (NVML):** the fit shows as "unknown" and there are no suggestions.
- **Units:** GPU memory is shown in binary units labelled GB, as `nvidia-smi` and the admin page
  show it (an RTX 5080 is 15.9 GB). The AI and status pages use the same units for disk sizes.
- **Calibration** on the RTX 5080 with vLLM v0.30.0 and Qwen3-0.6B:
  - the KV cache per token matched vLLM's own count exactly (5.4 GiB = 50,560 tokens);
  - the real overhead (non-torch, activations, CUDA graphs) was 1.3 GiB against the estimate's
    1.6 GB.
- **Gated models:** their configuration needs the Hugging Face token. Without it the estimate
  leaves out the cache and says so.
