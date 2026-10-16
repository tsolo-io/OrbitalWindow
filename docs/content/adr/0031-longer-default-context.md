<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0031 Longer contexts: plan for 32K, start with the longest that fits up to 64K

- Status: accepted
- Date: 2026-10-16

## Context
Ticket #38: Qwen/Qwen3.5-4B was started with an 8K context, and OpenCode did not work with it.
The ticket also asks OrbitalWindow to show the running model's context.

- **Why 8K:** the AI session form always kept the context box's value (8,192) when a model was
  chosen. The fit check (ADR 0026) also planned for 8,192 tokens. Nothing in the hardware limited
  it.
- **What coding tools need:** OpenCode's first request is about 7,500 tokens (system prompt and
  tool definitions) before any code or conversation. Guides for running it on local models use
  32K at the least, and 64K where possible. Ollama raised its own default for the same reason.
- **Cost:** a longer context costs GPU memory for the KV cache. vLLM reserves its share of the GPU
  up front, and the maximum context must fit in that cache.
  - Qwen3.5-4B is a hybrid model: only 8 of its 32 layers keep a KV cache, about 32 KB per token.
    So 8K costs 0.25 GiB and 64K about 2 GiB; about 80K fits in 90 % of a 16 GB GPU next to the
    weights.
  - A dense model such as Qwen3-8B needs 144 KB per token, so 32K already costs 4.5 GiB.
  - Longer contexts therefore push towards smaller (or hybrid) models on the same GPU, as the
    ticket expected.

## Decision
- **Fit check:** plans for 32,768 tokens (`fit.CODING_CONTEXT`), the least coding tools need.
  "fits" now means "fits with a context that coding tools can use". The model page warns when
  even the longest context that fits is shorter.
- **Session default:** choosing a model fills in the longest context that fits in 90 % of the GPU,
  up to 65,536 tokens (`fit.SUGGESTED_CONTEXT`) and the model's own maximum, rounded down to whole
  thousands (1,024). The GPU share is then sized from it as before. The AI manager can still type
  any value; the estimate warns below 32K.
- **Benchmarks** keep starting their sessions with 8,192 tokens: EvalPlus prompts and 768-token
  answers are short, and a smaller cache leaves the GPU to others.
- **Shown:**
  - the dashboard's AI card shows every user the running model's context window, with one line
    explaining it;
  - the AI page shows it as before, and its API example says it;
  - the user guide explains the context window and how to give it to OpenCode (`limit.context`).
- **Not done:** an FP8 KV cache (`--kv-cache-dtype fp8`) would halve the cache. It is not needed for
  the models in use, and it can cost accuracy. It remains an option if dense models need longer
  contexts.

## Consequences
- Existing sessions keep their context until they are started again.
- Some models that "fit" with 8K now show "tight" or "too big". That is intended: they could not
  serve coding tools on this GPU.
- More KV cache per session means fewer long conversations at once. With 2–3 users that is not a
  limit.
