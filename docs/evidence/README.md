# Evidence

Files here are produced by `./run.sh` on the machine running Nightshift.

- `console-*.txt`: full console output (tests, connection check, demo)
- `receipts-meterless-*.json`: one receipt per model call (`requested_model`, `routed_model`, `is_nemotron`, latency, usage)
- `run-meterless-*.json`: decisions with explanations, Markovian stats (chunks, prompt tokens per chunk, config snapshot)
- `ledger-<backend>-*.json`: ledger rows for the run. `ledger-zetaris-*` comes from Zetaris; `ledger-sqlite-fallback-*` is the local fallback and is **not** Zetaris evidence.
