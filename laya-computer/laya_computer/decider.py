"""A persistent local encoder; no remote planner and no generated actions."""

import asyncio
import contextlib
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

DEFAULT_MODEL = "aac6fef/laya-typed-decisions-mlx"


class LayaDecider:
    def __init__(self, model=None):
        self.model = model
        self.model_id = os.environ.get("LAYA_MODEL", DEFAULT_MODEL)
        # MLX work stays on one thread, serially, across sessions and warm-up.
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="laya")
        self.calls = 0
        self.input_tokens = 0
        self.inference_seconds = 0.0
        self.load_seconds = 0.0

    def _choose(self, instruction, candidates, snapshot):
        if not candidates or len(candidates) > 20:
            raise ValueError("Laya needs 1–20 observed candidates; narrow the selector.")
        ids = [str(item["id"]) for item in candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate candidate ids")
        if self.model is None:
            started = time.monotonic()
            # Model downloads/logs must not contaminate the MCP stdout stream.
            with contextlib.redirect_stdout(sys.stderr):
                import laya_mlx

                self.model = laya_mlx.load(self.model_id)
                self.model.system_one("Warm up.", {"q": {
                    "type": "choice", "instructions": "Warm up.", "criteria": ["a", "b"]
                }})
            self.load_seconds += time.monotonic() - started
        # The encoder's reference browser policy uses short textual criteria,
        # rather than JSON objects repeated in both state and alternatives.
        criteria = {str(item["id"]):
                    f"{str(item.get('role', '')).removeprefix('AX')} {str(item.get('label', ''))[:120]}"
                    for item in candidates}
        state = json.dumps({"window": str(snapshot.get("title", ""))[:200],
                            "candidates": criteria}, ensure_ascii=False)
        questions = {"target": {"type": "choice", "criteria": criteria,
                                 "instructions": instruction[:1000]}}
        started = time.monotonic()
        with contextlib.redirect_stdout(sys.stderr):
            result = self.model.system_one(state, questions)
        self.inference_seconds += time.monotonic() - started
        self.calls += 1
        self.input_tokens += int(result.get("usage", {}).get("input_tokens", 0))
        answer = result["answers"]["target"]
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        choice = str(answer["choice"])
        if (set(probabilities) != set(ids) or choice not in ids
                or any(type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 1
                       for n in numbers)
                or abs(sum(probabilities.values()) - 1) >= 0.02
                or probabilities[choice] < max(probabilities.values()) - 1e-6):
            raise ValueError("Invalid local decision; no action executed")
        return choice

    async def choose(self, instruction, candidates, snapshot):
        return await asyncio.get_running_loop().run_in_executor(
            self.pool, self._choose, instruction, candidates, snapshot)

    def metrics(self):
        return {"model": self.model_id, "calls": self.calls, "input_tokens": self.input_tokens,
                "inference_seconds": round(self.inference_seconds, 3),
                "load_seconds": round(self.load_seconds, 3), "remote_tokens": None}

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
