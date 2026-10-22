"""Meterless Markovian Engine (Python port of the spec in
github.com/Meterless/Meterless/engines/markovian/AGENTS.md).

Long agent jobs run as bounded chunks. Each chunk sees only the goal, the
structured carryover from the previous chunk (COMPLETED/REMAINING/DECISIONS/
CONTEXT) and, optionally, an [OVERLAP] tail. Context stays flat per step.
Markers: [STATE_CHECKPOINT] continues, [TASK_COMPLETE] finishes.
"""
from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

FRAMING_TOKENS = 400
OUTPUT_BUDGET = 1200
BOUNDS = {"chunkSize": (1000, 128000), "maxChunks": (1, 32),
          "carryoverTokens": (128, 32768), "overlapTokens": (0, 4096)}
DEFAULTS = {"chunkSize": 8000, "maxChunks": 24, "carryoverTokens": 800, "overlapTokens": 0}
DOC_DEFAULTS = {"F": 400, "G": 200, "S": 300, "O": 1200, "C": 800}


class ConfigValidationError(ValueError):
    pass


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def resolve_config(partial: dict[str, Any] | None = None) -> dict[str, int]:
    cfg = {**DEFAULTS, **(partial or {})}
    for k, (lo, hi) in BOUNDS.items():
        v = cfg[k]
        if not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ConfigValidationError(f"{k}={v} outside [{lo}, {hi}]")
    if cfg["chunkSize"] < FRAMING_TOKENS + cfg["carryoverTokens"] + OUTPUT_BUDGET:
        raise ConfigValidationError(
            f"chunkSize={cfg['chunkSize']} < framing({FRAMING_TOKENS}) + carryover({cfg['carryoverTokens']}) + outputBudget({OUTPUT_BUDGET})")
    return {k: int(v) for k, v in cfg.items()}


def model_totals(n: int, k: dict[str, int] = DOC_DEFAULTS) -> dict[str, float]:
    naive = n * (k["F"] + k["G"] + k["S"]) + k["O"] * n * (n - 1) / 2
    mark = n * (k["F"] + k["G"] + k["C"] + k["S"])
    return {"naiveTotal": naive, "markovianTotal": mark, "savings": naive - mark,
            "efficiency": (naive - mark) / naive}


def run_efficiency(chunks: list[dict[str, Any]], cfg: dict[str, int]) -> dict[str, float]:
    used = sum(c["tokens"] for c in chunks)
    standard, prefix = 0, 0
    for c in chunks:
        standard += prefix + c["tokens"]
        prefix += c["tokens"]
    mark = len(chunks) * cfg["carryoverTokens"] + used
    saved = max(0, standard - mark)
    return {"totalTokensUsed": used, "standardCost": standard, "markovianCost": mark,
            "tokensSaved": saved, "efficiencyPercent": (saved / standard * 100) if standard else 0.0}


_STATE_TAILS = [r"\[STATE_CHECKPOINT\]\s*([\s\S]*)$", r"@@@STATE@@@\s*([\s\S]*)$",
                r"(?<!_)\[STATE\]\s*([\s\S]*)$", r"---STATE---\s*([\s\S]*)$"]
_FINAL = [r"\[TASK_COMPLETE\]", r"@@@FINAL@@@", r"(?<!_)\[FINAL\]", r"---FINAL---"]


def parse_markers(raw: str) -> dict[str, Any]:
    state = None
    for p in _STATE_TAILS:
        m = re.search(p, raw)
        if m:
            state = m.group(1).strip()
            break
    return {"state": state, "done": any(re.search(p, raw) for p in _FINAL), "cleaned": clean_markers(raw)}


