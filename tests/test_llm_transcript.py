import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from llm_transcript import TranscriptClient, natural_text

class TranscriptTest(unittest.TestCase):
    def test_verbatim_and_readable_outputs(self):
        raw=json.dumps({'action': {'type': 'move'}, 'consideration': '식당을 조사하겠다.\n자료가 부족하다.',
                        'statements': [{'text': '그는 현장에 없었다.'}]}, ensure_ascii=False)
        role=SimpleNamespace(call=lambda route: {'cycle_id': 2, 'phase': 'movement'})
        client=SimpleNamespace(request=lambda *a, **k: (raw, {'output': 4}))
        with tempfile.TemporaryDirectory() as d:
            wrapped=TranscriptClient(client, d, 'player-1', role)
            self.assertEqual(wrapped.request('system', 'user'), (raw, {'output': 4}))
            text=(Path(d)/'llm-output.txt').read_text()
            self.assertIn('식당을 조사하겠다.\n자료가 부족하다.', text)
            self.assertIn('그는 현장에 없었다.', text)
            self.assertIn(raw, (Path(d)/'llm-raw.txt').read_text())
    def test_does_not_invent_narration(self):
        self.assertEqual(natural_text('{"action":{"type":"move"}}'), '')
        self.assertEqual(natural_text('그대로 남길 문장'), '그대로 남길 문장')
