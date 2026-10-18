<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0039 A second AI engine: llama.cpp with GGUF models, for Intel and AMD

- Status: accepted
- Date: 2026-10-18

## Context
vLLM hangs the Arc A770 on contexts over about 5,100 tokens (ADR 0038), and Open WebUI's built-in
tools make every chat longer than that. vLLM has no AI session for AMD in OrbitalWindow either.
OrbitalWindow must run AI models on NVIDIA, Intel and AMD hosts (#50, #54).

llama.cpp has official server images for CUDA, SYCL (Intel oneAPI), ROCm and Vulkan, with an
OpenAI-compatible API, tool calls through the model's own chat template (`--jinja`) and thinking
models. It loads GGUF files: compressed (quantised) models, usually published as `<model>-GGUF`
repositories with one file per quantisation.

Test on Wired (Arc A770, 16 GB), build b11382, Qwen3-8B Q4_K_M, 32K context:

| Build | 6,000-token prompt | Open WebUI request (35 tools) | 30,000-token prompt |
|---|---|---|---|
| SYCL (`server-intel`) | 6.3 s | 10.5 s, correct | 28 s |
| Vulkan (`server-vulkan`) | 12.1 s | 16.1 s, correct | 214 s |

Generation with SYCL: 27 tokens/s for one chat, 57 tokens/s in total for four. No GPU hangs.

## Decision
- The runtime setting `ai_engine` is `vllm` or `llamacpp` (**Settings → AI**; `OW_AI_ENGINE` sets
  the first value). The settings page says that vLLM does not work on Intel GPUs. vLLM stays
  allowed on Intel for newer GPUs; llama.cpp also runs on AMD.
- A llama.cpp session runs `ghcr.io/ggml-org/llama.cpp:server-<backend>-<build>`, with the backend
  from the GPU setting: `cuda`, `intel` (SYCL: much faster than Vulkan on the A770) or `rocm`. The
  build is pinned (`OW_LLAMACPP_BUILD`, default b11382). It uses the vLLM session's container
  name, network, port, API key (in `LLAMA_API_KEY`, not on the command line) and labels, so the
  chat, the API, the dashboard and the benchmarks work without change.
- llama.cpp gets the whole context as one KV cache shared by 4 slots (`--parallel 4
  --kv-unified`), all layers on the GPU, the model's chat template (`--jinja`) and no web UI. The
  GPU share is a vLLM setting and is hidden for llama.cpp.
- **Model library:** Inspect lists a GGUF repository's files grouped per quantisation (split
  parts together, vision projectors left out), with `Q4_K_M` chosen first. Only the chosen files
  are downloaded (`model_downloads.files`). A model folder holds one GGUF file. A folder with a
  `config.json` (safetensors) or a GGUF file is a model; the session list shows the models the
  engine can run.
- **Fit check:** a GGUF model's weights are its file size; the KV cache per token comes from the
  metadata at the start of the file (`gguf.py`): read from disk, or before the download with HTTP
  range requests to the Hub. llama.cpp keeps the cache in float16.

## Consequences
- Intel and AMD hosts have an AI session. Tested on Intel (A770); AMD (ROCm) is untested.
- Two model formats: vLLM can't run a GGUF model and llama.cpp can't run safetensors. Switching
  the engine means downloading the other format.
- llmfit's suggestions name safetensors repositories; for llama.cpp, look for the GGUF version.
- The Intel image is about 16 GB; the Vulkan image (1.2 GB) would be smaller but is much slower on
  long prompts.
