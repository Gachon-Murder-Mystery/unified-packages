"""FIX-A9 (first-come room clues + free remaining count), FIX-A14 (contested searches settle
at phase end, higher chance first), FIX-A13 (placement), FIX-A10 (mission feasibility
before assignment), FIX-A11 (O3/M25 follow their written conditions).

Reuses CompetitiveTests' fixture (3 players in characters 0, 1, 2) without re-running it.
"""
import unittest
from unittest import mock

import competitive
from competitive_rules import MISSIONS, candidates, mission_feasible, mission_complete
import test_competitive as base

SURE = {'dice': [6, 6], 'threshold': 2, 'successes': 2, 'prepared': False}


class RoomClueTests(unittest.TestCase):
    _base = base.CompetitiveTests
    setUp = _base.setUp
    call, pc, state, mutate, picks, action, advance, interaction = (
        _base.call, _base.pc, _base.state, _base.mutate, _base.picks, _base.action, _base.advance, _base.interaction)

    def room0(self):
        return [x['id'] for x in self.state()['clues'] if x['source_room'] == 0]

    def roll_as(self, rolls):
        """Rules.roll replaced by a fixed roll per character."""
        return mock.patch.object(competitive.Rules, 'roll', lambda _s, _g, c, *a, **k: dict(rolls[c['character_id']]))

    def settle(self, rolls):
        """Characters in `rolls` search room 0, the rest pass; the phase ends; their results."""
        with self.roll_as(rolls):
            pending = {i: self.action(self.ids[i], 'explore' if i in rolls else 'pass')[1]['action_id'] for i in range(3)}
        self.assertTrue(all(self.call('agent/result', self.pc(self.ids[i]), action_id=pending[i])['execution_status'] == 'pending'
                            for i in rolls))   # FIX-A14: nothing is decided before the phase ends
        self.advance()
        return {i: self.call('agent/result', self.pc(self.ids[i]), action_id=pending[i]) for i in rolls}

    def leave_one_clue(self):
        self.mutate(lambda g: g.update(room_taken=[x['id'] for x in g['clues'] if x['source_room'] == 0][1:]))

    def test_first_come_clue_is_gone_for_everyone_else(self):
        self.interaction()
        n = len(self.room0())
        r = self.settle({0: SURE, 1: SURE})
        got = [r[i]['acquired_information'][0]['id'] for i in (0, 1)]
        self.assertNotEqual(got[0], got[1])
        self.assertEqual((r[0]['room_remaining'], r[1]['room_remaining']), (n - 2, n - 2))
        self.assertEqual(sorted(self.state()['room_taken']), sorted(got))

    def test_higher_chance_takes_a_contested_clue_first(self):
        self.interaction()
        self.leave_one_clue()
        r = self.settle({0: dict(SURE, threshold=4, successes=1), 1: dict(SURE, threshold=2, successes=1)})
        self.assertEqual((len(r[0]['acquired_information']), len(r[1]['acquired_information'])), (0, 1))
        self.assertEqual((r[0]['room_remaining'], r[1]['room_remaining']), (0, 0))

    def test_preparation_raises_the_chance(self):
        self.interaction()
        self.leave_one_clue()
        r = self.settle({0: dict(SURE, threshold=3, dice=[3, 3, 3], successes=3), 1: dict(SURE, threshold=3, successes=2)})
        self.assertEqual((len(r[0]['acquired_information']), len(r[1]['acquired_information'])), (1, 0))

    def test_equal_chance_goes_to_exactly_one(self):
        self.interaction()
        self.leave_one_clue()
        r = self.settle({0: SURE, 1: SURE})
        self.assertEqual(sorted(len(r[i]['acquired_information']) for i in (0, 1)), [0, 1])

    def test_empty_room_reports_zero_and_gives_nothing(self):
        self.interaction()
        self.mutate(lambda g: g.update(room_taken=[x['id'] for x in g['clues'] if x['source_room'] == 0]))
        r = self.settle({0: SURE})[0]
        self.assertEqual((r['acquired_information'], r['room_remaining']), ([], 0))

    def test_count_is_reported_even_when_the_roll_fails(self):
        self.interaction()
        r = self.settle({0: dict(SURE, successes=0)})[0]
        self.assertEqual(r['room_remaining'], len(self.room0()))

    def test_shared_mode_keeps_v13_copies_and_answers_at_once(self):
        self.mutate(lambda g: g['rules'].update(room_clues='shared'))
        self.interaction()
        n = len(self.room0())
        with self.roll_as({0: SURE, 1: SURE}):
            first = self.action(self.ids[0], 'explore')[0]
            second = self.action(self.ids[1], 'explore')[0]
        self.assertEqual(first['execution_status'], 'completed')
        self.assertEqual(second['room_remaining'], n - 1)   # only its own take counts

    def test_rule_is_validated(self):
        with self.assertRaises(Exception):
            self.call('games/create', self.op, game_id='bad', seed=1, rules={'room_clues': 'copy'})


