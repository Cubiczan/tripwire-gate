import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tripwire.gate import Gate
from tripwire.intents import Intent, IntentError, extract_intents
from tripwire.meterless import MeterlessClient, MeterlessError, normalise_base
from tripwire.policy import evaluate, seed_rules
from tripwire.scenario import DEMO_TASK, scripted_chat
from tripwire.store import SqliteFallbackStore, ZetarisStore


def I(**kw):
    kw.setdefault("agent_id", "ops-agent")
    kw.setdefault("resource", "x")
    return Intent(**kw)


class PolicyTests(unittest.TestCase):
    rules = seed_rules()

    def v(self, **kw):
        return evaluate(I(**kw), self.rules).verdict

    def test_refund_threshold(self):
        self.assertEqual(self.v(action="issue_refund", amount=200), "allow")
        self.assertEqual(self.v(action="issue_refund", amount=200.5), "review")

    def test_deny_beats_review(self):
        self.assertEqual(self.v(action="pay_vendor", amount=9999), "review")
        self.assertEqual(self.v(action="pay_vendor", amount=10000), "deny")

    def test_pii_export_denied(self):
        self.assertEqual(self.v(action="export_data", data_class="pii"), "deny")
        self.assertEqual(self.v(action="export_data", data_class="internal"), "allow")

    def test_email_domains(self):
        self.assertEqual(self.v(action="send_email", destination="a@acme.example"), "allow")
        self.assertEqual(self.v(action="send_email", destination="a@evil.example"), "review")

    def test_default_deny(self):
        d = evaluate(I(action="run_sql", data_class="pii"), self.rules)
        self.assertEqual(d.verdict, "deny")
        self.assertIn("default deny", d.explain())

    def test_deterministic(self):
        i = I(action="issue_refund", amount=1450)
        self.assertEqual(evaluate(i, self.rules).to_dict(), evaluate(i, list(reversed(self.rules))).to_dict())


class IntentTests(unittest.TestCase):
    def test_rejects_unknown_action(self):
        with self.assertRaises(IntentError):
            I(action="launch_missiles")

    def test_extract(self):
        txt = 'hi\nINTENT {"action": "read_record", "resource": "r1"}\nINTENT {"action": "nope", "resource": "r"}\nINTENT {bad json}'
        ok, errs = extract_intents(txt, "ops-agent")
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(errs), 2)


class GateRunTests(unittest.TestCase):
    def test_offline_scenario_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            store = SqliteFallbackStore(os.path.join(d, "t.db"))
            store.init()
            gr = Gate(store).run_agent(DEMO_TASK, scripted_chat(), log=lambda *_: None)
            self.assertEqual(gr.markovian.status, "completed")
            self.assertEqual(gr.summary(), {"allow": 3, "review": 2, "deny": 3})
            self.assertEqual(len(store.ledger(gr.run_id)), 8)
            # gate decisions reach the next chunk through carryover only
            self.assertIn("GATE DECISIONS", gr.markovian.chunks[1]["carryover"])


class _Mock(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        _Mock.seen.append(dict(self.headers))
        if self.headers.get("Authorization") != "Bearer good":
            return self._send(401, {"error": {"message": "bad token"}})
        self._send(200, {"data": [{"id": "meterless/auto"}, {"id": "meterless/fast"}]})

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(n))
        _Mock.seen.append(dict(self.headers))
        if self.path.endswith("/sql-editor/sqls/run"):
            return self._send(200, {"data": [[1]]})
        self._send(200, {"model": body["model"], "choices": [{"message": {"content": "ready"}}],
                         "meterless": {"routed_model": "nvidia/nemotron-3.5-lightning-30b-a3b"}})


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Mock)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_normalise(self):
        self.assertEqual(normalise_base("127.0.0.1:8088/v1/chat/completions"), "http://127.0.0.1:8088/v1")
        self.assertEqual(normalise_base("http://h:1/v1"), "http://h:1/v1")

    def test_unauthorized_not_offline(self):
        self.assertEqual(MeterlessClient(self.base, "wrong").status(), "unauthorized")

    def test_missing_token(self):
        with self.assertRaises(MeterlessError):
            MeterlessClient(self.base, "")

    def test_chat_receipt_and_headers(self):
        c = MeterlessClient(self.base, "good")
        self.assertEqual(c.status(), "online")
        text, rc, _ = c.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(text, "ready")
        self.assertTrue(rc.is_nemotron)
        h = {k.lower(): v for k, v in _Mock.seen[-1].items()}
        self.assertEqual(h["meterless-app"], "tripwire")
        self.assertEqual(h["meterless-scope"], "user")

    def test_zetaris_rest_contract(self):
        z = ZetarisStore(self.base + "/api/v1.0", "k", "1")
        self.assertTrue(z.ping())
        h = {k.lower(): v for k, v in _Mock.seen[-1].items()}
        self.assertEqual(h["authorization"], "Bearer k")
        self.assertEqual(h["x-org-id"], "1")
        self.assertEqual(len(h["x-request-id"]), 36)


if __name__ == "__main__":
    unittest.main()
