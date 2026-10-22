"""The gate: agent proposes typed intents -> deterministic policy -> decision -> ledger.

The Markovian engine drives the agent's long job in bounded chunks; after every
chunk the gate evaluates that chunk's intents and its decisions are written into
the carryover, so the next chunk knows what was allowed, denied or sent for
review without ever replaying the full history.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .intents import Intent, extract_intents
from .markovian import Markovian, MarkovianRun
from .policy import Decision, Rule, evaluate
from .store import Store, now

AGENT_SYSTEM = """You are ops-agent, an autonomous operations agent at Acme. You cannot act directly.
Every action you want must be proposed to the Tripwire governance gate as ONE LINE:
INTENT {"action": "...", "resource": "...", "data_class": "...", "amount": 0, "destination": "", "justification": "..."}
Allowed actions: read_record, export_data, send_email, issue_refund, pay_vendor, change_permissions, delete_record, run_sql.
data_class is one of public, internal, confidential, pii, regulated. amount is in USD.
Propose at most 3 intents per chunk. The gate's decisions arrive in your carryover under GATE DECISIONS.
If something is denied, do not retry it; propose a safer alternative or hand it to a human."""


@dataclass
class GateRun:
    run_id: str
    decisions: list[Decision] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    markovian: MarkovianRun | None = None
    receipts: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        out = {"allow": 0, "review": 0, "deny": 0}
        for d in self.decisions:
            out[d.verdict] += 1
        return out


class Gate:
    def __init__(self, store: Store, agent_id: str = "ops-agent") -> None:
        self.store = store
        self.agent_id = agent_id
        self.rules: list[Rule] = store.load_rules()

    def decide(self, intent: Intent, run_id: str = "adhoc", chunk: int = 0) -> Decision:
        d = evaluate(intent, self.rules)
        i = d.intent
        self.store.append({
            "ts": now(), "run_id": run_id, "chunk": chunk, "agent_id": i.agent_id, "action": i.action,
            "resource": i.resource, "data_class": i.data_class, "amount": i.amount, "destination": i.destination,
            "verdict": d.verdict, "matched": ",".join(d.matched), "explanation": d.explain(),
            "policy_version": d.policy_version})
        return d

    def run_agent(self, task: str, chat: Callable[[list[dict[str, str]]], tuple[str, dict]],
                  chunk_config: dict[str, Any] | None = None, log: Callable[[str], None] = print) -> GateRun:
        gr = GateRun(run_id="")

        def on_chunk(step: int, cleaned: str, carry: str) -> str:
            intents, errs = extract_intents(cleaned, self.agent_id)
            gr.errors += errs
            lines = []
            for e in errs:
                log(f"  [chunk {step}] {e}")
            for it in intents:
                d = self.decide(it, gr.run_id, step)
                gr.decisions.append(d)
                log(f"  [chunk {step}] {d.explain()}")
                lines.append(f"- {d.verdict.upper()} {it.action} {it.resource} ({', '.join(d.matched) or 'default-deny'})")
            if not intents:
                lines.append("- (no intents proposed this chunk)")
            return "GATE DECISIONS:\n" + "\n".join(lines)

        def generator(prompt: str) -> dict[str, str]:
            text, receipt = chat([{"role": "system", "content": AGENT_SYSTEM}, {"role": "user", "content": prompt}])
            gr.receipts.append(receipt)
            return {"text": text}

        engine = Markovian(chunk_config or {"chunkSize": 4000, "carryoverTokens": 640, "maxChunks": 5}, on_chunk=on_chunk)
        # run id must exist before chunks are evaluated
        import uuid
        gr.run_id = str(uuid.uuid4())
        gr.markovian = engine.run(task, generator, reflect=False)
        return gr


def to_json(gr: GateRun) -> str:
    m = gr.markovian
    return json.dumps({
        "run_id": gr.run_id, "summary": gr.summary(), "errors": gr.errors,
        "decisions": [d.to_dict() for d in gr.decisions],
        "markovian": {"status": m.status, "chunks": len(m.chunks), "promptTokensPerChunk": m.promptTokens,
                      "tokenLabel": m.tokenLabel, "totalTokensUsed": m.totalTokensUsed,
                      "tokensSaved": m.totalTokensSaved, "efficiencyPercent": round(m.efficiencyPercent, 1),
                      "chunkConfig": m.chunkConfig} if m else None,
        "receipts": gr.receipts,
    }, indent=2, default=str)
