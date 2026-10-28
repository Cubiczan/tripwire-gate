"""Policy + ledger storage.

PRIMARY: Zetaris (Lightning SQL over the REST API described in the hackathon
setup guide: POST {ZETARIS_REST_URL}/sql-editor/sqls/run with a Bearer API key,
X-Org-ID and a fresh UUID X-Request-ID, one statement per request).

FALLBACK ONLY: a local SQLite file, used when Zetaris is not configured or not
reachable, so the demo still runs. Everything it writes is labeled
backend="sqlite-fallback" and is NOT Zetaris evidence.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Protocol

from .policy import SEED_RULES, Rule

RULE_COLS = ["rule_id", "action", "effect", "reason", "data_classes", "min_amount",
             "max_amount", "dest_not_suffix", "agents"]
LEDGER_COLS = ["ts", "run_id", "chunk", "agent_id", "action", "resource", "data_class",
               "amount", "destination", "verdict", "matched", "explanation", "policy_version"]


class Store(Protocol):
    backend: str
    def init(self) -> None: ...
    def load_rules(self) -> list[Rule]: ...
    def append(self, row: dict[str, Any]) -> None: ...
    def ledger(self, run_id: str | None = None) -> list[dict[str, Any]]: ...


def _q(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(float(v))
    return "'" + str(v).replace("'", "''") + "'"


class ZetarisError(RuntimeError):
    pass


class ZetarisStore:
    backend = "zetaris"

    def __init__(self, rest_url: str, api_key: str, org_id: str = "1", schema: str = "tripwire",
                 timeout: float = 60.0, datasource: str | None = None) -> None:
        self.rest_url = rest_url.rstrip("/")
        # Self-hosted Zetaris (hackathon Docker kit): Lightning cannot CREATE TABLE inside a
        # JDBC datasource, so the two tables are created once in the source DB
        # (scripts/zetaris_local_setup.sh) and then every read/write goes through Zetaris
        # as <datasource>.<table>.
        self.datasource = datasource
        self.prefix = f"{datasource}." if datasource else ""
        self._key = api_key
        self.org_id = org_id
        self.schema = schema
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "ZetarisStore | None":
        url = os.environ.get("ZETARIS_REST_URL")
        if not url and os.environ.get("ZETARIS_URL"):
            base = os.environ["ZETARIS_URL"].rstrip("/")
            if not base.startswith("http"):
                base = "https://" + base
            url = base + "/api/v1.0"
        key = os.environ.get("ZETARIS_API_KEY")
        if not url or not key:
            return None
        return cls(url, key, os.environ.get("ZETARIS_ORG_ID", "1"),
                   os.environ.get("ZETARIS_SCHEMA", "tripwire"),
                   datasource=os.environ.get("ZETARIS_DATASOURCE") or None)

    def sql(self, statement: str, limit: int = 1000) -> dict[str, Any]:
        body = json.dumps({"queryId": str(uuid.uuid4()), "sql": statement,
                           "source": "SqlEditor", "limit": limit}).encode()
        req = urllib.request.Request(
            self.rest_url + "/sql-editor/sqls/run", data=body, method="POST",
            headers={"Authorization": "Bearer " + self._key, "X-Org-ID": str(self.org_id),
                     "X-Request-ID": str(uuid.uuid4()), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise ZetarisError(f"Zetaris HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from None
        except urllib.error.URLError as e:
            raise ZetarisError(f"Zetaris unreachable at {self.rest_url}: {e.reason}") from None
        if isinstance(resp, dict) and "msg" in resp and "data" not in resp:
            raise ZetarisError(f"Zetaris SQL error: {str(resp['msg'])[:300]}")
        return resp

    @staticmethod
    def _rows(resp: dict[str, Any], cols: list[str]) -> list[dict[str, Any]]:
        data = resp.get("data") or []
        header = resp.get("columns") or resp.get("header") or resp.get("schema")
        names = cols
        if isinstance(header, list) and header:
            names = [h.get("name") if isinstance(h, dict) else str(h) for h in header]
        out = []
        for row in data:
            if isinstance(row, dict):
                out.append({k.lower(): v for k, v in row.items()})
            else:
                out.append({str(n).lower(): v for n, v in zip(names, row)})
        return out

    def ping(self) -> bool:
        resp = self.sql("SELECT 1", limit=1)
        data = resp.get("data")
        return bool(data) and str((data[0][0] if isinstance(data[0], list) else list(data[0].values())[0])) == "1"

    def t(self, name: str) -> str:
        return f"{self.prefix}{self.schema}_{name}"

    def init(self) -> None:
        s = self.schema
        if self.datasource:
            self.sql(f"REGISTER DATASOURCE TABLES FROM {self.datasource}")
            self._seed()
            return
        stmts = [
            f"CREATE TABLE IF NOT EXISTS {s}_policy_rules (rule_id STRING, action STRING, effect STRING, reason STRING, "
            f"data_classes STRING, min_amount DOUBLE, max_amount DOUBLE, dest_not_suffix STRING, agents STRING)",
            f"CREATE TABLE IF NOT EXISTS {s}_ledger (ts DOUBLE, run_id STRING, chunk INT, agent_id STRING, action STRING, "
            f"resource STRING, data_class STRING, amount DOUBLE, destination STRING, verdict STRING, matched STRING, "
            f"explanation STRING, policy_version STRING)",
        ]
        for st in stmts:
            self.sql(st)
        self._seed()

    def _seed(self) -> None:
        s = self.schema
        if not self.load_rules():
            for r in SEED_RULES:
                vals = ", ".join(_q(r.get(c)) for c in RULE_COLS)
                self.sql(f"INSERT INTO {self.t('policy_rules')} VALUES ({vals})")

    def load_rules(self) -> list[Rule]:
        resp = self.sql(f"SELECT {', '.join(RULE_COLS)} FROM {self.t('policy_rules')}")
        return [Rule.from_row(r) for r in self._rows(resp, RULE_COLS)]

    def append(self, row: dict[str, Any]) -> None:
        vals = ", ".join(_q(row.get(c)) if c != "chunk" else str(int(row.get(c, 0))) for c in LEDGER_COLS)
        self.sql(f"INSERT INTO {self.t('ledger')} VALUES ({vals})")

    def ledger(self, run_id: str | None = None) -> list[dict[str, Any]]:
        where = f" WHERE run_id = {_q(run_id)}" if run_id else ""
        resp = self.sql(f"SELECT {', '.join(LEDGER_COLS)} FROM {self.t('ledger')}{where} ORDER BY ts")
        return self._rows(resp, LEDGER_COLS)


class SqliteFallbackStore:
    """FALLBACK ONLY. Not Zetaris. Lets the demo run when Zetaris is unavailable."""
    backend = "sqlite-fallback"

    def __init__(self, path: str = "tripwire_fallback.db") -> None:
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row

    def init(self) -> None:
        self.db.execute(f"CREATE TABLE IF NOT EXISTS policy_rules ({', '.join(RULE_COLS)})")
        self.db.execute(f"CREATE TABLE IF NOT EXISTS ledger ({', '.join(LEDGER_COLS)})")
        if not self.db.execute("SELECT count(*) FROM policy_rules").fetchone()[0]:
            self.db.executemany(
                f"INSERT INTO policy_rules VALUES ({', '.join('?' * len(RULE_COLS))})",
                [[r.get(c) for c in RULE_COLS] for r in SEED_RULES])
        self.db.commit()

    def load_rules(self) -> list[Rule]:
        return [Rule.from_row(dict(r)) for r in self.db.execute("SELECT * FROM policy_rules")]

    def append(self, row: dict[str, Any]) -> None:
        self.db.execute(f"INSERT INTO ledger VALUES ({', '.join('?' * len(LEDGER_COLS))})",
                        [row.get(c) for c in LEDGER_COLS])
        self.db.commit()

    def ledger(self, run_id: str | None = None) -> list[dict[str, Any]]:
        q, a = "SELECT * FROM ledger", []
        if run_id:
            q, a = q + " WHERE run_id = ?", [run_id]
        return [dict(r) for r in self.db.execute(q + " ORDER BY ts", a)]


def open_store(log=print) -> tuple[Store, str]:
    """Zetaris if configured and reachable, else the labeled SQLite fallback."""
    z = ZetarisStore.from_env()
    if z is None:
        note = "Zetaris not configured (ZETARIS_REST_URL/ZETARIS_URL + ZETARIS_API_KEY unset) -> using SQLite FALLBACK"
    else:
        try:
            z.init()
            return z, f"Zetaris store at {z.rest_url} (tables {z.t('policy_rules')}, {z.t('ledger')})"
        except ZetarisError as e:
            note = f"Zetaris configured but failed ({e}) -> using SQLite FALLBACK"
    s = SqliteFallbackStore(os.environ.get("TRIPWIRE_FALLBACK_DB", "tripwire_fallback.db"))
    s.init()
    log("WARNING: " + note)
    return s, note


def now() -> float:
    return time.time()
