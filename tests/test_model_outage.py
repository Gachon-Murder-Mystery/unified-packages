"""[FIX-I11] A model outage pauses the game and resumes it instead of aborting it."""
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import model_outage  # noqa: E402
from model_outage import OutageGate, PauseAwareClient  # noqa: E402


class Down(BaseException):
    pass


class Refused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class GateTests(unittest.TestCase):
    def gate(self, **kw):
        g = OutageGate(delays=(0.01,), sleep=lambda s: time.sleep(0.01), **kw)
        self.calls = []
        g.bind(lambda: self.calls.append("PAUSE"), lambda: self.calls.append("RESUME"))
        return g

    def test_outage_pauses_probes_and_resumes(self):
        g = self.gate(max_seconds=60)
        health = iter([("", {}, "down"), ("", {}, "down"), ('{"ok": true}', {}, None)])
        result = g.outage(lambda: next(health), lambda: ("answer", {"output": 3}, None), "first error", Down)
        self.assertEqual(result, ("answer", {"output": 3}))
        self.assertEqual(self.calls, ["PAUSE", "RESUME"])
        [event] = g.events
        self.assertEqual((event["outcome"], event["probes"], event["game_paused"]), ("recovered", 3, True))
        self.assertTrue(g.healthy.is_set() and g.resumed.is_set() and not g.engine_paused)

    def test_other_calls_wait_at_the_gate_and_then_run(self):
        g = self.gate(max_seconds=60)
        release = threading.Event()

        def probe():
            return ('{"ok": true}', {}, None) if release.is_set() else ("", {}, "down")
        leader = threading.Thread(target=lambda: g.outage(probe, lambda: ("a", {}, None), "e", Down))
        leader.start()
        while g.healthy.is_set():
            time.sleep(0.005)
        got = []
        waiter = threading.Thread(target=lambda: got.append(g.outage(probe, lambda: ("b", {}, None), "e", Down)))
        waiter.start()
        time.sleep(0.05)
        self.assertEqual(got, [])                      # still waiting: no stand-in decision
        release.set()
        leader.join(2)
        waiter.join(2)
        self.assertEqual(got, [("b", {})])
        self.assertEqual(len(g.events), 1)             # one outage, one prober

    def test_an_outage_that_lasts_too_long_still_aborts(self):
        g = self.gate(max_seconds=0)
        with self.assertRaises(Down):
            g.outage(lambda: ("", {}, "down"), lambda: ("x", {}, None), "e", Down)
        self.assertEqual(self.calls, ["PAUSE", "RESUME"])   # the game is not left paused
        with self.assertRaises(Down):
            g.wait_healthy(Down)                       # later calls abort too
        self.assertEqual(g.events[0]["outcome"], "gave_up")

    def test_only_the_original_request_failing_is_a_single_failure(self):
        g = self.gate(max_seconds=60)
        with self.assertRaises(RuntimeError):          # B falls back once, counted
            g.outage(lambda: ('{"ok": true}', {}, None), lambda: ("", {}, "bad prompt"), "e", Down)

    def test_engine_calls_refused_while_paused_wait_for_resume(self):
        g = self.gate(max_seconds=60)
        attempts = []

        class Engine:
            def bind(self, **_):
                return self

            def call(self, route, **data):
                attempts.append(route)
                if len(attempts) == 1:
                    raise Refused("not_running")
                return {"ok": True}
        client = PauseAwareClient(Engine(), g).bind(token="t")
        g.engine_paused = True
        g.resumed.clear()
        threading.Timer(0.05, lambda: (setattr(g, "engine_paused", False), g.resumed.set())).start()
        self.assertEqual(client.call("agent/action", x=1), {"ok": True})
        self.assertEqual(attempts, ["agent/action", "agent/action"])
        with self.assertRaises(Refused):               # not paused: the refusal is real
            PauseAwareClient(type("E", (), {"call": lambda self, r, **d: (_ for _ in ()).throw(Refused("not_running"))})(), g).call("x")


class CodexClientTests(unittest.TestCase):
    def test_codex_client_rides_out_an_outage(self):
        import offline_llm
        gate = OutageGate(delays=(0.01,), sleep=lambda s: None, max_seconds=60)
        old = model_outage.GATE
        model_outage.GATE = gate
        try:
            client = offline_llm.CodexCLIClient.__new__(offline_llm.CodexCLIClient)
            answers = iter([("", {}, "e1"), ("", {}, "e2"), ("", {}, "e3"),     # 3 failures: outage
                            ('{"ok": true}', {}, None),                         # health check
                            ('{"decision": 1}', {"output": 5}, None)])          # my request again
            kinds = []

            def attempt(system, user, kind="agent"):
                kinds.append(kind)
                text, usage, error = next(answers)
                offline_llm.CodexCLIClient._failures = offline_llm.CodexCLIClient._failures + 1 if error else 0
                return text, usage, error
            client._attempt = attempt
            offline_llm.CodexCLIClient._failures = 0
            for _ in range(2):
                with self.assertRaises(RuntimeError):                         # single failures
                    client.request("s", "u")
            self.assertEqual(client.request("s", "u"), ('{"decision": 1}', {"output": 5}))
            self.assertEqual(kinds, ["agent", "agent", "agent", "health_check", "agent"])
            self.assertEqual(gate.events[0]["outcome"], "recovered")
        finally:
            model_outage.GATE = old
            offline_llm.CodexCLIClient._failures = 0


if __name__ == "__main__":
    unittest.main()