class PlacementTests(unittest.TestCase):
    """FIX-A13: moves are public only when the movement turn ends, and stay on record."""
    _base = base.CompetitiveTests
    setUp = _base.setUp
    call, pc, state, mutate, picks, action, advance, interaction = (
        _base.call, _base.pc, _base.state, _base.mutate, _base.picks, _base.action, _base.advance, _base.interaction)

    def room_of(self, viewer, character):
        view = self.call('agent/observe', self.pc(self.ids[viewer]))
        return next(x['room'] for x in view['positions'] if x['id'] == character), view

    def test_moves_are_hidden_until_the_phase_ends_then_recorded(self):
        before, _ = self.room_of(1, 0)
        self.action(self.ids[0], 'move', {'room': 5})
        during, view = self.room_of(1, 0)
        self.assertEqual(during, before)                        # not leaked mid-phase
        self.assertEqual(self.call('agent/observe', self.pc(self.ids[0]))['self']['room'], 5)  # own move is known
        for a in self.ids[1:]: self.action(a, 'stay')
        self.advance()
        after, view = self.room_of(1, 0)
        self.assertEqual(after, 5)
        self.assertEqual(view['placements'][-1]['cycle'], 1)
        self.assertEqual(view['placements'][-1]['rooms']['0'], 5)
        self.assertEqual(len(view['placements'][-1]['rooms']), 12)


class ReputationTests(unittest.TestCase):
    """FIX-A16 -> A26 (user decision 2026-11-08): the pool agent behind a character in my room is
    visible, its trade record is not. The engine keeps the record for research only."""
    _base = base.CompetitiveTests
    setUp = _base.setUp
    call, pc, state, mutate, picks, action, advance, interaction, start_trade = (
        _base.call, _base.pc, _base.state, _base.mutate, _base.picks, _base.action, _base.advance,
        _base.interaction, _base.start_trade)

    def seen_by(self, viewer, character):
        view = self.call('agent/observe', self.pc(self.ids[viewer]))
        return next(x for x in view['occupants'] if x['id'] == character)

    def test_occupants_carry_the_agent_not_its_record(self):
        self.interaction()
        seen = self.seen_by(2, 0)
        self.assertEqual(seen['agent_id'], self.ids[0])
        self.assertNotIn('reputation', seen)

    def test_settlement_is_recorded_but_not_shown(self):
        self.start_trade()
        self.call('trades/transfer', self.pc(self.ids[0]), trade_id='deal', message_id='give')
        self.now += 31; self.e.tick()   # partner never sent: settles as fulfilled by 0, broken by 1
        reps = {x['agent_id']: x['negotiation_reputation'] for x in self.call('pool/bootstrap', self.op)['agents']}
        self.assertEqual((reps[self.ids[0]], reps[self.ids[1]]), ({'fulfilled': 1, 'broken': 0}, {'fulfilled': 0, 'broken': 1}))
        self.assertNotIn('reputation', self.seen_by(2, 0))


