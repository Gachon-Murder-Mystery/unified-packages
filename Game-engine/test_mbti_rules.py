"""FIX-A28 (agent-to-agent relationship carries over) and FIX-A29 (MBTI strengths and costs in play)."""
import unittest
from unittest import mock

import competitive
from competitive import public_clue, MBTI_RULES
import test_competitive as base


class MbtiRuleTests(unittest.TestCase):
    _base = base.CompetitiveTests
    call, pc, state, mutate, picks, action, advance, error = (
        _base.call, _base.pc, _base.state, _base.mutate, _base.picks, _base.action, _base.advance, _base.error)

    def setUp(self):
        self._base.setUp(self)
        # FIX-A37: these test the experimental A29 switch together with the v1.3 E/I multiplier (both opt-in now)
        self.mutate(lambda g: g['rules'].update(interaction_order='simultaneous', mbti_rules=True, ei_relationship_multiplier=True))
        for a in self.ids: self.action(a, 'move', {'room': 0})
        self.advance()

    def set_mbti(self, cid, mbti):
        self.mutate(lambda g: g['characters'][str(cid)].update(mbti=mbti))

    def threshold(self, cid, approach, target=1, relation=None):
        g = self.state()
        if relation is not None:
            g['relationships'][str(cid)][str(target)] = relation
        rules = object.__new__(competitive.Rules)
        return rules.threshold(g, g['characters'][str(cid)], approach, g['characters'][str(target)] if approach != 'explore' else None)

    def base_threshold(self, cid, approach, target=1, relation=50):
        g = self.state()
        g['rules']['mbti_rules'] = False
        g['relationships'][str(cid)][str(target)] = relation
        rules = object.__new__(competitive.Rules)
        return rules.threshold(g, g['characters'][str(cid)], approach, g['characters'][str(target)] if approach != 'explore' else None)

    def view(self, i):
        return self.call('agent/observe', self.pc(self.ids[i]))

    # -- thresholds --------------------------------------------------------------
    def test_s_searches_easier_and_asks_harder(self):
        self.set_mbti(0, 'ISTJ')
        self.assertEqual(self.threshold(0, 'explore'), max(2, self.base_threshold(0, 'explore') + MBTI_RULES['S_explore']))
        self.assertEqual(self.threshold(0, 'question'), min(6, self.base_threshold(0, 'question') + MBTI_RULES['S_question_family']))

    def test_t_ignores_relationships_and_persuades_easier(self):
        self.set_mbti(0, 'INTJ')
        self.assertEqual(self.threshold(0, 'question', relation=90), self.threshold(0, 'question', relation=50))
        self.assertEqual(self.threshold(0, 'persuade', relation=50),
                         max(2, min(6, self.base_threshold(0, 'persuade') + MBTI_RULES['T_persuade'])))

    def test_f_relationship_modifier_is_doubled(self):
        self.set_mbti(0, 'ENFJ')
        neutral = self.threshold(0, 'question', relation=50)
        self.assertEqual(self.threshold(0, 'question', relation=80), max(2, neutral + MBTI_RULES['F_good']))
        self.assertEqual(self.threshold(0, 'question', relation=20), min(6, neutral + MBTI_RULES['F_bad']))
        self.assertEqual(self.threshold(0, 'interrogate'), min(6, self.base_threshold(0, 'interrogate') + MBTI_RULES['F_pressure']))

    def test_switch_off_restores_v13(self):
        self.set_mbti(0, 'ISTJ')
        self.mutate(lambda g: g['rules'].update(mbti_rules=False))
        self.assertEqual(self.threshold(0, 'explore'), self.base_threshold(0, 'explore'))
        self.assertEqual(self.view(0)['mbti_rules'], {})

    # -- E/I -------------------------------------------------------------------
    def ask(self, asker=0, target=1, successes=2):
        with mock.patch.object(competitive.Rules, 'roll', lambda *a, **k: {'dice': [6, 6], 'threshold': 2, 'successes': successes, 'prepared': False}):
            self.action(self.ids[asker], 'question', {'target_id': target, 'approach': 'question',
                                                      'hypothesis': {'suspect': 0, 'weapon': 0, 'place': 0}})
            for i in range(3):
                if i != asker: self.action(self.ids[i], 'prepare')

    def test_e_builds_relationships_fast_and_is_seen(self):
        self.mutate(lambda g: g['rules'].update(ei_relationship_multiplier=True))   # FIX-A37: v1.3 multiplier is opt-in
        self.set_mbti(0, 'ESTJ'); self.set_mbti(1, 'ISTJ')        # target I: owner multiplier 0.5
        self.ask()
        self.assertEqual(self.state()['relationships']['1']['0'], 50 + MBTI_RULES['question_gain']['E'] * 0.5)
        self.assertEqual(len(self.view(2)['question_pairs']), 1)  # a bystander sees an E's question

    def test_relationship_change_does_not_depend_on_e_or_i_by_default(self):
        """FIX-A37: a personality is not a game stat."""
        self.mutate(lambda g: g['rules'].update(ei_relationship_multiplier=False))   # the default (setUp opts in)
        self.set_mbti(0, 'ESTJ'); self.set_mbti(1, 'ISTJ')
        self.ask()
        self.assertEqual(self.state()['relationships']['1']['0'], 50 + MBTI_RULES['question_gain']['E'])

    def test_i_builds_slowly_and_is_not_always_seen(self):
        self.mutate(lambda g: g['rules'].update(ei_relationship_multiplier=True))   # FIX-A37: v1.3 multiplier is opt-in
        self.set_mbti(0, 'ISTJ'); self.set_mbti(1, 'ESTJ')        # target E: owner multiplier 1.5
        with mock.patch.dict(MBTI_RULES, {'I_published': 0.0}):  # this draw: not published
            self.ask()
        self.assertEqual(self.state()['relationships']['1']['0'], 50 + MBTI_RULES['question_gain']['I'] * 1.5)
        self.assertEqual(self.view(2)['question_pairs'], [])
        self.assertEqual(len(self.view(1)['question_pairs']), 1)  # the two sides know
        self.assertEqual(self.call('observer/snapshot', game_id='case', observer_token=self.state()['observer_token'])['question_pairs'], [])

    # -- N ----------------------------------------------------------------------
    def test_n_reads_indirect_answers_as_originals(self):
        clue = next(x for x in self.state()['clues'] if x['kind'] == 'indirect')
        self.mutate(lambda g: g['characters']['1']['information'].update({clue['id']: public_clue(clue)}))
        self.set_mbti(0, 'INTP')
        with mock.patch.dict(MBTI_RULES, {'N_original': 1.0}):    # this draw: the original
            self.ask(successes=1)
            q = self.call('questions/inbox', self.pc(self.ids[1]))['questions'][0]
            option = q['response_options']['indirect'][0]
            self.call('questions/respond', self.pc(self.ids[1]), question_id=q['question_id'], response_id='r',
                      statements=[{'kind': 'indirect', 'clue_id': option['clue_id'], 'text': option['narrative_options'][-1]}])
        got = self.state()['characters']['0']['information'][clue['id']]
        self.assertEqual(got['kind'], 'indirect')                  # the original, usable by belief

    def test_n_scoring(self):
        self.set_mbti(0, 'ENTP'); self.set_mbti(1, 'ESTP')
        s0, s1 = self.view(0)['scoring'], self.view(1)['scoring']
        self.assertEqual((s0['wrong_accusation'], s0['win_by_rank'][0]), (MBTI_RULES['N_wrong'], 100 + MBTI_RULES['N_win_bonus']))
        self.assertEqual((s1['wrong_accusation'], s1['win_by_rank'][0]), (-20, 100))

    # -- J/P trades ----------------------------------------------------------------
    def trade_setup(self):
        g = self.state()
        self.a = next(x for x in g['clues'] if x['kind'] == 'definite' and x['category'] == 'suspect')
        self.b = next(x for x in g['clues'] if x['kind'] == 'definite' and x['category'] == 'weapon')
        self.mutate(lambda g: (g['characters']['0']['information'].update({self.a['id']: public_clue(self.a)}),
                               g['characters']['1']['information'].update({self.b['id']: public_clue(self.b)})))
        self.call('trades/propose', self.pc(self.ids[0]), trade_id='deal', target_id=1, offer_types=['suspect'],
                  offer_ids=[self.a['id']], requested=[{'type': 'weapon'}], message_id='p')

    def test_j_may_counter_and_gains_more_from_honest_trades(self):  # a tilt, never a prohibition
        self.set_mbti(0, 'ISTJ'); self.set_mbti(1, 'ISFJ')      # both J, both I owners (x0.5)
        self.trade_setup()
        self.call('trades/respond', self.pc(self.ids[1]), trade_id='deal', decision='counter', offer_ids=[self.b['id']],
                  offer_types=['weapon'], requested=[{'type': 'suspect'}], message_id='c')
        self.call('trades/respond', self.pc(self.ids[0]), trade_id='deal', decision='accept', offer_ids=[self.a['id']], message_id='a')
        g = self.state()
        self.assertEqual((g['relationships']['1']['0'], g['relationships']['0']['1']), (50 + MBTI_RULES['trade_gain']['J'] * 0.5,) * 2)

    def test_p_counters_for_free_until_it_settles(self):
        self.set_mbti(0, 'ISTJ'); self.set_mbti(1, 'ISTP')
        self.trade_setup()
        self.call('trades/respond', self.pc(self.ids[1]), trade_id='deal', decision='counter', offer_ids=[self.b['id']],
                  offer_types=['weapon'], requested=[{'type': 'suspect'}], message_id='c')
        self.assertEqual(self.view(1)['remaining_actions'], 1)    # still free to act
        self.call('trades/respond', self.pc(self.ids[0]), trade_id='deal', decision='accept', offer_ids=[self.a['id']], message_id='a')
        self.assertEqual(self.view(1)['remaining_actions'], 0)    # the settled counter took the action
        self.assertEqual(self.state()['relationships']['0']['1'], 50 + MBTI_RULES['trade_gain']['P'] * 0.5)

    def test_j_deception_costs_more(self):
        self.set_mbti(0, 'ISTP'); self.set_mbti(1, 'ISTJ')
        c = next(x for x in self.state()['clues'] if x['kind'] == 'indirect')
        self.trade_setup()
        self.mutate(lambda g: g['characters']['1']['information'].update({c['id']: public_clue(c)}))
        self.call('trades/respond', self.pc(self.ids[1]), trade_id='deal', decision='accept', offer_ids=[c['id']], message_id='a')
        self.assertEqual(self.state()['relationships']['0']['1'], 50 + MBTI_RULES['J_broken'] * 0.5)

    def test_corrections_are_capped(self):
        self.set_mbti(0, 'ISTJ')
        with mock.patch.dict(MBTI_RULES, {'S_question_family': 5}):
            self.assertEqual(self.threshold(0, 'question'), min(6, self.base_threshold(0, 'question') + MBTI_RULES['cap']))

    def test_every_type_stays_within_the_cap_and_has_a_strength_and_a_cost(self):
        rules = object.__new__(competitive.Rules)
        g = self.state()
        letters = [a + b + c + d for a in 'EI' for b in 'SN' for c in 'TF' for d in 'JP']
        for mbti in letters:
            g['characters']['0']['mbti'] = mbti
            for rel in (10, 50, 90):
                g['relationships']['0']['1'] = rel
                for approach in ('explore', 'question', 'persuade', 'interrogate', 'threaten'):
                    target = None if approach == 'explore' else g['characters']['1']
                    g['rules']['mbti_rules'] = False; plain = rules.threshold(g, g['characters']['0'], approach, target)
                    g['rules']['mbti_rules'] = True; tilted = rules.threshold(g, g['characters']['0'], approach, target)
                    self.assertLessEqual(abs(tilted - plain), MBTI_RULES['cap'], (mbti, rel, approach))
            effects = rules.my_mbti_rules(g, g['characters']['0'])
            self.assertEqual(set(effects) - {'cap'}, set(mbti))

    def test_my_type_effects_are_shown(self):
        self.set_mbti(0, 'ENTP')
        rules = self.view(0)['mbti_rules']
        self.assertEqual(set(rules) - {'cap'}, {'E', 'N', 'T', 'P'})


