<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0024 AI manager role and the model library

- Status: accepted
- Date: 2026-10-10

## Context
Researchers want local LLMs: chat, and calling a model from code. Ticket #15 is the first step:
- a role that manages the AI functions;
- models downloaded from Hugging Face and managed in OrbitalWindow: free space, the downloaded
  models with their sizes, and downloads started from the UI that run in the background.

Later tickets add the fit check (#16), one vLLM session (#17) and Open WebUI (#18). Production GPUs
are 16–48 GB, most 32 GB.

## Decision
- **Role:** `users.is_ai_manager`, granted on the Users page. Admins count as AI managers. The AI
  page (`/ai`) needs the role; the session check on every action works as for admin pages.
- **One folder per model**, `$OW_ROOT/shared/models/<org>/<name>`, made with
  `hf download --local-dir`, not the Hub cache.
  - Since `huggingface_hub` 2, the cache deduplicates files into a shared blob store with symlinks,
    which makes per-model sizes and safe deletion awkward.
  - A plain folder is measured with one walk, deleted with one `rmtree`, and loaded by path by vLLM
    and by Python code.
- **Only what inference needs is downloaded:** `*.json`, `*.safetensors`, `*.txt`, `*.model`,
  `*.tiktoken`, `*.jinja`. `original/*` is skipped; for Llama models that is a 16 GB duplicate
  checkpoint. Models without safetensors weights are refused, because vLLM loads safetensors.
- **Inspect first:** the Hub API (`/api/models/<repo>/revision/<rev>?blobs=true`, via `httpx2`)
  gives:
  - the size of the selected files, whether the model is gated, the architecture, the parameters
    and the licence;
  - the commit sha, which the download is pinned to.

  A download that doesn't fit on the disk is refused.
- **Downloads run one at a time** in a container `ow-model-dl`, from a small local image
  `ow-hf:1` (`python:3.13-slim` + `huggingface_hub[hf_xet]` 2.1.1):
  - The container runs as the service's UID, with all capabilities dropped and
    `no-new-privileges`, and mounts only the models folder.
  - It keeps running across service restarts; the worker adopts it again by its label.
  - Progress is the model folder's size against the expected total.
  - Cancel removes the container. `hf` resumes partial files, so Resume queues the same commit
    again.
- **The models folder** is created by the layout helper and owned by the service's UID, so the
  service measures and deletes models without a root helper. Desktops can read it; mounting it
  read-only into desktops is left to the session work.
- **Hugging Face token** (for gated models): set on the AI page and stored in
  `$OW_ROOT/state/hf_token` (mode 600). It is passed only to download containers and to the
  inspect request, and is never written to the database or the audit log.

## Consequences
- **Disk:** the models' disk use shows on the AI page, next to the free space of the disk the
  models are on.
- **Gated models** need a Hugging Face account that accepted the model's licence; its token is
  shared by all AI managers.
- **GGUF-only or `.bin`-only repos** can't be downloaded until an engine that uses them is added.
