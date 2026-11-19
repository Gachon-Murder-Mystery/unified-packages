"""FIX-A39 winner takes all, FIX-A40 cumulative score and post-game sharing (user decisions 2026-11-10).

Reuses CompetitiveTests' fixture (3 players: characters 0 탐정, 1 배우, 2 사업가).
"""
import unittest

from competitive import WRONG_ACCUSE_SCORE
from competitive_rules import REWARDS
from integration import ContractError
import test_competitive as base
import test_fixes as fixes


class WinnerTakesAllTests(unittest.TestCase):
    _base = base.CompetitiveTests
    setUp = _base.setUp
    call, pc, state, mutate, picks, action, advance, interaction = (
        _base.call, _base.pc, _base.state, _base.mutate, _base.picks, _base.action, _base.advance, _base.interaction)
    achieve_first_mission = fixes.FixTests.achieve_first_mission

    def member(self, i):
        return self.call('pool/credentials', self.op, agent_id=self.ids[i])

    def solve(self, winner=0, dead=None):
        """winner accuses the truth; `dead` (if any) accuses wrongly; the rest pass. Then the GM finishes."""
        self.interaction()
        for a in self.ids: self.action(a, 'pass')
        self.advance()
        truth = self.state()['truth']
        for i, a in enumerate(self.ids):
            if i == winner: self.action(a, 'accuse', truth | {'output_tokens': 5})
            elif i == dead: self.action(a, 'accuse', truth | {'suspect': (truth['suspect'] + 1) % 12, 'output_tokens': 5})
            else: self.action(a, 'pass')
        self.advance()
        self.assertEqual(self.state()['resolution'], 'winner')
        self.call('gm/command', self.gm, command_id='finish', operation='FINISH')
        return self.state()['final_feedback']

    def standings(self, i=0):
        return {r['agent_id']: r['cumulative_score'] for r in self.call('pool/standings', self.member(i))['standings']}

    # FIX-A39 --------------------------------------------------------------
    def test_survivors_of_a_solved_game_lose_their_missions_the_dead_keep_theirs(self):
        win_reward = self.achieve_first_mission(0)
        self.achieve_first_mission(1)
        dead_reward = self.achieve_first_mission(2)
        fb = self.solve(winner=0, dead=2)
        self.assertEqual(fb[self.ids[0]]['final_score'], 100 + win_reward)
        self.assertEqual((fb[self.ids[1]]['final_score'], fb[self.ids[1]]['missions_voided']), (0, True))
        self.assertEqual((fb[self.ids[2]]['final_score'], fb[self.ids[2]]['missions_voided']), (WRONG_ACCUSE_SCORE + dead_reward, False))
        self.assertEqual(self.call('agent/observe', self.pc(self.ids[1]))['scoring']['sub_missions_if_someone_wins'], 'void for survivors')

    def test_the_old_rule_can_be_restored(self):
        self.mutate(lambda g: g['rules'].update(void_missions_if_someone_wins=False))
        reward = self.achieve_first_mission(1)
        fb = self.solve(winner=0)
        self.assertEqual((fb[self.ids[1]]['final_score'], fb[self.ids[1]]['missions_voided']), (reward, False))

    # FIX-A40 --------------------------------------------------------------
    def test_scores_accumulate_and_only_the_first_ranked_may_share_once(self):
        fb = self.solve(winner=0)
        score = fb[self.ids[0]]['final_score']
        self.assertEqual(self.state()['sharing'], {self.ids[0]: {'budget': score, 'status': 'open'}})
        self.assertNotIn('standings_published', self.state())          # waits for the winner
        with self.assertRaises(ContractError):
            self.call('pool/share', self.member(1), game_id='case', shares=[{'agent_id': self.ids[1], 'points': 1}])
        with self.assertRaises(ContractError):                         # at most its own score of this game
            self.call('pool/share', self.member(0), game_id='case', shares=[{'agent_id': self.ids[1], 'points': score + 1}])
        self.call('pool/share', self.member(0), game_id='case', shares=[{'agent_id': self.ids[1], 'points': 30}])
        totals = self.standings()
        self.assertEqual((totals[self.ids[0]], totals[self.ids[1]], totals[self.ids[2]]), (score - 30, 30, 0))
        with self.assertRaises(ContractError):
            self.call('pool/share', self.member(0), game_id='case', shares=[])
        published = {r['agent_id']: r['cumulative_score'] for r in self.state()['standings_published']}
        self.assertEqual(published[self.ids[1]], 30)
        notes = self.call('pool/standings', self.member(1))
        self.assertEqual(notes['shares_received'], [{'game_id': 'case', 'from': self.ids[0], 'points': 30}])
        self.assertEqual(self.call('pool/standings', self.member(0))['shares_given'][0]['to'], self.ids[1])

    def test_a_recipient_may_be_a_pool_player_who_sat_out(self):
        self.solve(winner=0)
        outsider = next(r['agent_id'] for r in self.call('pool/standings', self.member(0))['standings'] if r['agent_id'] not in self.ids)
        self.call('pool/share', self.member(0), game_id='case', shares=[{'agent_id': outsider, 'points': 10}])
        self.assertEqual(self.standings()[outsider], 10)

    def test_closing_without_a_share_publishes_the_standings(self):
        fb = self.solve(winner=0)
        closed = self.call('games/sharing-close', self.op, game_id='case')
        self.assertEqual(closed['sharing'][self.ids[0]]['status'], 'none')
        self.assertEqual({r['agent_id']: r['cumulative_score'] for r in closed['standings']}[self.ids[0]], fb[self.ids[0]]['final_score'])

    def test_an_unsolved_game_has_no_sharing_and_publishes_at_once(self):
        fixes.FixTests.finish_by_timeout(self)
        self.assertEqual(self.state()['sharing'], {})
        self.assertEqual({r['cumulative_score'] for r in self.state()['standings_published'] if r['agent_id'] in self.ids}, {-10})

    def test_players_see_the_standings_at_game_start(self):
        rows = self.call('agent/observe', self.pc(self.ids[0]))['self']['standings']
        self.assertEqual(len(rows), len(self.call('pool/standings', self.member(0))['standings']))
        self.assertTrue(all(r['cumulative_score'] == 0 for r in rows))


if __name__ == '__main__':
    unittest.main()
