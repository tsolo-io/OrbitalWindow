<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0027 One AI session: a vLLM container with an OpenAI-compatible API

- Status: accepted
- Date: 2026-10-10

## Context
Ticket #17: an AI manager picks a downloaded model and starts it, with one session at a time.
Everyone uses it through Open WebUI (#18) or from code. Martin also wants the OpenAI API usable from
other machines without a desktop, for example OrbitalWindow on a big GPU server and a laptop
calling it.

## Decision
- **Engine:** vLLM, image `OW_VLLM_IMAGE` (default `vllm/vllm-openai:v0.30.0`, CUDA 13; about
  30 GB, pulled on first start). Most production GPUs are 32 GB, and vLLM serves several users at
  once well. Checked on the RTX 5080 (Blackwell): the official image works.
- **Container `ow-ai-model`:**
  - Arguments: `<models>/<org>/<name> --served-model-name <org>/<name> --max-model-len
    <context> --gpu-memory-utilization <share>`.
  - **Tool calling:** for model families with a known vLLM parser (Qwen 2/3: `hermes`, Qwen3
    coder: `qwen3_coder`, Llama 3: `llama3_json`, Mistral, gpt-oss, Granite, OLMo 3, Gemma 4),
    also `--enable-auto-tool-choice --tool-call-parser <parser>`, picked from `model_type` in
    `config.json`. Open WebUI sends its built-in tools with every chat, and without these flags
    vLLM refuses the request ("auto" tool choice requires …). For other families, Open WebUI is
    started with `DEFAULT_MODEL_PARAMS={"function_calling": "legacy"}`, so it describes tools in the
    prompt instead.
  - Models are mounted read-only, and Hugging Face is set offline. The chosen GPU comes in through
    `device_ids`.
  - A named volume `ow-vllm-cache` holds compiled kernels, so later starts are faster.
  - `no-new-privileges`, restart policy `no`: a crash shows as *failed* with the end of the log,
    instead of looping.
  - A healthcheck on `/health` (start period 30 min) decides when *starting* becomes *running*.
- **API key:** vLLM checks `VLLM_API_KEY` (an environment variable, not an argument, so it stays out
  of the process list). The key is generated once and kept in `$OW_ROOT/state/ai_api_key`
  (mode 600). AI managers see it on the AI page, and **New key** replaces it at the next start.
- **Network:** an internal network `ow-ai` with no internet. Open WebUI (#18) and benchmarks (#21)
  join it. **Reachable from other computers** (a setting, off by default) adds Traefik labels so
  that `https://<host>/v1` reaches vLLM; vLLM checks the key. Reconcile reconnects Traefik after
  restarts.
- **One session:** a second start is refused while one is starting or running. The page pulls the
  image and starts the container in a background thread, and shows the log while the model loads.
  Each start is a row in `ai_sessions` (who, when, model, GPU, context, share, outcome) for the
  history and later benchmarks.
- **Settings suggested from the estimate (ADR 0026):** the context, and a GPU share of the
  estimate plus 10 %, between 30 % and 90 %. More share means more cache, so more people can chat
  at once. Before starting a model that may not fit, the page asks for confirmation.
- **Dashboard:** every user sees "AI model … is running and ready" while a session runs.

## Consequences
- **Start times:** the first start of a model takes several minutes (about 4 min for a small model
  on the RTX 5080, mostly compiling and capturing CUDA graphs). Later starts are faster thanks to
  the cache volume.
- **GPU sharing:** desktops share the GPU. The share reserves memory for the whole session, so
  desktops have less while it runs. Stopping the session frees it at once.
- **Service restarts** don't touch the session; its state is read from Docker.