class FeasibilityTests(unittest.TestCase):
    def test_twelve_players_never_get_npc_missions(self):
        offered = set()
        for seed in range(30):
            for cid in range(12):
                for ids in candidates(seed, f'a{cid}', cid, 'ENTJ', (), 12, deadline=20).values():
                    offered.update(ids)
        self.assertFalse(offered & {'M13', 'M15', 'M35', 'M47'})
        self.assertTrue(offered & {'M36', 'M37', 'M38', 'M39'})   # trades are live

    def test_cycle_bounds_cover_counted_actions(self):
        self.assertFalse(mission_feasible('M24', 40, 12)[0])        # FIX-A53: retired in v10
        self.assertFalse(mission_feasible('O2', 60, 12)[0])         # FIX-A53: retired in v10
        self.assertFalse(mission_feasible('M45', 14, 12)[0])        # 15 cycles in the safe room
        self.assertTrue(mission_feasible('M45', 15, 12)[0])
        self.assertFalse(mission_feasible('M09', 29, 12)[0])        # the last 8 of 30 cycles

    def test_population_bounds(self):
        self.assertFalse(mission_feasible('M43', 20, 4)[0])         # FIX-A53: needs 4 other players
        self.assertTrue(mission_feasible('M43', 20, 5)[0])
        self.assertFalse(mission_feasible('M13', 20, 9)[0])         # FIX-A53: retired in v10

    def test_every_mission_has_a_bound(self):
        self.assertEqual(set(MISSIONS), set(__import__('competitive_rules').MIN_CYCLES))


def mini_game(rooms_by_cycle, start_room=7, searches=()):
    """Smallest state mission_complete() reads, for character 8 (기술자) alone in the game."""
    cycles = [{'cycle': i + 1, 'rooms': {str(c): (r if c == 8 else 0) for c in range(12)}, 'alive': ['8']}
              for i, r in enumerate(rooms_by_cycle)]
    activity = [{'actor': 8, 'kind': 'explore', 'room': room, 'successes': 1, 'cycle': cyc, 'target': None,
                 'approach': 'explore', 'prepared': False} for cyc, room in searches]
    chars = {str(c): {'information': {}, 'mode': 'active' if c == 8 else 'npc'} for c in range(12)}
    return {'players': {'a': {'character_id': 8, 'visited': [start_room], 'acquired': []}},
            'characters': chars, 'activity': activity, 'cycle_history': cycles, 'cycle_id': len(cycles),
            'trades': {}, 'relationships': {str(i): {str(j): 50 for j in range(12) if j != i} for i in range(12)},
            'disclosures': []}


class MissionTextTests(unittest.TestCase):
    def test_o3_is_retired_and_m50_counts_total_stay(self):   # FIX-A53
        self.assertFalse(mission_feasible('O3', 30, 12)[0])
        self.assertTrue(mission_complete(mini_game([7, 1] * 6 + [7] * 6), 'a', {'mission_id': 'M50'}))
        self.assertFalse(mission_complete(mini_game([7, 1] * 6 + [7] * 5), 'a', {'mission_id': 'M50'}))

    def test_m25_any_twenty_consecutive_moves(self):
        rooms = [7] * 3 + [i % 2 for i in range(25)]   # FIX-A53: waits 3 cycles, then moves every cycle for 25
        self.assertTrue(mission_complete(mini_game(rooms), 'a', {'mission_id': 'M25'}))
        self.assertFalse(mission_complete(mini_game(rooms[:-1]), 'a', {'mission_id': 'M25'}))


if __name__ == '__main__':
    unittest.main()