class AgentRelationshipTests(unittest.TestCase):
    """FIX-A28: game relationships restart at 50; the agent-to-agent relationship carries over."""
    _base = base.CompetitiveTests
    setUp = _base.setUp
    call, pc, state, mutate, picks, action, advance, error = (
        _base.call, _base.pc, _base.state, _base.mutate, _base.picks, _base.action, _base.advance, _base.error)

    def test_change_carries_into_the_pool(self):
        self.mutate(lambda g: g['rules'].update(engine_agent_relationships=True))   # FIX-A34: opt-in now
        self.mutate(lambda g: g['relationships']['0'].update({'1': 80.0, '2': 40.0}))
        self.call('games/operator', self.op, game_id='case', operation='FINISH')
        agents = {x['agent_id']: x for x in self.call('pool/bootstrap', self.op)['agents']}
        self.assertEqual(agents[self.ids[0]]['agent_relationships'], {self.ids[1]: 80.0, self.ids[2]: 40.0})

    def test_by_default_the_engine_keeps_and_shows_no_agent_relationship(self):
        """FIX-A34: the out-of-game relationship is each agent's own; the engine stays out of it."""
        self.mutate(lambda g: g['relationships']['0'].update({'1': 80.0, '2': 40.0}))
        for a in self.ids: self.action(a, 'move', {'room': 0})
        self.advance()
        view = self.call('agent/observe', self.pc(self.ids[0]))
        self.assertFalse(any('my_agent_relationship' in x for x in view['occupants']))
        self.call('games/operator', self.op, game_id='case', operation='FINISH')
        agents = {x['agent_id']: x for x in self.call('pool/bootstrap', self.op)['agents']}
        self.assertFalse(agents[self.ids[0]].get('agent_relationships'))

    def test_occupants_show_my_agent_relationship_game_relationships_stay_separate(self):
        self.mutate(lambda g: g['rules'].update(engine_agent_relationships=True))   # FIX-A34: opt-in now
        self.mutate(lambda g: g.setdefault('agent_trust', {}).update({self.ids[0]: {self.ids[1]: 80.0}}))
        for a in self.ids: self.action(a, 'move', {'room': 0})
        self.advance()
        view = self.call('agent/observe', self.pc(self.ids[0]))
        seen = {x['agent_id']: x for x in view['occupants'] if x.get('agent_id')}
        self.assertEqual((seen[self.ids[1]]['my_agent_relationship'], seen[self.ids[2]]['my_agent_relationship']), (80.0, 50.0))
        self.assertEqual(view['relationships']['1'], 50.0)


if __name__ == '__main__':
    unittest.main()