def clean_markers(raw: str) -> str:
    out = raw
    for p in (r"\[STATE_CHECKPOINT\][\s\S]*$", r"@@@STATE@@@[\s\S]*$", r"---STATE---[\s\S]*$", r"\n\[STATE\][\s\S]*$"):
        out = re.sub(p, "", out)
    for p in (r"\[TASK_COMPLETE\]", r"@@@FINAL@@@", r"---FINAL---"):
        out = re.sub(p, "", out)
    out = re.sub(r"\n\[FINAL\][\s\S]*$", "", out)
    out = re.sub(r"[ \t]+$", "", out, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def compress_carryover(state: str | None, cleaned: str, budget_tokens: int) -> str:
    """Bound the carryover to the token budget; keep the canonical labeled block."""
    text = state if state else f"COMPLETED: {cleaned[-600:]}\nREMAINING: unknown\nDECISIONS: none\nCONTEXT: none"
    limit = budget_tokens * 4
    return text if len(text) <= limit else text[:limit]


MARKER_RULES = ("End every non-final chunk with [STATE_CHECKPOINT] followed by a concise state block:\n"
                "COMPLETED: ...\nREMAINING: ...\nDECISIONS: ...\nCONTEXT: ...\n"
                "End the final chunk with [TASK_COMPLETE]. Exactly one marker per chunk. Never narrate the markers.")


def build_prompt(goal: str, step: int, carryover: str, overlap: str, context: str = "") -> str:
    parts: list[str] = []
    if step == 1:
        if context:
            parts += [context.strip(), ""]
        parts += ["You are executing a long task in bounded chunks. Plan, then start.", "", f"User Request: {goal}"]
    else:
        parts += [f"Continuing a chunked run. Goal (truncated): {goal[:200]}", "", f"[CARRYOVER STATE]\n{carryover}"]
        if overlap:
            parts += ["", f"[OVERLAP]\n{overlap}"]
        parts += ["", "Continue exactly where the state leaves off. Do not repeat completed work."]
    parts += ["", MARKER_RULES]
    return "\n".join(parts)


Generator = Callable[[str], Any]   # returns {"text": str} or str


@dataclass
class MarkovianRun:
    id: str
    timestamp: float
    prompt: str
    mode: str
    chunkConfig: dict[str, int]
    chunks: list[dict[str, Any]] = field(default_factory=list)
    output: str = ""
    status: str = "completed"
    tokenLabel: str = "estimated"
    totalTokensUsed: int = 0
    totalTokensSaved: int = 0
    efficiencyPercent: float = 0.0
    durationMs: int = 0
    reflection: str | None = None
    promptTokens: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Markovian:
    def __init__(self, chunkConfig: dict[str, Any] | None = None, clock: Callable[[], float] | None = None,
                 on_chunk: Callable[[int, str, str], str | None] | None = None) -> None:
        self.config = resolve_config(chunkConfig)
        self.clock = clock or (lambda: time.time() * 1000)
        self.on_chunk = on_chunk   # hook: (step, cleaned, carryover) -> extra carryover text
        self.history: list[MarkovianRun] = []

    def run(self, goal: str, generator: Generator, reflect: bool = True, mode: str = "ARCHITECT",
            context: str = "") -> MarkovianRun:
        cfg = dict(self.config)
        t0 = self.clock()
        run = MarkovianRun(str(uuid.uuid4()), t0, goal[:500], mode, cfg)
        carry, prev_clean, outputs = "", "", []
        try:
            for step in range(1, cfg["maxChunks"] + 1):
                overlap = prev_clean[-cfg["overlapTokens"] * 4:] if cfg["overlapTokens"] and prev_clean else ""
                prompt = build_prompt(goal, step, carry, overlap, context)
                run.promptTokens.append(estimate_tokens(prompt))
                res = generator(prompt)
                text = res["text"] if isinstance(res, dict) else str(res)
                pm = parse_markers(text)
                cleaned = pm["cleaned"]
                run.chunks.append({"id": step, "tokens": estimate_tokens(cleaned), "carryover": carry, "content": cleaned})
                outputs.append(cleaned)
                prev_clean = cleaned
                carry = compress_carryover(pm["state"], cleaned, cfg["carryoverTokens"])
                if self.on_chunk:
                    extra = self.on_chunk(step, cleaned, carry)
                    if extra:
                        carry = compress_carryover(carry + "\n" + extra, "", cfg["carryoverTokens"])
                if pm["done"]:
                    run.status = "completed"
                    break
            else:
                run.status = "max-chunks"
        except Exception:
            run.status = "errored"
            raise
        finally:
            run.output = "\n\n".join(outputs)
            eff = run_efficiency(run.chunks, cfg)
            run.totalTokensUsed = int(eff["totalTokensUsed"])
            run.totalTokensSaved = int(eff["tokensSaved"])
            run.efficiencyPercent = eff["efficiencyPercent"]
            run.durationMs = int(self.clock() - t0)
        if reflect and run.status == "completed":
            try:   # reflection failure never fails the run (spec)
                r = generator(f"Reflect in two sentences on this completed run.\nGoal: {goal[:200]}\nResult: {run.output[-800:]}")
                run.reflection = clean_markers(r["text"] if isinstance(r, dict) else str(r))
            except Exception as e:  # noqa: BLE001
                run.reflection = f"(reflection skipped: {e})"
        self.history.append(run)
        return run
