<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0029 Coding benchmarks with EvalPlus, run in isolated containers

- Status: accepted
- Date: 2026-10-10

## Context
Ticket #21:
- benchmark a downloaded model with something like EvalPlus, starting the model if it isn't
  running;
- show roughly how far it has got, and allow cancelling;
- keep every result, show a headline score next to the model with a link to a full report, and let
  every user browse a table of all benchmarks.

## Decision
- **Suites:** EvalPlus HumanEval+ (164 tasks) and MBPP+ (378). Each has a *base* score (the original
  tests) and a *plus* score (many more tests, the stricter one), pass@1 with one greedy answer per
  task.
- **Image `ow-bench:1`**, built locally: `python:3.11-slim`, `evalplus==0.3.1`, the datasets baked
  in (benchmarks run without internet), and `bench/runner.py`.
- **Steps**, one benchmark at a time, run by a worker thread that adopts its containers after a
  restart:
  1. **starting:** if the model isn't running, the AI session is started with it, using the
     suggested settings. If another model runs, the page asks first and the benchmark replaces it;
     a benchmark queued without that permission fails with a clear message.
  2. **generating:** `ow-bench-generate` runs on the session network as the service's UID.
     `runner.py` sends EvalPlus's own request (`evalplus.codegen --backend openai --greedy`: same
     messages, temperature 0, top-p 0.95, 768 tokens), 8 at a time so vLLM batches them. It
     switches thinking off with `chat_template_kwargs.enable_thinking=false` (otherwise Qwen3
     spends the 768 tokens thinking and returns `<think>` without code), extracts the code with
     EvalPlus's `sanitize`, and writes `progress.json` for the page.
  3. **evaluating:** `evalplus.evaluate` runs in `ow-bench-evaluate` with **no network**, all
     capabilities dropped, `no-new-privileges`, 8 GB memory and 1,024 processes, because it runs
     code the model wrote.
- **Storage:**
  - files in `$OW_ROOT/state/benchmarks/<id>/` (`samples.jsonl`, `raw.jsonl`,
    `samples_eval_results.json`);
  - one row per run in `benchmarks`: model, suite, who and when, the session settings, task counts
    and scores, and errors.
- **Pages:**
  - the AI page has a **Benchmark** column with the latest score per suite (a link to the report), a
    menu to start one, and a "Benchmarks running" card with the step, n of N tasks and **Cancel**;
  - **Benchmarks** (header link, every user) lists all runs with model, suite, plus and base
    scores, date, duration and settings;
  - the report shows the scores, the run details, every task with its base and plus result and the
    model's code, and the files to download (logged-in users, known names only).

## Consequences
- **Timing:** on the RTX 5080, HumanEval+ for Qwen3-0.6B took 6½ minutes. Most of that was
  starting the model and building the image once; answering took 20 s and testing about 1 minute.
- **Repeatability:** batched greedy decoding in vLLM isn't bit-for-bit repeatable, so runs differ by
  a few points (36.6 % and 34.1 % plus for the same model). The page says so.
- **Comparability:** the scores are comparable with published EvalPlus results for chat models
  without thinking. Thinking-mode results would need longer answers and are not offered.
