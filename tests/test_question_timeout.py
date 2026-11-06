import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Agent_Reasoning'))
from adapter import LLMPlayerRuntime
from GM_Agent_Society.agent_runtime import ScriptedPlayerRuntime
from GM_Agent_Society.engine_v1 import EngineV1Error

QUESTION = {'question_id': 'q', 'asker_character': 0, 'successes': 0,
            'response_options': {'definite': [], 'ambiguous': [], 'indirect': []}}

class Role:
    def __init__(self, codes):
        self.codes = iter(codes)
        self.responses = 0
    def call(self, route, **kwargs):
        if route == 'questions/inbox':
            return {'questions': [QUESTION]}
        self.responses += 1
        code = next(self.codes)
        raise EngineV1Error(409, {'code': code, 'error': 'fixture'})

class TimeoutTest(unittest.TestCase):
    def test_closed_question_does_not_stop_either_runtime(self):
        for runtime_type in (LLMPlayerRuntime, ScriptedPlayerRuntime):
            for codes in (['expired'], ['invalid_input', 'expired']):
                with self.subTest(runtime=runtime_type.__name__, codes=codes):
                    runtime = object.__new__(runtime_type)
                    runtime.role = Role(codes)
                    runtime.fallbacks = 0
                    runtime.agent = SimpleNamespace(answer_question=lambda q: [])
                    runtime.policy = SimpleNamespace(respond=lambda q: {'statements': []})
                    runtime._answer()
                    self.assertEqual(runtime.role.responses, len(codes))
    def test_authentication_errors_are_not_hidden(self):
        runtime = object.__new__(LLMPlayerRuntime)
        runtime.role = Role(['unauthorized'])
        runtime.agent = SimpleNamespace(answer_question=lambda q: [])
        runtime.fallbacks = 0
        with self.assertRaises(EngineV1Error):
            runtime._answer()

if __name__ == '__main__':
    unittest.main()
