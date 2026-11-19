import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

class RetryTest(unittest.TestCase):
    def test_truncation_retries_and_never_returns_fragment(self):
        root=Path(__file__).resolve().parents[1]/'Agent_Reasoning'
        saved={k:sys.modules.get(k) for k in ('schema','llm','agent')}
        try:
            for name in ('schema','llm','agent'):
                spec=importlib.util.spec_from_file_location(name,root/(name+'.py'))
                m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m)
            cls=sys.modules['agent'].LLMAgent
            class Cut(Exception):body={'code':'llm_truncated'}
            a=object.__new__(cls);a._log=Mock();a.client=Mock()
            # [FIX-B11] default: the first budget is the reasoning cap, no escalation
            a.client.request.side_effect=Cut()
            with self.assertRaises(sys.modules['llm'].GatewayError):a._request_complete('s','u',4096)
            self.assertEqual([c.kwargs['budget'] for c in a.client.request.call_args_list],[4096])
            # an explicit cap restores the old escalation, never past the cap
            a.client.reset_mock();a.max_budget=16384
            a.client.request.side_effect=[Cut(),Cut(),('COMPLETE',{})]
            self.assertEqual(a._request_complete('s','u',4096)[0],'COMPLETE')
            self.assertEqual([c.kwargs['budget'] for c in a.client.request.call_args_list],[4096,8192,16384])
            a.client.request.side_effect=Cut()
            with self.assertRaises(sys.modules['llm'].GatewayError):a._request_complete('s','u',4096)
        finally:
            for k,v in saved.items():
                if v is None:sys.modules.pop(k,None)
                else:sys.modules[k]=v
