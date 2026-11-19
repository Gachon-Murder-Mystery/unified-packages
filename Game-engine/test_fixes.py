"""Tests for the experiment-30 fixes in competitive.py (FIX-A1..A3).

Reuses CompetitiveTests' fixture (3 players: characters 0 탐정, 1 배우, 2 사업가)
without re-running its tests.
"""
import unittest

from competitive import WRONG_ACCUSE_SCORE
from competitive_rules import REWARDS
from integration import ContractError
import test_competitive as base  # module import: do not re-collect its tests


class FixTests(unittest.TestCase):
    _base = base.CompetitiveTests
    setUp = _base.setUp
    call, pc, state, mutate, picks, action, advance, interaction = (
        _base.call, _base.pc, _base.state, _base.mutate, _base.picks, _base.action, _base.advance, _base.interaction)

    def observe(self, index):
        return self.call('agent/observe', self.pc(self.ids[index]))

    # FIX-A1 ---------------------------------------------------------------
    def test_time_budget_off_by_default_follows_deadline(self):
        budget = self.observe(0)['time_budget']
        self.assertIsNone(budget['time_limit_seconds'])
        self.assertEqual(budget['estimated_remaining_cycles'], self.observe(0)['remaining_cycles'])
        self.assertEqual(budget['binding_limit'], 'deadline_cycles')

    def test_time_budget_reports_the_wall_clock_as_binding(self):
        # 2 cycles finished in 40 s -> 20 s/cycle; 60 s left -> current cycle + 3 more = 4
        self.mutate(lambda g: (g['rules'].update(time_limit_seconds=100), g.update(cycle_id=3)))
        self.now += 40
        budget = self.observe(0)['time_budget']
        self.assertEqual((budget['elapsed_seconds'], budget['average_cycle_seconds'], budget['remaining_seconds']),
                         (40.0, 20.0, 60.0))
        self.assertEqual(budget['estimated_remaining_cycles'], 4)
        self.assertEqual(budget['binding_limit'], 'time_limit_seconds')
        self.assertGreater(self.observe(0)['remaining_cycles'], 100)  # what players were told before

    def test_time_budget_does_not_claim_deadline_cycles_before_a_cycle_finished(self):
        self.mutate(lambda g: g['rules'].update(time_limit_seconds=100))
        budget = self.observe(0)['time_budget']
        self.assertIsNone(budget['estimated_remaining_cycles'])
        self.assertEqual(budget['remaining_seconds'], 100.0)

    # FIX-A2 ---------------------------------------------------------------
    def test_thresholds_include_skills_and_match_the_roll(self):
        self.interaction()  # everyone in room 0, interaction phase
        detective, actor, merchant = (self.observe(i)['thresholds'] for i in range(3))
        self.assertEqual(detective['explore'], 2)                  # observe 5 -> 3, 현장 감식 -1
        self.assertEqual(actor['by_target']['0']['question'], 2)   # speech 5 -> 3, 감정 연기 -1
        self.assertEqual(merchant['by_target']['0']['persuade'], 3)  # speech 4 -> 4, 거래의 달인 -1
        self.assertEqual(merchant['by_target']['0']['question'], 4)
        result, _ = self.action(self.ids[0], 'explore')
        self.assertEqual(result['roll']['threshold'], detective['explore'])

    # FIX-A3 ---------------------------------------------------------------
    def finish_by_timeout(self):
        self.call('games/operator', self.op, game_id='case', operation='TIMEOUT')
        self.call('gm/command', self.gm, command_id='t-announce', operation='ANNOUNCE', ref='time_out', kind='time_out')
        self.call('gm/command', self.gm, command_id='t-finish', operation='FINISH')
        return self.state()['final_feedback']

    @staticmethod
    def base(feedback):
        return feedback['final_score'] - sum(REWARDS[m['tier']] for m in feedback['sub_missions'] if m['achieved'])

    def test_unsolved_score_defaults_to_minus_ten(self):  # FIX-A24 (v1.3 was 0)
        feedback = self.finish_by_timeout().values()
        self.assertEqual({f['final_score'] for f in feedback}, {-10})   # base -10, missions void (FIX-A4)

    # FIX-A4 ---------------------------------------------------------------
    def achieve_first_mission(self, index, corpse=False):
        def f(g):
            p = g['players'][self.ids[index]]
            p['missions'][0].update(achieved=True, achieved_cycle=1)
            if corpse:
                p['mode'] = 'corpse'
        self.mutate(f)
        return REWARDS[self.state()['players'][self.ids[index]]['missions'][0]['tier']]

    def test_unsolved_game_voids_survivor_missions_but_not_the_dead(self):
        self.achieve_first_mission(0)
        reward = self.achieve_first_mission(1, corpse=True)
        feedback = self.finish_by_timeout()
        alive, dead = feedback[self.ids[0]], feedback[self.ids[1]]
        self.assertEqual((alive['final_score'], alive['missions_voided']), (-10, True))   # FIX-A24 base -10
        self.assertEqual((dead['final_score'], dead['missions_voided']), (WRONG_ACCUSE_SCORE + reward, False))
        self.assertEqual(self.observe(2)['scoring']['sub_missions_if_unsolved'], 'void for survivors')

    def test_unsolved_feedback_shows_the_cost_and_the_closest_rival(self):  # FIX-A31
        reward = self.achieve_first_mission(0)
        clue = next(x for x in self.state()['clues'] if x['kind'] == 'definite')
        self.mutate(lambda g: g['characters']['1']['information'].update({clue['id']: {k: v for k, v in clue.items() if k != 'source_room'}}))
        fb = self.finish_by_timeout()
        mine, rival = fb[self.ids[0]], fb[self.ids[1]]
        self.assertEqual((mine['final_score'], mine['score_if_someone_had_won'], mine['missions_voided_points']), (-10, 0, reward))   # FIX-A39: a solved game would have given 0 without missions
        self.assertLess(rival['final_combinations'], mine['final_combinations'])
        self.assertEqual(mine['closest_rival_combinations'], min(fb[a]['final_combinations'] for a in self.ids[1:]))

    def test_void_rule_can_be_turned_off(self):
        self.mutate(lambda g: g['rules'].update(void_missions_if_unsolved=False))
        reward = self.achieve_first_mission(0)
        self.assertEqual(self.finish_by_timeout()[self.ids[0]]['final_score'], -10 + reward)   # FIX-A24 base -10

    def test_unsolved_score_option_applies_to_survivors(self):
        self.mutate(lambda g: g['rules'].update(unsolved_score=WRONG_ACCUSE_SCORE))
        self.assertEqual(self.observe(0)['scoring']['unsolved_survivor'], WRONG_ACCUSE_SCORE)
        self.assertEqual({self.base(f) for f in self.finish_by_timeout().values()}, {WRONG_ACCUSE_SCORE})

    def test_rule_values_are_validated(self):
        for rules in ({'unsolved_score': 5}, {'time_limit_seconds': -1}):
            with self.assertRaises(ContractError) as exc:
                self.call('games/create', self.op, game_id='bad', seed=1, rules=rules)
            self.assertEqual(exc.exception.code, 'invalid_input')


if __name__ == '__main__':
    unittest.main()
