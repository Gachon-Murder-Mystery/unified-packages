"""FIX-B54: the agent side of the talk rooms (A42): intent, one line per round as this game's character, hearing."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Agent_Reasoning'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapter import LLMPlayerRuntime          # noqa: E402
from test_agent_experience import SayDoTests   # noqa: E402

VIEW = {'joined': True, 'round': 1, 'rounds': 3, 'closed': False, 'members': [0, 3], 'left': [],
        'log': [{'round': 1, 'from_character': 3, 'text': '탐정님, 아까는 고마웠어요.'}], 'my_turn': True, 'must_speak': True}
OBS = {'self': {'character_id': 0, 'name': '탐정', 'job': {'name': '탐정'}, 'mbti': 'INTP', 'talk': VIEW},
       'occupants': [{'id': 3, 'name': '배우', 'job': '배우', 'mode': 'active', 'agent_id': 'society:three3'}]}


class ChatTests(unittest.TestCase):
    def agent(self, replies):
        a = SayDoTests.agent(SayDoTests(), replies)
        a.profile = dict(a.profile or {}, character_id=0)
        return a

    def test_one_line_as_the_character_and_the_others_line_is_heard_once(self):
        a = self.agent([{'consideration': '고마움에 답한다', 'say': '별말씀을요. 배우님 연기만큼 대단하진 않았죠.', 'leave': False}])
        line = a.chat(OBS, VIEW)
        self.assertEqual(line, {'say': '별말씀을요. 배우님 연기만큼 대단하진 않았죠.', 'leave': False})
        self.assertEqual([d['who'] for d in a.new_dealings], [3])        # the line is a dealing to react to
        a.ingest_talk(VIEW)
        self.assertEqual(len(a.new_dealings), 1)                         # heard once

    def test_round_one_without_words_is_asked_again(self):
        a = self.agent([{'say': '', 'leave': True}, {'say': '안녕하세요.', 'leave': False}])
        self.assertEqual(a.chat(OBS, VIEW)['say'], '안녕하세요.')

    def test_leaving_without_words_is_fine_from_round_two(self):
        later = dict(VIEW, round=2, must_speak=False)
        a = self.agent([{'say': '', 'leave': True}])
        self.assertEqual(a.chat(dict(OBS, self=dict(OBS['self'], talk=later)), later), {'say': None, 'leave': True})

    def test_the_movement_output_sets_my_talk_intent(self):
        a = self.agent([{'consideration': '이동', 'action': {'type': 'stay', 'payload': {}}, 'talk': True}])
        a.decide({'phase': 'movement', 'allowed_actions': ['move', 'stay'], 'cycle_id': 2})
        self.assertTrue(a.talk_intent)


class TalkStepTests(unittest.TestCase):
    def test_my_turn_sends_the_line_otherwise_waits(self):
        calls = []
        r = object.__new__(LLMPlayerRuntime)
        r.idle, r.fallbacks = 0, 0
        r.role = SimpleNamespace(call=lambda route, **kw: calls.append((route, kw)) or {})
        r.agent = SimpleNamespace(chat=lambda obs, view: {'say': '좋은 저녁이에요.', 'leave': False})
        self.assertEqual(r._talk({'self': {'talk': VIEW}, 'cycle_id': 2, 'phase': 'interaction'}), 'talk')
        self.assertEqual(calls[-1], ('talk/say', {'text': '좋은 저녁이에요.', 'leave': False}))
        self.assertEqual(r._talk({'self': {'talk': dict(VIEW, my_turn=False)}}), 'waiting')


if __name__ == '__main__':
    unittest.main()
