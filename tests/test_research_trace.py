import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import research_trace as t
from GM_Agent_Society.role_policy import player_count, REQUESTED_PLAYERS

class TraceTests(unittest.TestCase):
    def test_explicit_count_overrides_initial_coverage(self):
        token=REQUESTED_PLAYERS.set(4)
        try:self.assertEqual(player_count([str(i) for i in range(12)],{}),4)
        finally:REQUESTED_PLAYERS.reset(token)
        self.assertEqual(player_count([str(i) for i in range(12)],{}),12)
    def test_public_does_not_embed_private_details(self):
        with tempfile.TemporaryDirectory() as d:
            t.export_views({'events':[{'type':'game_finished','payload':{'reason':'deadline','feedback':'PRIVATE_SENTINEL'}},{'type':'action_completed','visibility':'private','payload':{'result':{'acquired_information':[{'text':'PRIVATE_SENTINEL'}]}}}], 'llm_records':[{'request':{'api_key':'SECRET'},'text':'THOUGHT_SENTINEL'}]},d)
            public=(Path(d)/'public-log.html').read_text()
            self.assertNotIn('PRIVATE_SENTINEL',public)
            self.assertNotIn('THOUGHT_SENTINEL',public)
            detail=(Path(d)/'detail-log.html').read_text()
            self.assertNotIn('SECRET',detail)
            self.assertIn('THOUGHT_SENTINEL',detail)
    def test_error_and_correlation_are_recorded(self):
        class Client:
            credentials={}
            def call(self,route,**data):
                self.data=data
                raise RuntimeError('rejected')
        with tempfile.TemporaryDirectory() as d, patch.object(t,'ROOT',Path(d)):
            context=t.begin({'game_id':'test','self':{'id':'a'}},'action')
            c=Client()
            with self.assertRaises(RuntimeError):t.TraceClient(c).call('llm/invoke',api_key='sk-examplelongsecret')
            self.assertEqual(c.data['correlation']['decision_id'],context['decision_id'])
            lines=(Path(d)/'test/detail.jsonl').read_text()
            self.assertIn('tool_error',lines)
            self.assertNotIn('sk-examplelongsecret',lines)
if __name__=='__main__':unittest.main()
