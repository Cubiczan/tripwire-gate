"""Python port of the official Markovian conformance suite
(Meterless/Meterless engines/markovian/conformance/runner.ts), check-for-check."""
import unittest

from tripwire.markovian import ConfigValidationError, Markovian, model_totals, run_efficiency

T0 = 1_750_000_000_000


def step_gen(total):
    calls = {"n": 0}

    def fn(prompt):
        calls["n"] += 1
        n = calls["n"]
        body = f"Output for step {n} with enough text to register in the accounting math. " * 10
        marker = "[TASK_COMPLETE]" if n >= total else f"[STATE_CHECKPOINT]\nCOMPLETED: steps 1-{n}\nREMAINING: {total - n}\nDECISIONS: none\nCONTEXT: conformance"
        return {"text": body + "\n" + marker}
    return fn


class Conformance(unittest.TestCase):
    def test_eff_001_worked_example(self):
        t = model_totals(20)
        self.assertEqual(t["naiveTotal"], 246_000)
        self.assertEqual(t["markovianTotal"], 34_000)
        self.assertLess(abs(t["efficiency"] * 100 - 86.18), 0.5)

    def test_snap_001_snapshot(self):
        m = Markovian({"chunkSize": 4000, "carryoverTokens": 640}, clock=lambda: T0)
        run = m.run("snapshot check", step_gen(3), reflect=False)
        self.assertEqual(run.chunkConfig["carryoverTokens"], 640)

    def test_snap_002_historical_efficiency(self):
        chunks = [{"id": i, "tokens": 1000, "carryover": "", "content": ""} for i in range(1, 5)]
        a = run_efficiency(chunks, {"chunkSize": 8000, "maxChunks": 24, "carryoverTokens": 512, "overlapTokens": 0})
        b = run_efficiency(chunks, {"chunkSize": 8000, "maxChunks": 24, "carryoverTokens": 2048, "overlapTokens": 0})
        self.assertEqual(a["standardCost"], 10_000)
        self.assertEqual(a["markovianCost"], 6_048)
        self.assertNotEqual(a["tokensSaved"], b["tokensSaved"])

    def test_ovl_001_overlap_implemented(self):
        m = Markovian({"chunkSize": 4000, "carryoverTokens": 640, "overlapTokens": 64}, clock=lambda: T0)
        prompts = []

        def gen(p):
            prompts.append(p)
            done = len(prompts) >= 3
            return {"text": f"chunk {len(prompts)} content " * 20 + ("[TASK_COMPLETE]" if done else "[STATE_CHECKPOINT]\nCOMPLETED: chunk recorded with carried state")}
        m.run("overlap conformance", gen, reflect=False)
        self.assertTrue(any("[OVERLAP]" in p for p in prompts[1:]))

    def test_lab_001_token_label(self):
        run = Markovian({"chunkSize": 4000, "carryoverTokens": 640}, clock=lambda: T0).run("label", step_gen(2), reflect=False)
        self.assertIn(run.tokenLabel, ("estimated", "measured"))

    def test_ref_001_reflection_failure(self):
        calls = {"n": 0}

        def gen(p):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("provider exploded in reflection")
            return {"text": "done in one chunk [TASK_COMPLETE]"}
        run = Markovian({"chunkSize": 4000, "carryoverTokens": 640}, clock=lambda: T0).run("reflection failure", gen)
        self.assertEqual(run.status, "completed")

    def test_mrk_001_markers(self):
        run = Markovian({"chunkSize": 4000, "carryoverTokens": 640}, clock=lambda: T0).run("markers", step_gen(3), reflect=False)
        self.assertEqual(run.status, "completed")
        self.assertNotRegex(run.output, "STATE_CHECKPOINT|TASK_COMPLETE")
        self.assertEqual(len(run.chunks), 3)

    def test_cfg_001_bounds(self):
        with self.assertRaises(ConfigValidationError):
            Markovian({"chunkSize": 128, "carryoverTokens": 640})

    def test_flat_context(self):
        """Prompt size stays bounded as the chain grows (the point of the engine)."""
        run = Markovian({"chunkSize": 4000, "carryoverTokens": 640, "maxChunks": 12}).run("long", step_gen(12), reflect=False)
        self.assertEqual(len(run.chunks), 12)
        self.assertLess(max(run.promptTokens[1:]) - min(run.promptTokens[1:]), 50)


if __name__ == "__main__":
    unittest.main()
