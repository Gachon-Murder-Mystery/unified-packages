"""FIX-A42 talk rooms (담화), user decisions 2026-11-10.

Intent in the movement phase; rooms with 2+ intending players talk for a fixed number of rounds before the
interaction actions; round 1 every member must speak; from round 2 a member may leave; non-members in the
room see only who is talking. Reuses CompetitiveTests' fixture (3 players), switched to simultaneous order.
"""
import unittest

from integration import ContractError
import test_competitive as base


class TalkRoomTests(unittest.TestCase):
    _base = base.CompetitiveTests
    call, pc, state, mutate, picks, action, advance = (
        _base.call, _base.pc, _base.state, _base.mutate, _base.picks, _base.action, _base.advance)

    def setUp(self):
        self._base.setUp(self)
        self.mutate(lambda g: g['rules'].update(interaction_order='simultaneous'))

    def view(self, i):
        return self.call('agent/observe', self.pc(self.ids[i]))

    def intent(self, i, join=True):
        return self.call('talk/intent', self.pc(self.ids[i]), join=join)

    def say(self, i, text=None, leave=False):
        body = {'leave': leave} | ({'text': text} if text is not None else {})
        return self.call('talk/say', self.pc(self.ids[i]), **body)

    def open_room(self, joiners=(0, 1)):
        for i in joiners: self.intent(i)
        for a in self.ids: self.action(a, 'move', {'room': 0})
        self.advance()

    def test_two_intending_players_in_a_room_talk_before_acting(self):
        self.open_room()
        g = self.state()
        self.assertEqual(g['stage'], 'talk')
        self.assertEqual(self.view(0)['allowed_actions'], ['talk'])
        self.assertTrue(self.view(0)['self']['talk']['must_speak'])
        outsider = self.view(2)['self']['talk']
        self.assertEqual(outsider, {'joined': False, 'talking': [0, 1], 'closed': False})
        self.assertEqual(self.view(2)['allowed_actions'], [])
        with self.assertRaises(ContractError):
            self.action(self.ids[2], 'pass')

    def test_round_one_needs_words_then_members_may_leave(self):
        self.open_room()
        with self.assertRaises(ContractError):
            self.say(0, leave=True)                      # round 1: must speak
        self.say(0, '배우님, 오늘 표정이 밝으시네요.')
        with self.assertRaises(ContractError):
            self.say(0, '한 번 더')                       # one line per round
        self.say(1, '고마워요. 탐정님은 피곤해 보여요.')
        self.assertEqual(self.state()['talk']['round'], 2)   # engine-orchestrated test game: next round at once
        log = self.view(1)['self']['talk']['log']
        self.assertEqual([x['from_character'] for x in log], [0, 1])
        self.assertNotIn('log', self.view(2)['self']['talk'])   # non-members never see the words
        self.say(0, leave=True)                          # round 2: leaving is fine; one member left -> closed
        self.assertEqual(self.state()['stage'], 'negotiation')
        self.assertIn('trade', self.view(2)['allowed_actions'])

    def test_all_rounds_run_then_negotiation(self):
        self.open_room()
        for r in range(3):
            self.say(0, f'탐정 {r}'); self.say(1, f'배우 {r}')
        g = self.state()
        self.assertEqual((g['stage'], g['talk']['round'], g['talk']['closed']), ('negotiation', 3, True))
        self.assertEqual(len(self.view(0)['self']['talk']['log']), 6)

    def test_one_intending_player_opens_no_room(self):
        self.open_room(joiners=(0,))
        self.assertEqual(self.state()['stage'], 'negotiation')
        self.assertIsNone(self.view(0)['self']['talk'])

    def test_intent_only_in_the_movement_phase_and_rooms_off_when_zero(self):
        self.mutate(lambda g: g['rules'].update(talk_rounds=0))
        self.open_room()
        self.assertEqual(self.state()['stage'], 'negotiation')
        with self.assertRaises(ContractError):
            self.intent(0)

    def test_the_gm_lets_a_silent_member_go_after_the_timeout(self):
        self.open_room()
        self.mutate(lambda g: g['rules'].update(orchestration='gm'))   # the GM drives from here (moves needed no request)
        self.say(0, '안녕하세요')
        facts = self.call('gm/observe', self.gm)['talk']
        self.assertEqual((facts['ready'], facts['overdue']), (False, False))
        epoch = self.state()['phase_epoch']
        with self.assertRaises(ContractError):
            self.call('gm/command', self.gm, command_id='t1', operation='TALK_NEXT', phase_epoch=epoch, force=True)
        self.now += self.state()['rules']['question_timeout'] + 1
        self.call('gm/command', self.gm, command_id='t2', operation='TALK_NEXT', phase_epoch=epoch, force=True)
        g = self.state()
        self.assertEqual(g['stage'], 'negotiation')      # the silent member left; one member: room closed
        self.assertEqual(g['talk']['rooms']['0']['left'], [self.ids[1]])
        self.assertEqual(self.call('gm/observe', self.gm)['needs_request'] != [], True)


if __name__ == '__main__':
    unittest.main()
