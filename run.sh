#!/usr/bin/env bash
# Tripwire: tests -> connection check -> demo run through Meterless, with evidence capture.
# Needs Python 3.10+ only (stdlib). Secrets come from the environment or .env, never from this file.
set -uo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
EV=${EVIDENCE_DIR:-docs/evidence}
mkdir -p "$EV"
STAMP=$(date +%Y%m%d-%H%M%S)
LOG="$EV/console-$STAMP.txt"
{
  echo "# Tripwire run $STAMP ($(date '+%Z'))  host=$(uname -sm)  python=$($PY -V 2>&1)"
  echo "## 1. unit + Markovian conformance tests"
  $PY -m unittest discover -s tests 2>&1 | tail -4
  echo "## 2. connection check (Meterless + Zetaris)"
  $PY -m tripwire check
  echo "## 3. demo: ops-agent backlog through the gate (Markovian engine, Meterless provider)"
  if [ "${OFFLINE:-0}" = "1" ]; then $PY -m tripwire demo --offline --evidence "$EV"
  else $PY -m tripwire demo --evidence "$EV" ${MODEL:+--model "$MODEL"}; fi
} 2>&1 | tee "$LOG"
# scrub anything token-shaped, just in case
[ -n "${METERLESS_RUNTIME_TOKEN:-}" ] && sed -i.bak "s/${METERLESS_RUNTIME_TOKEN}/[REDACTED]/g" "$EV"/*.json "$LOG" && rm -f "$EV"/*.bak
echo "console log: $LOG"
