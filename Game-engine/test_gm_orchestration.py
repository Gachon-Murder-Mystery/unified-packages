"""FIX-A33 (user decision 2026-11-08): the GM orchestrates, the engine referees.

Under orchestration='gm' the engine takes no initiative: a player can act only on the GM's open request
(REQUEST: legal actions and targets), the negotiation stage closes only by RELEASE, timeouts are applied
only when the GM declares them (EXPIRE), and the GM view states what is ready.
"""
import unittest
from unittest import mock

import competitive
from competitive import public_clue
import test_competitive as base


class GMOrchestrationTests(unittest.TestCase):
    _base = base.CompetitiveTests
    call, pc, state, mutate, picks, action, advance, error = (
        _base.call, _base.pc, _base.state, _base.mutate, _base.picks, _base.action, _base.advance, _base.error)

    def setUp(self):
        self._base.setUp(self)
        self.mutate(lambda g: g['rules'].update(interaction_order='simultaneous', orchestration='gm'))

    def gm_view(self):
        return self.call('gm/observe', self.gm)

    def request(self, ids=None):
        v = self.gm_view()
        return self.call('gm/command', self.gm, command_id=f"req:{v['phase_epoch']}:{self.now}:{len(ids or [])}:{id(ids)}",
                         operation='REQUEST', phase_epoch=v['phase_epoch'], agent_ids=ids or v['needs_request'])

    def view(self, i):
        return self.call('agent/observe', self.pc(self.ids[i]))

    def to_interaction(self):
        self.request()
        for a in self.ids: self.action(a, 'move', {'room': 0})
        self.advance()
        self.request()

    def test_no_request_no_action(self):
        self.assertEqual(self.view(0)['allowed_actions'], [])
        self.assertEqual(self.gm_view()['needs_request'], sorted(self.ids))
        r = self.view(0)
        req = {k: r[k] for k in ('game_id', 'cycle_id', 'phase', 'phase_epoch')}
        req.update(agent_id=self.ids[0], action_id='x1', action='move', payload={'room': 1})
        self.error('conflict', lambda: self.call('agent/action', self.pc(self.ids[0]), request=req))

    def test_request_opens_only_the_offered_actions(self):
        self.request([self.ids[0]])
        v = self.view(0)
        self.assertEqual(v['action_request']['issued_by'], 'gm')
        self.assertTrue(v['action_request']['request_id'].startswith('req:'))
        self.assertEqual(set(v['allowed_actions']), set(v['action_request']['options']))
        self.assertEqual(self.view(1)['allowed_actions'], [])            # not asked yet
        self.assertEqual(self.gm_view()['needs_request'], sorted(self.ids[1:]))

    def test_question_target_must_be_offered(self):
        self.to_interaction()
        here = [x['id'] for x in self.view(0)['action_request']['options']['question']['targets']]
        far = next(i for i in range(12) if i not in here and i != 0)
        self.error('invalid_input', lambda: self.action(self.ids[0], 'question', {
            'target_id': far, 'approach': 'question', 'hypothesis': {'suspect': 0, 'weapon': 0, 'place': 0}}))

    def test_negotiation_closes_only_by_release(self):
        self.to_interaction()
        for a in self.ids: self.action(a, 'prepare')
        g = self.gm_view()
        self.assertEqual((g['stage'], g['ready_to_release'], g['ready_to_advance']), ('negotiation', True, False))
        self.error('pending', self.advance)                              # the engine never moves on by itself
        self.call('gm/command', self.gm, command_id='rel', operation='RELEASE', phase_epoch=g['phase_epoch'])
        g = self.gm_view()
        self.assertEqual((g['stage'], g['ready_to_advance']), ('execution', True))

    def test_timeouts_wait_for_the_gm(self):
        self.to_interaction()
        a = next(x for x in self.state()['clues'] if x['kind'] == 'definite')
        self.mutate(lambda g: g['characters']['0']['information'].update({a['id']: public_clue(a)}))
        b = next(x for x in self.state()['clues'] if x['kind'] == 'definite' and x['category'] != a['category'])
        self.mutate(lambda g: g['characters']['1']['information'].update({b['id']: public_clue(b)}))
        self.call('trades/propose', self.pc(self.ids[0]), trade_id='deal', target_id=1, offer_types=[a['category']],
                  offer_ids=[a['id']], requested=[{'type': b['category']}], message_id='p')
        self.now += 35; self.e.tick()          # past the trade deadline (30 s), inside the player lease (40 s)
        self.assertEqual(self.state()['trades']['deal']['status'], 'proposed')   # no automatic cancel
        g = self.gm_view()
        self.assertEqual(g['overdue'], [{'kind': 'trade', 'ref': 'deal'}])
        self.call('gm/command', self.gm, command_id='exp', operation='EXPIRE', kind='trade', ref='deal')
        self.assertEqual(self.state()['trades']['deal']['status'], 'cancelled')
        self.assertIn(self.ids[0], self.gm_view()['needs_request'])               # owed a replacement request
        self.request([self.ids[0]])
        self.assertEqual(self.view(0)['allowed_actions'], ['explore', 'question', 'prepare', 'pass'])

    def test_engine_mode_still_runs_itself(self):
        self.mutate(lambda g: g['rules'].update(orchestration='engine'))
        self.assertTrue(self.view(0)['allowed_actions'])
        self.error('conflict', lambda: self.call('gm/command', self.gm, command_id='r2', operation='REQUEST',
                                                 phase_epoch=self.gm_view()['phase_epoch'], agent_ids=[self.ids[0]]))


if __name__ == '__main__':
    unittest.main()