class PairStatementTests(unittest.TestCase):
    """FIX-A18: ambiguous statement = pair exclusion (anchor from my definite clue, partner from
    the asker's hypothesis in another category)."""

    def options(self, held, hypothesis):
        rules = object.__new__(competitive.Rules)
        info = {f'd{i}': {'id': f'd{i}', 'kind': 'definite', 'category': c, 'candidate': v} for i, (c, v) in enumerate(held)}
        g = {'characters': {'1': {'information': info}}}
        return rules, g, {'target_id': 1, 'hypothesis': hypothesis, 'successes': 1}

    def test_anchor_outside_and_inside_the_hypothesis(self):
        rules, g, q = self.options([('suspect', 3), ('weapon', 2)], {'suspect': 5, 'weapon': 2, 'place': 6})
        pairs = [x['pair'] for x in rules.options(g, q)['ambiguous']]
        self.assertIn({'suspect': 3, 'weapon': 2}, pairs)          # anchor 3 (outside), partner weapon 2
        self.assertIn({'place': 6, 'suspect': 3}, pairs)
        self.assertIn({'suspect': 5, 'weapon': 2}, pairs)          # anchor weapon 2 (inside), partner suspect 5
        self.assertIn({'place': 6, 'weapon': 2}, pairs)
        self.assertTrue(all(len(p) == 2 for p in pairs))            # never two candidates of one category
        self.assertEqual(len(pairs), len({tuple(sorted(p.items())) for p in pairs}))

    def test_no_definite_clue_no_pair(self):
        rules, g, q = self.options([], {'suspect': 5, 'weapon': 2, 'place': 6})
        self.assertEqual(rules.options(g, q)['ambiguous'], [])

    def test_validation_accepts_pairs_only(self):
        rules, g, q = self.options([('suspect', 3)], {'suspect': 5, 'weapon': 2, 'place': 6})
        ok = rules.validate_response(g, q, [{'kind': 'ambiguous', 'pair': {'weapon': 2, 'suspect': 3}}])
        self.assertEqual(ok[0]['pair'], {'suspect': 3, 'weapon': 2})
        with self.assertRaises(Exception):
            rules.validate_response(g, q, [{'kind': 'ambiguous', 'category': 'weapon', 'candidate': 2}])
        with self.assertRaises(Exception):   # a pair the clue does not entail
            rules.validate_response(g, q, [{'kind': 'ambiguous', 'pair': {'suspect': 5, 'weapon': 2}}])


class BoostedFlowTests(unittest.TestCase):
    """FIX-A19: clue_flow = boosted."""
    _base = base.CompetitiveTests
    setUp = _base.setUp
    call, pc, state, mutate, picks, action, advance, interaction = (
        _base.call, _base.pc, _base.state, _base.mutate, _base.picks, _base.action, _base.advance, _base.interaction)
    explore_settle = RoomClueTests.settle
    roll_as = RoomClueTests.roll_as

    def rules(self):
        return object.__new__(competitive.Rules)

    def test_start_clues_one_definite_two_indirect_no_overlap(self):
        g = self.state()
        self.rules().deal_start_clues(g)
        dealt = [g['players'][a]['start_clues'] for a in self.ids]
        kinds = {x['id']: x['kind'] for x in g['clues']}
        self.assertTrue(all(sorted(kinds[i] for i in d) == ['definite', 'indirect', 'indirect'] for d in dealt))
        flat = [i for d in dealt for i in d]
        self.assertEqual(len(flat), len(set(flat)))
        self.assertTrue(all(set(d) <= set(g['characters'][str(n)]['information']) for n, d in enumerate(dealt)))

    def test_start_clues_do_not_count_as_acquired(self):
        g = self.state()
        self.rules().deal_start_clues(g)
        self.assertFalse(mission_complete(g, self.ids[0], {'mission_id': 'M51', 'params': {'N': 1}, 'targets': {}}))

    def test_detective_finds_one_more_and_duplicates_are_wasted(self):
        self.mutate(lambda g: g['rules'].update(clue_flow='boosted'))
        self.interaction()
        room = [x for x in self.state()['clues'] if x['source_room'] == 0]
        held = public_clue_of(room[0])
        # player 0 (탐정) already holds one room-0 clue; only it and one more remain in the room
        self.mutate(lambda g: (g['characters']['0']['information'].update({held['id']: held}),
                               g.update(room_taken=[x['id'] for x in room[2:]])))
        r = self.explore_settle({0: dict(SURE, successes=1)})[0]   # 1 success + skill = 2 clues
        self.assertEqual((len(r['acquired_information']) + r['duplicates'], r['room_remaining']), (2, 0))
        self.assertEqual(len(r['acquired_information']), 1)       # the one it already held is a wasted find

    def test_extra_vague_slots(self):
        rules = self.rules()
        g = {'rules': {'clue_flow': 'boosted', 'deadline_cycles': 20}, 'cycle_id': 5}
        hold = lambda n: {'information': {f'c{i}': {'id': f'c{i}', 'kind': 'definite'} for i in range(n)}}
        self.assertEqual([rules.extra_vague(g, hold(n)) for n in (4, 5, 9, 14, 15)], [0, 1, 1, 1, 2])
        g['cycle_id'] = 11
        self.assertEqual(rules.extra_vague(g, hold(4)), 1)       # after half the deadline

    def test_validation_requires_the_extra_slots(self):
        rules = object.__new__(competitive.Rules)
        info = {'d0': {'id': 'd0', 'kind': 'definite', 'category': 'suspect', 'candidate': 3}}
        g = {'characters': {'1': {'information': info}}}
        q = {'target_id': 1, 'hypothesis': {'suspect': 5, 'weapon': 2, 'place': 6}, 'successes': 1, 'extra_vague': 1}
        one = [{'kind': 'ambiguous', 'pair': {'suspect': 3, 'weapon': 2}}]
        with self.assertRaises(Exception):
            rules.validate_response(g, q, one)
        rules.validate_response(g, q, one + [{'kind': 'ambiguous', 'pair': {'place': 6, 'suspect': 3}}])


