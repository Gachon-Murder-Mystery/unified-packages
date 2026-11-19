"""FIX-A17 + A25-A27: interaction_order = simultaneous (user decisions 2026-11-07/08).

- everyone submits an action or a trade proposal; actions are held until proposals are answered
- proposing and accepting/countering both spend this phase's action (accepting cancels the held action)
- A25 declared-type trades: "I give these TYPES, I want these TYPES (with exclusions)". The engine
  checks each side HOLDS such clues; the clues actually handed over are sealed and may differ.
  Accepting exchanges at once, always. Honest = what was handed over fits the declaration/request.
  Both honest: relationship +10 each way. A deceiver: the deceived side's relationship to it -20
  and a hidden broken mark; the honest side of a deception: no change.
- A26 the engine's trade record and past trades are hidden from players (agents remember)
- A27 free talk on questions, answers and trades
- only a mechanical timeout cancels a trade (replacement for whoever paid) or fails a question
  (replacement explore for the asker)
"""
import unittest

from competitive import public_clue
import test_competitive as base

TRADE_REPLACEMENT = ['explore', 'question', 'prepare', 'pass']


class SimultaneousTests(unittest.TestCase):
    _base = base.CompetitiveTests
    call, pc, state, mutate, picks, action, advance, error = (
        _base.call, _base.pc, _base.state, _base.mutate, _base.picks, _base.action, _base.advance, _base.error)

    def setUp(self):
        self._base.setUp(self)
        self.mutate(lambda g: g['rules'].update(interaction_order='simultaneous'))
        for a in self.ids: self.action(a, 'move', {'room': 0})
        self.advance()   # interaction, negotiation stage
        g = self.state()
        self.a = next(x for x in g['clues'] if x['kind'] == 'definite' and x['category'] == 'suspect')
        self.b = next(x for x in g['clues'] if x['kind'] == 'definite' and x['category'] == 'weapon')
        self.c = next(x for x in g['clues'] if x['kind'] == 'indirect' and x['type'] == 'character_room')
        # each player holds exactly these (start clues removed so the tests control holdings)
        self.mutate(lambda g: (g['characters']['0']['information'].clear(), g['characters']['1']['information'].clear(),
                               g['characters']['2']['information'].clear(),
                               g['characters']['0']['information'].update({self.a['id']: public_clue(self.a)}),
                               g['characters']['1']['information'].update({self.b['id']: public_clue(self.b)})))

    def view(self, i):
        return self.call('agent/observe', self.pc(self.ids[i]))

    def propose(self, tid='deal', give=None, offer_types=('suspect',), requested=({'type': 'weapon'},), who=0, target=1, **extra):
        return self.call('trades/propose', self.pc(self.ids[who]), trade_id=tid, target_id=target,
                         offer_types=list(offer_types), offer_ids=list(give or [self.a['id']]),
                         requested=list(requested), message_id='p:' + tid, **extra)

    def answer(self, decision='accept', tid='deal', who=1, give=None, **extra):
        body = dict(trade_id=tid, decision=decision, message_id=f'r:{tid}:{who}:{decision}', **extra)
        if decision != 'refuse':
            body['offer_ids'] = list(give or [self.b['id']])
        if decision == 'counter':
            body.setdefault('offer_types', ['weapon'])
            body.setdefault('requested', [{'type': 'suspect'}])
        return self.call('trades/respond', self.pc(self.ids[who]), **body)

    def held(self, i, act='prepare'):
        return self.action(self.ids[i], act)

    def result(self, i, request):
        return self.call('agent/result', self.pc(self.ids[i]), action_id=request['action_id'])

    def knows(self, i, clue):
        return clue['id'] in self.state()['characters'][str(i)]['information']

    def hidden_record(self, i):
        return {x['agent_id']: x['negotiation_reputation'] for x in self.call('pool/bootstrap', self.op)['agents']}[self.ids[i]]

    # -- holding --------------------------------------------------------------
    def test_actions_wait_until_everyone_submitted_and_proposals_are_answered(self):
        self.assertIn('trade', self.view(0)['allowed_actions'])
        r0, q0 = self.held(0)
        self.assertEqual(r0['execution_status'], 'held')
        self.held(2)
        self.assertEqual(self.result(0, q0)['execution_status'], 'held')   # player 1 has not submitted
        self.held(1)
        self.assertEqual(self.result(0, q0)['execution_status'], 'completed')
        self.assertEqual(self.state()['stage'], 'execution')

    # -- honest exchange ----------------------------------------------------------
    def test_accepting_exchanges_at_once_spends_actions_and_raises_relationships(self):
        self.propose()
        self.assertEqual(self.view(0)['remaining_actions'], 0)
        _, q1 = self.held(1)                    # player 1 had already submitted an action
        self.answer('accept')
        self.assertEqual(self.result(1, q1)['outcome'], 'replaced_by_trade')
        self.assertTrue(self.knows(1, self.a) and self.knows(0, self.b))
        t = self.state()['trades']['deal']
        self.assertEqual((t['status'], t['fulfilled']), ('settled', {self.ids[0]: True, self.ids[1]: True}))
        g = self.state()
        self.assertTrue(g['relationships']['1']['0'] > 50 and g['relationships']['0']['1'] > 50)
        self.assertEqual((self.hidden_record(0), self.hidden_record(1)), ({'fulfilled': 1, 'broken': 0},) * 2)

    # -- deception ---------------------------------------------------------------
    def test_handing_over_something_else_is_a_deception_only_for_the_deceiver(self):
        self.mutate(lambda g: g['characters']['1']['information'].update({self.c['id']: public_clue(self.c)}))
        self.propose()
        self.answer('accept', give=[self.c['id']])          # holds a weapon clue, hands an indirect one
        self.assertTrue(self.knows(0, self.c) and not self.knows(0, self.b) and self.knows(1, self.a))
        t = self.state()['trades']['deal']
        self.assertEqual(t['withheld'], {self.ids[0]: False, self.ids[1]: True})
        g = self.state()
        self.assertLess(g['relationships']['0']['1'], 50)    # the deceived side distrusts the deceiver
        self.assertEqual(g['relationships']['1']['0'], 50)   # the honest side: no change
        self.assertEqual((self.hidden_record(0), self.hidden_record(1)),
                         ({'fulfilled': 0, 'broken': 0}, {'fulfilled': 0, 'broken': 1}))

    def test_the_proposer_can_deceive_too(self):
        self.mutate(lambda g: g['characters']['0']['information'].update({self.c['id']: public_clue(self.c)}))
        self.propose(give=[self.c['id']])                   # declares suspect, seals an indirect clue
        self.answer('accept')
        self.assertEqual(self.state()['trades']['deal']['withheld'], {self.ids[0]: True, self.ids[1]: False})

    # -- possession checks and exclusions ------------------------------------------
    def test_you_must_hold_what_you_declare(self):
        self.error('unauthorized', lambda: self.propose(offer_types=['place']))

    def test_you_can_only_accept_when_you_hold_what_was_requested(self):
        self.propose(requested=[{'type': 'weapon', 'exclude': [self.b['candidate']]}])   # my only weapon clue is excluded
        self.error('unauthorized', lambda: self.answer('accept'))

    def test_an_excluded_clue_handed_over_is_a_deception(self):
        other = next(x for x in self.state()['clues'] if x['kind'] == 'definite' and x['category'] == 'weapon' and x['id'] != self.b['id'])
        self.mutate(lambda g: g['characters']['1']['information'].update({other['id']: public_clue(other)}))
        self.propose(requested=[{'type': 'weapon', 'exclude': [self.b['candidate']]}])
        self.answer('accept', give=[self.b['id']])          # holds a fitting one, hands the excluded one
        self.assertTrue(self.state()['trades']['deal']['withheld'][self.ids[1]])

    # -- what the parties see --------------------------------------------------------
    def test_the_recipient_sees_types_exclusions_and_talk_never_the_sealed_clues(self):
        self.propose(requested=[{'type': 'weapon', 'exclude': [3, 5]}], message='무기 정보가 필요합니다')
        seen = self.call('trades/inbox', self.pc(self.ids[1]))['trades'][0]
        self.assertEqual(seen['offer_types'], [{'type': 'suspect', 'exclude': []}])
        self.assertEqual(seen['requested'], [{'type': 'weapon', 'exclude': [3, 5]}])
        self.assertEqual(seen['messages'][0]['text'], '무기 정보가 필요합니다')
        self.assertNotIn(self.a['id'], str(seen))
        self.assertNotIn('my_sealed_offer', seen)

    def test_settled_view_never_says_who_was_honest(self):
        self.propose()
        self.answer('accept')
        seen = self.call('trades/inbox', self.pc(self.ids[0]))['trades'][0]
        self.assertEqual((seen['received_ids'], seen['gave_ids']), ([self.b['id']], [self.a['id']]))
        self.assertFalse({'fulfilled', 'withheld', 'sealed', 'honest'} & set(seen))

    def test_inbox_keeps_no_history_after_the_cycle(self):  # FIX-A26
        self.propose()
        self.answer('refuse')
        self.assertEqual(len(self.call('trades/inbox', self.pc(self.ids[1]))['trades']), 1)
        self.mutate(lambda g: g.update(cycle_id=g['cycle_id'] + 1))
        self.assertEqual(self.call('trades/inbox', self.pc(self.ids[1]))['trades'], [])

    def test_inbox_survives_a_corpse_losing_the_offered_clue(self):  # FIX-A23
        self.propose()
        self.answer('refuse')
        self.mutate(lambda g: [c['information'].pop(self.a['id'], None) for c in g['characters'].values()])
        self.assertEqual(len(self.call('trades/inbox', self.pc(self.ids[1]))['trades']), 1)

    def test_occupants_show_the_agent_but_not_the_trade_record(self):  # FIX-A26
        seen = next(x for x in self.view(2)['occupants'] if x['id'] == 0)
        self.assertEqual(seen['agent_id'], self.ids[0])
        self.assertNotIn('reputation', seen)

    # -- GM action request (FIX-A32) -------------------------------------------------
    def test_request_lists_only_legal_actions_and_targets(self):
        view = self.view(0)
        req = view['action_request']
        here = sorted(x['id'] for x in view['occupants'] if x['mode'] != 'corpse')   # players and NPCs in my room
        self.assertEqual((req['issued_by'], req['phase']), ('gm', 'interaction'))
        self.assertTrue({1, 2} <= set(here))
        self.assertEqual(sorted(x['id'] for x in req['options']['question']['targets']), here)
        self.assertEqual(sorted(req['options']['trade']['targets']), here)
        self.assertIn('question', req['options']['question']['targets'][0]['thresholds'])

    def test_alone_means_no_question_or_trade(self):
        def away(g):  # everyone else (players and NPCs) is elsewhere
            for i in range(1, 12):
                g['characters'][str(i)]['room'] = 5
                if g.get('placements'): g['placements'][-1]['rooms'][str(i)] = 5
        self.mutate(away)
        view = self.view(0)
        self.assertNotIn('question', view['allowed_actions'])
        self.assertNotIn('trade', view['allowed_actions'])
        self.assertNotIn('question', view['action_request']['options'])

    # -- costs ------------------------------------------------------------------
    def test_refusal_keeps_the_recipients_action(self):
        _, q1 = self.held(1)
        self.propose()
        self.answer('refuse', message='지금은 어렵습니다')
        self.held(2)
        self.assertEqual(self.result(1, q1)['execution_status'], 'completed')
        self.assertNotEqual(self.result(1, q1)['outcome'], 'replaced_by_trade')

    def test_one_action_one_trade(self):
        self.propose()
        self.mutate(lambda g: g['characters']['2']['information'].update({self.c['id']: public_clue(self.c)}))
        self.propose(tid='two', who=2, target=0, give=[self.c['id']], offer_types=['character_room'],
                     requested=[{'type': 'suspect'}])
        self.error('conflict', lambda: self.answer('accept', tid='two', who=0, give=[self.a['id']]))

    def test_phase_cannot_end_while_a_proposal_is_open(self):
        self.propose()
        self.held(1); self.held(2)
        self.error('pending', self.advance)

    def test_transfer_route_is_gone(self):
        self.propose()
        self.error('conflict', lambda: self.call('trades/transfer', self.pc(self.ids[0]), trade_id='deal', message_id='t'))

    # -- bundles (FIX-A20) -----------------------------------------------------
    def test_unequal_bundle_one_definite_for_three_indirect(self):
        ind = [x for x in self.state()['clues'] if x['kind'] == 'indirect' and x['type'] == 'character_room'][:3]
        self.mutate(lambda g: g['characters']['1']['information'].update({x['id']: public_clue(x) for x in ind}))
        self.propose(tid='big', requested=[{'type': 'character_room'}] * 3)
        self.answer('accept', tid='big', give=[x['id'] for x in ind])
        self.assertTrue(all(self.knows(0, x) for x in ind) and self.knows(1, self.a))

    def test_sealed_clues_must_number_the_items(self):
        self.mutate(lambda g: g['characters']['1']['information'].update({self.c['id']: public_clue(self.c)}))
        self.propose(tid='two', requested=[{'type': 'weapon'}, {'type': 'character_room'}])
        self.error('unauthorized', lambda: self.answer('accept', tid='two', give=[self.b['id']]))

    def test_bundle_size_is_capped_at_three(self):
        self.error('invalid_input', lambda: self.propose(tid='four', requested=[{'type': 'weapon'}] * 4))

    def test_unknown_type_is_refused(self):
        self.error('invalid_input', lambda: self.propose(tid='bad', requested=['indirect']))

    def test_counter_swaps_sides_and_is_answered_by_the_first_proposer(self):
        self.answer_counter = self.propose()
        self.answer('counter', offer_types=['weapon'], requested=[{'type': 'suspect'}], give=[self.b['id']])
        t = self.state()['trades']['deal']
        self.assertEqual((t['proposer'], t['recipient']), (self.ids[1], self.ids[0]))
        self.answer('accept', who=0, give=[self.a['id']])
        self.assertTrue(self.knows(0, self.b) and self.knows(1, self.a))

    # -- proposal limit (FIX-A38, user decision 2026-11-10) -----------------------
    def use_up(self, i, n=None):
        limit = self.state()['rules']['trade_proposals_per_game']
        self.mutate(lambda g: g.setdefault('proposals_used', {}).update({self.ids[i]: limit if n is None else n}))

    def test_proposing_and_countering_each_use_one_proposal(self):
        self.propose()
        self.assertEqual(self.state()['proposals_used'][self.ids[0]], 1)
        self.answer('counter', offer_types=['weapon'], requested=[{'type': 'suspect'}], give=[self.b['id']])
        self.assertEqual(self.state()['proposals_used'][self.ids[1]], 1)
        self.answer('accept', who=0, give=[self.a['id']])          # accepting is free
        self.assertEqual(self.state()['proposals_used'][self.ids[0]], 1)

    def test_no_proposals_left_means_no_proposal_and_no_trade_option(self):
        self.use_up(0)
        me = self.view(0)['self']
        self.assertEqual(me['trade_proposals'], {'per_game': 6, 'left': 0})
        self.assertNotIn('trade', self.view(0)['allowed_actions'])
        self.error('conflict', lambda: self.propose())

    def test_without_proposals_you_can_still_accept_and_refuse_but_not_counter(self):
        self.use_up(1)
        self.propose()
        self.error('conflict', lambda: self.answer('counter', offer_types=['weapon'], requested=[{'type': 'suspect'}],
                                                   give=[self.b['id']]))
        self.answer('accept')
        self.assertTrue(self.knows(0, self.b) and self.knows(1, self.a))

    def test_the_businessman_gets_three_more_and_each_sees_only_its_own_count(self):
        self.mutate(lambda g: g['characters']['0'].update(job='사업가'))
        self.assertEqual(self.view(0)['self']['trade_proposals'], {'per_game': 9, 'left': 9})
        self.assertEqual(self.view(1)['self']['trade_proposals'], {'per_game': 6, 'left': 6})
        self.assertNotIn('trade_proposals', str([x for x in self.view(1).get('occupants') or []]))

    def test_zero_restores_unlimited(self):
        self.mutate(lambda g: g['rules'].update(trade_proposals_per_game=0))
        self.use_up(0, 99)
        self.assertNotIn('trade_proposals', self.view(0)['self'])
        self.propose()

    # -- talk (FIX-A27) ---------------------------------------------------------
    def test_question_and_answer_carry_talk(self):
        self.mutate(lambda g: g['characters']['1']['information'].update({self.c['id']: public_clue(self.c)}))
        from unittest import mock
        import competitive
        with mock.patch.object(competitive.Rules, 'roll', lambda *a, **k: {'dice': [6, 6], 'threshold': 2, 'successes': 2, 'prepared': False}):
            _, q0 = self.action(self.ids[0], 'question', {'target_id': 1, 'approach': 'question', 'message': '3번은 믿을 만한가요?',
                                                          'hypothesis': {'suspect': 0, 'weapon': 0, 'place': 0}})
            self.held(1); self.held(2)
        q = self.call('questions/inbox', self.pc(self.ids[1]))['questions'][0]
        self.assertEqual(q['asker_message'], '3번은 믿을 만한가요?')
        self.call('questions/respond', self.pc(self.ids[1]), question_id=q['question_id'], response_id='r1',
                  statements=[{'kind': 'definite', 'clue_id': self.b['id']}], message='3번은 거래를 어겼어요')
        self.assertEqual(self.result(0, q0)['answer_message'], '3번은 거래를 어겼어요')

    def test_talk_is_capped(self):
        self.error('invalid_input', lambda: self.propose(message='가' * 201))

    # -- mechanical failures --------------------------------------------------
    def test_unanswered_proposal_times_out_and_the_proposer_gets_a_choice(self):
        self.propose()
        self.now += 31; self.e.tick()
        self.assertEqual(self.state()['trades']['deal']['status'], 'cancelled')
        v0, v1 = self.view(0), self.view(1)
        self.assertEqual(v0['allowed_actions'], TRADE_REPLACEMENT)
        self.assertIsNone(v1['replacement'])                  # the recipient spent nothing
        self.assertFalse(self.knows(1, self.a))

    def test_countered_trade_timing_out_returns_both_actions(self):
        self.propose()
        self.answer('counter')                                 # the counter spends player 1's action
        self.now += 31; self.e.tick()
        self.assertEqual(self.view(0)['allowed_actions'], TRADE_REPLACEMENT)
        self.assertEqual(self.view(1)['allowed_actions'], TRADE_REPLACEMENT)

    def test_question_timeout_fails_without_a_stand_in_answer_and_gives_a_search(self):
        from unittest import mock
        import competitive
        with mock.patch.object(competitive.Rules, 'roll', lambda *a, **k: {'dice': [6, 6], 'threshold': 2, 'successes': 2, 'prepared': False}):
            _, q0 = self.action(self.ids[0], 'question', {'target_id': 1, 'approach': 'question',
                                                          'hypothesis': {'suspect': 0, 'weapon': 0, 'place': 0}})
            self.held(1); self.held(2)                        # release: the question is asked now
        self.now += 31; self.e.tick()
        self.assertEqual(self.result(0, q0)['outcome'], 'question_failed')
        self.assertFalse(self.knows(0, self.b))
        self.assertEqual(self.view(0)['allowed_actions'], ['explore'])


if __name__ == '__main__':
    unittest.main()
