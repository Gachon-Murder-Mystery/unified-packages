"""FIX-C20: the GM decides the model-call deadline (learned) and what happens after a stuck call."""
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import model_outage                                   # noqa: E402
from offline_llm import CodexCLIClient                # noqa: E402
from GM_Agent_Society.gm_memory import GMMemory       # noqa: E402

STUCK = "codex exec could not run: TimeoutExpired: Command [...] timed out after 300 seconds"


class FakeClient(CodexCLIClient):
    def __init__(self, results):
        super().__init__("codex", Path(tempfile.mkdtemp()) / "calls.jsonl")
        self.results = list(results)
        self.calls = []

    def _attempt(self, system, user, kind="agent", remedy=None):
        self.calls.append(remedy)
        text, error = self.results.pop(0)
        CodexCLIClient._failures = CodexCLIClient._failures + 1 if error else 0
        return text, {"output": 1}, error


class DeadlineTests(unittest.TestCase):
    def test_the_deadline_follows_this_pool_once_there_is_data(self):
        m = GMMemory("gm", directory=None)
        self.assertEqual(m.model_deadline(), 300.0)              # the old fixed cap until 20 calls
        for s in [10] * 18 + [25, 25]:
            m.model_call(s)
        self.assertEqual(m.model_deadline(), 60.0)               # 4 x p90 (10 s) = 40 -> floor 60
        for s in [30] * 40:
            m.model_call(s)
        self.assertEqual(m.model_deadline(), 120.0)              # 4 x 30

    def test_without_a_gm_the_client_keeps_its_cap(self):
        policy = model_outage.ModelCallPolicy()
        self.assertEqual(policy.deadline(300), 300)
        self.assertEqual(policy.remedy(), "stand_in")


class StuckCallTests(unittest.TestCase):
    def setUp(self):
        CodexCLIClient._failures = 0
        self.memory = GMMemory("gm", directory=None, rng=random.Random(1))
        model_outage.MODEL_POLICY.bind(self.memory)
        self.addCleanup(model_outage.MODEL_POLICY.bind, None)

    def test_the_gm_first_retries_and_a_good_retry_keeps_the_model_decision(self):
        client = FakeClient([("", STUCK), ('{"ok": 1}', None)])
        self.assertEqual(client.request("s", "u")[0], '{"ok": 1}')
        self.assertEqual(client.calls, [None, "retry"])
        self.assertEqual(self.memory.data["playbook"]["model_timeout"]["retry"]["resolved"], 1)

    def test_a_failed_retry_is_a_single_failure_and_is_learned(self):
        client = FakeClient([("", STUCK), ("", STUCK)])
        with self.assertRaises(RuntimeError):
            client.request("s", "u")
        self.assertEqual(self.memory.data["playbook"]["model_timeout"]["retry"]["tries"], 1)
        self.assertEqual(self.memory.data["playbook"]["model_timeout"]["retry"]["resolved"], 0)

    def test_other_errors_are_not_the_gms_call(self):
        client = FakeClient([("", "codex exec failed (1): bad request")])
        with self.assertRaises(RuntimeError):
            client.request("s", "u")
        self.assertEqual(client.calls, [None])
        self.assertNotIn("model_timeout", self.memory.data["playbook"])


if __name__ == "__main__":
    unittest.main()