def public_clue_of(clue):
    return {k: v for k, v in clue.items() if k != 'source_room'}


class NewMissionTests(unittest.TestCase):
    """FIX-A21: M52-M56 exist, are feasible only where their mechanics exist, and judge settled trades."""

    def test_bundle_missions_need_simultaneous(self):
        self.assertFalse(mission_feasible('M52', 20, 12, rules={'interaction_order': 'immediate'})[0])
        self.assertTrue(mission_feasible('M52', 20, 12, rules={'interaction_order': 'simultaneous'})[0])
        self.assertTrue(mission_feasible('M55', 20, 12)[0])
        self.assertFalse(mission_feasible('M56', 3, 12, rules={'interaction_order': 'simultaneous'})[0])

    def test_trade_predicates(self):
        def trade(mine, theirs, i_gave=True, they_gave=True, withheld=False):
            return {'proposer': 'a', 'recipient': 'b', 'accepted': True, 'status': 'settled',
                    'offers': {'a': ['x'] * mine, 'b': ['y'] * theirs}, 'fulfilled': {'a': i_gave, 'b': they_gave},
                    'withheld': {'a': withheld, 'b': False}, 'first_sender': 'b', 'cycle': 1}
        g = mini_game([7])
        g['players'] = {'a': {'character_id': 8, 'visited': [7], 'acquired': []}}
        check = lambda mid: mission_complete(g, 'a', {'mission_id': mid})
        g['trades'] = {'1': trade(1, 3)}                      # FIX-A53 v10 quantities
        self.assertFalse(check('M52'))
        g['trades']['2'] = trade(1, 3)
        self.assertTrue(check('M52'))
        self.assertFalse(check('M53'))
        g['trades']['3'] = trade(1, 3)
        self.assertTrue(check('M53'))
        g['trades'] = {'1': trade(2, 1), '2': trade(3, 1)}
        self.assertFalse(check('M54'))
        g['trades']['3'] = trade(2, 1)
        self.assertTrue(check('M54'))
        g['trades'] = {str(i): trade(1, 1) for i in range(8)}
        self.assertTrue(check('M56'))
        g['trades']['8'] = trade(1, 1, i_gave=False, withheld=True)
        self.assertFalse(check('M56'))
        g['players']['a']['multi_finds'] = 2
        self.assertFalse(check('M55'))
        g['players']['a']['multi_finds'] = 3
        self.assertTrue(check('M55'))
