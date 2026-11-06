import tempfile
import unittest
from pathlib import Path
from game_safety import TrackedClient, cleanup

class Client:
    def __init__(self): self.calls=[]
    def bind(self, **credentials): return self
    def call(self, route, **data):
        self.calls.append((route,data))
        if route=='games/create': return {'game_id':'owned'}
        if route=='games/observe': return {'ended':False}
        return {}

class SafetyTest(unittest.TestCase):
    def test_cleanup_only_owned_game_and_exports(self):
        raw=Client();client=TrackedClient(raw)
        client.bind(operator_token='fixture').call('games/create')
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cleanup(client,'fixture',d),[])
            self.assertTrue((Path(d)/'runs/owned/full-record.json').is_file())
        finishes=[data for route,data in raw.calls if route=='games/operator']
        self.assertEqual(finishes,[{'game_id':'owned','operation':'FINISH'}])
    def test_no_created_game_no_termination(self):
        raw=Client()
        self.assertEqual(cleanup(TrackedClient(raw),'fixture','unused'),[])
        self.assertEqual(raw.calls,[])
