# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Generate EvalPlus solutions from the AI session (runs inside the ow-bench image).

The request is EvalPlus's own (``evalplus.codegen --backend openai --greedy``: same system and user
message, temperature 0, top-p 0.95, 768 new tokens), with two changes: requests run in parallel so
vLLM can batch them, and thinking is switched off for models whose chat template supports it
(``enable_thinking``), so the answer isn't cut off mid-thought. Progress goes to progress.json.

Usage: python runner.py <dataset> <model> <base_url> <out_dir>   (OPENAI_API_KEY in the environment)
"""

import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import openai
from evalplus.data import get_human_eval_plus, get_mbpp_plus
from evalplus.sanitize import sanitize

INSTRUCTION = (
    "Please provide a self-contained Python script that solves the following problem in a "
    "markdown code block:"
)
SYSTEM = "You are a helpful assistant good at coding."
THINK_RE = re.compile(r"<think>.*?</think>", re.S)
WORKERS = 8
MAX_TOKENS = 768


def main(dataset: str, model: str, base_url: str, out_dir: str) -> None:
    """Generate one greedy solution per task and write samples.jsonl.

    Args:
        dataset: ``humaneval`` or ``mbpp``.
        model: The served model name.
        base_url: The OpenAI-compatible API, e.g. ``http://ow-ai-model:8000/v1``.
        out_dir: Folder for samples.jsonl and progress.json.
    """
    tasks = get_human_eval_plus() if dataset == "humaneval" else get_mbpp_plus()
    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=base_url, timeout=600)
    total, done, lock = len(tasks), 0, threading.Lock()
    progress_path = os.path.join(out_dir, "progress.json")

    def report() -> None:
        """Write the number of finished tasks."""
        with open(progress_path + ".tmp", "w") as out:
            json.dump({"phase": "generate", "done": done, "total": total}, out)
        os.replace(progress_path + ".tmp", progress_path)

    def solve(task: dict) -> dict:
        """Ask the model for one task and extract the code.

        Args:
            task: The EvalPlus task.

        Returns:
            The sample: task id, extracted solution and the raw answer.
        """
        nonlocal done
        message = INSTRUCTION + f"\n```python\n{task['prompt'].strip()}\n```"
        reply = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": message}],
            max_tokens=MAX_TOKENS,
            temperature=0.0,
            top_p=0.95,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        raw = reply.choices[0].message.content or ""
        solution = sanitize(THINK_RE.sub("", raw), task["entry_point"])
        with lock:
            done += 1
            report()
        return {"task_id": task["task_id"], "solution": solution, "raw": raw}

    report()
    with ThreadPoolExecutor(WORKERS) as pool:
        samples = list(pool.map(solve, tasks.values()))
    with open(os.path.join(out_dir, "samples.jsonl"), "w") as out:
        for sample in samples:
            line = {"task_id": sample["task_id"], "solution": sample["solution"]}
            out.write(json.dumps(line) + "\n")
    with open(os.path.join(out_dir, "raw.jsonl"), "w") as out:
        for sample in samples:
            out.write(json.dumps({"task_id": sample["task_id"], "raw": sample["raw"]}) + "\n")


if __name__ == "__main__":
    main(*sys.argv[1:5])
