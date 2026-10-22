"""Tripwire CLI.

  python -m tripwire check                 # Meterless + Zetaris connection checks
  python -m tripwire demo [--offline]      # Markovian agent run through the gate
  python -m tripwire decide '<intent json>'
  python -m tripwire ledger [--run RUN_ID]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .gate import Gate, to_json
from .intents import Intent
from .meterless import MeterlessClient, MeterlessError
from .scenario import DEMO_TASK, scripted_chat
from .store import ZetarisError, ZetarisStore, open_store


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cmd_check(_: argparse.Namespace) -> int:
    ok = True
    try:
        mc = MeterlessClient()
        st = mc.status()
        print(f"Meterless runtime {mc.base_url}: {st}")
        if st == "online":
            ids = mc.models()
            print("  routes:", ", ".join(i for i in ids if i.startswith("meterless/"))[:400])
            text, rc, _ = mc.chat([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=16)
            print(f"  chat on {rc.requested_model} -> routed_model={rc.routed_model} ({rc.latency_ms} ms) reply={text.strip()[:40]!r}")
            print(f"  Nemotron lane: {'YES' if rc.is_nemotron else 'NO'}")
        ok &= st == "online"
    except MeterlessError as e:
        print(f"Meterless: {e.state}: {e}")
        ok = False
    z = ZetarisStore.from_env()
    if z is None:
        print("Zetaris: NOT CONFIGURED (set ZETARIS_REST_URL or ZETARIS_URL, and ZETARIS_API_KEY)")
        ok = False
    else:
        try:
            print(f"Zetaris {z.rest_url}: SELECT 1 -> {'PASS' if z.ping() else 'unexpected reply'}")
        except ZetarisError as e:
            print(f"Zetaris: FAIL {e}")
            ok = False
    return 0 if ok else 1


def cmd_demo(a: argparse.Namespace) -> int:
    store, note = open_store()
    print(f"[store] backend={store.backend}  {note}")
    gate = Gate(store)
    print(f"[policy] {len(gate.rules)} rules loaded from {store.backend}")
    if a.offline:
        print("[provider] OFFLINE scripted generator (NOT Meterless, NOT evidence)")
        chat = scripted_chat()
    else:
        mc = MeterlessClient(model=a.model)
        print(f"[provider] Meterless Runtime {mc.base_url} model={mc.model} app=tripwire")

        def chat(messages):
            text, rc, _ = mc.chat(messages, max_tokens=a.max_tokens)
            print(f"  [meterless] {rc.requested_model} -> {rc.routed_model} ({rc.latency_ms} ms)")
            return text, rc.to_dict()
    print("[engine] Markovian: bounded chunks, carryover only, gate decisions folded into carryover\n")
    gr = gate.run_agent(DEMO_TASK, chat, {"chunkSize": 4000, "carryoverTokens": 640, "maxChunks": a.max_chunks})
    m = gr.markovian
    print(f"\n[result] run_id={gr.run_id} status={m.status} chunks={len(m.chunks)} decisions={gr.summary()}")
    print(f"[engine] prompt tokens per chunk (estimated): {m.promptTokens}")
    print(f"[engine] efficiency vs full-history baseline: {m.efficiencyPercent:.1f}% "
          f"({m.totalTokensSaved} tokens saved, {m.tokenLabel})")
    base = m.promptTokens[0] if m.promptTokens else 0
    naive, acc = [], 0
    for c in m.chunks:
        naive.append(base + acc)
        acc += c["tokens"]
    print(f"[engine] full-history prompt tokens would have been:  {naive}")
    nem = [r for r in gr.receipts if r.get("is_nemotron")]
    print(f"[receipts] {len(nem)}/{len(gr.receipts)} model calls served by a Nemotron model")
    led = store.ledger(gr.run_id)
    print(f"[ledger] {len(led)} rows written to {store.backend} for this run")
    if a.evidence:
        d = Path(a.evidence)
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        tag = "offline" if a.offline else "meterless"
        (d / f"run-{tag}-{stamp}.json").write_text(to_json(gr))
        (d / f"receipts-{tag}-{stamp}.json").write_text(json.dumps(gr.receipts, indent=2, default=str))
        (d / f"ledger-{store.backend}-{stamp}.json").write_text(json.dumps(led, indent=2, default=str))
        print(f"[evidence] written to {d}/ (*-{stamp}.json)")
    return 0


def cmd_decide(a: argparse.Namespace) -> int:
    store, _ = open_store()
    d = Gate(store).decide(Intent.from_dict(json.loads(a.intent)))
    print(json.dumps(d.to_dict(), indent=2))
    return {"allow": 0, "review": 3, "deny": 4}[d.verdict]


def cmd_ledger(a: argparse.Namespace) -> int:
    store, _ = open_store()
    for r in store.ledger(a.run):
        print(f"{r['verdict']:>6}  {r['agent_id']}  {r['action']}  {r['resource']}  [{r['matched']}]")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="tripwire")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    d = sub.add_parser("demo")
    d.add_argument("--offline", action="store_true", help="scripted generator, no model (not evidence)")
    d.add_argument("--model", default=None, help="Meterless route, default $METERLESS_MODEL or meterless/auto")
    d.add_argument("--max-chunks", type=int, default=5)
    d.add_argument("--max-tokens", type=int, default=900)
    d.add_argument("--evidence", default=None, help="directory to write run/receipt/ledger JSON")
    d.set_defaults(fn=cmd_demo)
    x = sub.add_parser("decide")
    x.add_argument("intent")
    x.set_defaults(fn=cmd_decide)
    lg = sub.add_parser("ledger")
    lg.add_argument("--run", default=None)
    lg.set_defaults(fn=cmd_ledger)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except MeterlessError as e:
        print(f"Meterless {e.state}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
