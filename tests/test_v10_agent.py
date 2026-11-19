"""v10 agent side (CHANGES B61-B66): reactions reach the engine, several proposals are answered at once, the GM's
absence is voted on, and nothing is judged for the agent."""
import json
import unittest

from test_rules_and_trades import OBS, Agent, RuntimeTradeFlow


class Reactions(unittest.TestCase):
    def test_a_reaction_moves_my_affinity_and_is_queued_for_the_game_relationship(self):   # B61
        a = Agent.make()
        a.agent_of_char[5] = 'society:x'
        a._dealing(5, '거래 제안')
        a._apply_reaction([{'who': 5, 'their_attitude_to_me': 1, 'delta': 4, 'why': '먼저 단서를 줬다'}])
        self.assertEqual(a.pending_game_reactions, [{'character_id': 5, 'delta': 4.0}])
        self.assertEqual(a._affinity('society:x')['score'], 54.0)

    def test_the_runtime_sends_queued_reactions_to_the_engine(self):
        rt, calls = RuntimeTradeFlow.runtime(RuntimeTradeFlow(), [])
        rt.agent.pending_game_reactions = [{'character_id': 5, 'delta': 4.0}]
        rt._send_game_reactions()
        self.assertIn(('relations/react', {'reactions': [{'character_id': 5, 'delta': 4.0}]}), calls)
        self.assertEqual(rt.agent.pending_game_reactions, [])


class OrderedAnswers(unittest.TestCase):   # B65 (engine A56)
    def proposal(self, tid, who, decidable=True, crossed=False):
        return dict(RuntimeTradeFlow.trade('proposed'), trade_id=tid, proposer='society:' + who, proposer_character=int(who[-1]),
                    decidable=decidable, crossed=crossed)

    def test_nothing_is_answered_until_the_engine_says_i_can_decide(self):
        rt, calls = RuntimeTradeFlow.runtime(RuntimeTradeFlow(), [self.proposal('t1', 'x5', decidable=False)])
        rt._trades(dict(OBS, phase_epoch=7, self={'id': 'society:me', 'character_id': 0, 'mbti': 'INTP'}))
        self.assertFalse([r for r, _ in calls if r == 'trades/respond'])
        self.assertEqual(rt.trade_activity['refuse_no_action_left'], 0)

    def test_several_proposals_one_choice(self):
        rt, calls = RuntimeTradeFlow.runtime(RuntimeTradeFlow(), [self.proposal('t1', 'x5'), self.proposal('t2', 'x6')])
        rt.agent._request_complete = lambda *_, **__: (json.dumps({'decision': 'accept', 'offer_ids': ['c1'], 'choose_ref': 't2'}), None)
        rt._trades(dict(OBS, phase_epoch=7, self={'id': 'society:me', 'character_id': 0, 'mbti': 'INTP'}))
        respond = [d for r, d in calls if r == 'trades/respond']
        self.assertEqual([(d['trade_id'], d['decision']) for d in respond], [('t2', 'accept')])   # the engine tells t1's proposer

    def test_refusing_refuses_every_proposal(self):
        rt, calls = RuntimeTradeFlow.runtime(RuntimeTradeFlow(), [self.proposal('t1', 'x5'), self.proposal('t2', 'x6')])
        rt.agent._request_complete = lambda *_, **__: (json.dumps({'decision': 'refuse', 'message': '지금은 아니에요'}), None)
        rt._trades(dict(OBS, phase_epoch=7, self={'id': 'society:me', 'character_id': 0, 'mbti': 'INTP'}))
        respond = [d for r, d in calls if r == 'trades/respond']
        self.assertEqual(sorted(d['trade_id'] for d in respond), ['t1', 't2'])
        self.assertTrue(all(d['message'] == '지금은 아니에요' for d in respond))

    def test_a_crossed_proposal_is_never_countered(self):
        rt, calls = RuntimeTradeFlow.runtime(RuntimeTradeFlow(), [self.proposal('t1', 'x5', crossed=True)])
        rt.agent._request_complete = lambda *_, **__: (json.dumps(
            {'decision': 'counter', 'offer_ids': ['c1'], 'offer_types': ['weapon'], 'requested': [{'type': 'place'}]}), None)
        rt._trades(dict(OBS, phase_epoch=7, self={'id': 'society:me', 'character_id': 0, 'mbti': 'INTP',
                                                   'trade_proposals': {'left': 3}}))
        self.assertEqual([d['decision'] for r, d in calls if r == 'trades/respond'], ['refuse'])


class GMAbsence(unittest.TestCase):   # B66 (engine A57)
    def test_the_agent_votes_and_a_failed_model_call_waits(self):
        a = Agent.make([{'consideration': '추리가 거의 끝났다', 'choice': 'appoint', 'message': '새 GM을 부릅시다'}])
        self.assertEqual(a.decide_gm_absent(dict(OBS, gm_absent={'round': 1, 'reserves_left': 4})),
                         {'choice': 'appoint', 'message': '새 GM을 부릅시다'})
        b = Agent.make([{'choice': 'maybe'}])
        self.assertEqual(b.decide_gm_absent(dict(OBS, gm_absent={'round': 1})), {'choice': 'wait'})
        self.assertEqual(b.model_fallbacks['gm_absent'], 1)


if __name__ == '__main__':
    unittest.main()
