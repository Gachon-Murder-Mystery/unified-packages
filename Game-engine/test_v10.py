"""v10 rules (user decisions 2026-11-12, CHANGES A52-A57).

- A52 in-game relationships come from the agents' own reactions; the engine never moves them by rule
- A53/A54 missions: 20 each, judged-at-end missions, interference missions; the GM relays each success
- A56 ordered trade resolution: answers only after everyone submitted; crossed proposals; several proposals at
  once; the unchosen are told why and take a question or a search; loops get one more choice
- A57 disconnection scenarios: the GM decides about a cut player, the players decide about a cut GM
"""
import unittest
from unittest import mock

from competitive import public_clue
import competitive
import competitive_rules
import test_competitive as base
import test_simultaneous_interaction as simul


class V10Base(unittest.TestCase):
    _base = base.CompetitiveTests
    call, pc, state, mutate, picks, action, advance, error = (
        _base.call, _base.pc, _base.state, _base.mutate, _base.picks, _base.action, _base.advance, _base.error)
    propose, answer, held, view = simul.SimultaneousTests.propose, simul.SimultaneousTests.answer, \
        simul.SimultaneousTests.held, simul.SimultaneousTests.view

    def setUp(self):
        self._base.setUp(self)
        self.mutate(lambda g: g['rules'].update(interaction_order='simultaneous', trade_resolution='ordered',
                                                relationship_source='reactions'))
        for a in self.ids: self.action(a, 'move', {'room': 0})
        self.advance()   # interaction, negotiation stage
        g = self.state()
        defs = [x for x in g['clues'] if x['kind'] == 'definite']
        self.a = next(x for x in defs if x['category'] == 'suspect')
        self.b = next(x for x in defs if x['category'] == 'weapon')
        self.w2 = [x for x in defs if x['category'] == 'weapon'][1]
        self.s2 = [x for x in defs if x['category'] == 'suspect'][1]
        self.mutate(lambda g: (g['characters']['0']['information'].clear(), g['characters']['1']['information'].clear(),
                               g['characters']['2']['information'].clear(),
                               g['characters']['0']['information'].update({self.a['id']: public_clue(self.a)}),
                               g['characters']['1']['information'].update({self.b['id']: public_clue(self.b),
                                                                           self.w2['id']: public_clue(self.w2)}),
                               g['characters']['2']['information'].update({self.s2['id']: public_clue(self.s2)})))

    def inbox(self, i):
        return {t['trade_id']: t for t in self.call('trades/inbox', self.pc(self.ids[i]))['trades']}

    def announce_all(self):
        for item in self.call('gm/observe', self.gm)['pending_announcements']:
            self.call('gm/command', self.gm, command_id='ann:' + item['ref'], operation='ANNOUNCE', ref=item['ref'], kind=item['kind'])


class OrderedTrades(V10Base):
    def test_nobody_answers_before_everyone_submitted(self):
        self.propose('t1')
        self.held(1)
        self.assertFalse(self.inbox(1)['t1']['decidable'])
        self.error('pending', lambda: self.answer('accept', 't1'))
        self.held(2)
        self.assertTrue(self.inbox(1)['t1']['decidable'])
        self.answer('accept', 't1')
        self.assertEqual(self.state()['trades']['t1']['status'], 'settled')

    def test_a_refused_proposer_is_told_why_and_asks_or_searches_instead(self):
        self.propose('t1')
        self.held(1); self.held(2)
        self.answer('refuse', 't1', message='지금은 필요 없어요')
        g = self.state()
        self.assertEqual(g['trades']['t1']['refusal_reason'], 'refused')
        self.assertEqual(g['replacements'][self.ids[0]]['allowed'], ['question', 'explore'])
        self.assertNotIn('gm_notices', self.inbox(0).get('t1', {}))   # not before the GM tells
        self.announce_all()
        notice = self.inbox(0)['t1']['gm_notices'][0]
        self.assertEqual((notice['reason'], notice['their_message']), ('refused', '지금은 필요 없어요'))

    def test_a_recipient_sees_every_proposal_and_the_unchosen_are_told(self):
        self.propose('t0', who=0, target=1)
        self.propose('t2', who=2, target=1, give=[self.s2['id']])
        self.held(1)
        box = self.inbox(1)
        self.assertTrue(box['t0']['decidable'] and box['t2']['decidable'])
        self.answer('accept', 't2', message='이쪽으로 할게요')
        g = self.state()
        self.assertEqual(g['trades']['t2']['status'], 'settled')
        self.assertEqual((g['trades']['t0']['status'], g['trades']['t0']['refusal_reason']), ('refused', 'another_chosen'))
        self.assertIn(self.ids[0], g['replacements'])

    def test_crossed_proposals_that_already_fit_settle_at_once(self):
        self.propose('x0', who=0, target=1)                                       # suspect a for a weapon
        self.propose('x1', who=1, target=0, give=[self.b['id']], offer_types=('weapon',), requested=({'type': 'suspect'},))
        self.held(2)
        g = self.state()
        self.assertEqual({g['trades']['x0']['status'], g['trades']['x1']['status']}, {'settled', 'merged'})
        self.assertIn(self.b['id'], g['characters']['0']['information'])
        self.assertIn(self.a['id'], g['characters']['1']['information'])

    def test_crossed_both_accept_runs_both_and_locks_two_phases(self):
        self.propose('x0', who=0, target=1, requested=({'type': 'weapon', 'exclude': [self.b['candidate']]},))
        self.propose('x1', who=1, target=0, give=[self.b['id']], offer_types=('weapon',), requested=({'type': 'suspect'},))
        self.held(2)
        box0, box1 = self.inbox(0), self.inbox(1)
        self.assertTrue(box0['x1']['decidable'] and box0['x1']['crossed'])
        self.error('invalid_input', lambda: self.answer('counter', 'x1', who=0, give=[self.a['id']]))
        self.answer('accept', 'x1', who=0, give=[self.a['id']])
        self.assertEqual(self.state()['trades']['x1']['status'], 'proposed')      # waits for the partner
        self.answer('accept', 'x0', who=1, give=[self.w2['id']])
        g = self.state()
        self.assertEqual((g['trades']['x0']['status'], g['trades']['x1']['status']), ('settled', 'settled'))
        cycle = g['cycle_id']
        self.assertEqual(g['trade_lock'][self.ids[0]], {'deduction': cycle, 'movement': cycle + 1})
        self.advance()   # -> deduction
        g = self.state()
        self.assertEqual((g['spent'].get(self.ids[0]), g['spent'].get(self.ids[1])), ('locked', 'locked'))

    def test_crossed_both_refuse_gives_both_a_question_or_search(self):
        self.propose('x0', who=0, target=1, requested=({'type': 'weapon', 'exclude': [self.b['candidate']]},))
        self.propose('x1', who=1, target=0, give=[self.b['id']], offer_types=('weapon',), requested=({'type': 'suspect'},))
        self.held(2)
        self.answer('refuse', 'x1', who=0)
        self.answer('refuse', 'x0', who=1)
        g = self.state()
        self.assertEqual({g['trades']['x0']['refusal_reason'], g['trades']['x1']['refusal_reason']}, {'crossed_both_refused'})
        self.assertTrue(self.ids[0] in g['replacements'] and self.ids[1] in g['replacements'])

    def test_a_recipient_whose_action_went_to_another_trade_cannot_take_more(self):
        self.propose('t0', who=0, target=1)
        self.propose('t1', who=1, target=2, give=[self.b['id']], offer_types=('weapon',), requested=({'type': 'suspect'},))
        self.held(2)
        self.assertFalse(self.inbox(1)['t0']['decidable'])                     # 1 waits for its own proposal
        self.answer('accept', 't1', who=2, give=[self.s2['id']])
        g = self.state()
        self.assertEqual((g['trades']['t0']['status'], g['trades']['t0']['refusal_reason']), ('refused', 'recipient_action_used'))
        self.assertIn(self.ids[0], g['replacements'])

    def test_a_full_loop_chooses_once_more_then_fails(self):
        self.propose('l0', who=0, target=1)
        self.propose('l1', who=1, target=2, give=[self.b['id']], offer_types=('weapon',), requested=({'type': 'suspect'},))
        self.propose('l2', who=2, target=0, give=[self.s2['id']], offer_types=('suspect',), requested=({'type': 'suspect'},))
        g = self.state()
        self.assertEqual(sorted(g['loop_rechoice'][str(g['phase_epoch'])]), sorted(self.ids))
        self.assertTrue(self.inbox(1)['l0']['decidable'])
        self.answer('accept', 'l0', who=1, give=[self.b['id']])                # 1's own proposal is withdrawn
        g = self.state()
        self.assertEqual((g['trades']['l0']['status'], g['trades']['l1']['status']), ('settled', 'withdrawn'))


class ReactionRelationships(V10Base):
    def test_rules_do_not_move_relationships_reactions_do(self):
        g = self.state()
        competitive.Rules(self.e).relation(g, 1, 0, 10, 'trade_fulfilled')
        self.assertEqual(g['relationships']['1']['0'], 50.0)
        self.call('relations/react', self.pc(self.ids[1]), reactions=[{'character_id': 0, 'delta': 7}])
        self.assertEqual(self.state()['relationships']['1']['0'], 57.0)
        self.error('invalid_input', lambda: self.call('relations/react', self.pc(self.ids[1]), reactions=[{'character_id': 0, 'delta': 11}]))
        self.error('invalid_input', lambda: self.call('relations/react', self.pc(self.ids[1]), reactions=[{'character_id': 1, 'delta': 1}]))

    def test_an_honest_trade_changes_no_relationship(self):
        self.propose('t1'); self.held(1); self.held(2)
        self.answer('accept', 't1')
        g = self.state()
        self.assertEqual((g['relationships']['0']['1'], g['relationships']['1']['0']), (50.0, 50.0))


class MissionNotices(V10Base):
    def test_the_gm_relays_each_success_privately(self):
        aid = self.ids[0]
        mid = self.state()['players'][aid]['missions'][0]['mission_id']
        with mock.patch.object(competitive, 'mission_complete', lambda g, a, m, final=False: a == aid and m['mission_id'] == mid):
            with self.e.transaction() as db:
                g = self.e.load(db, 'games', 'case', 'id'); competitive.Rules(self.e).award_missions(g); self.e.put(db, 'games', 'case', g, 'id')
        mine = lambda: next(m for m in self.view(0)['sub_missions'] if m['mission_id'] == mid)
        self.assertFalse(mine()['achieved'])                                    # the engine told only the GM
        pending = [x for x in self.call('gm/observe', self.gm)['pending_announcements'] if x['kind'] == 'mission_achieved']
        self.assertEqual(len(pending), 1)
        self.announce_all()
        self.assertTrue(mine()['achieved'])
        self.assertEqual(mine()['name'], competitive.MISSIONS[mid]['name'])

    def test_rewards_are_flat_and_interference_targets_stay_secret(self):
        self.assertEqual(set(competitive.REWARDS.values()), {20})
        self.mutate(lambda g: g['players'][self.ids[0]]['missions'][0].update(mission_id='I1', targets={'player': self.ids[1], 'mission_id': 'M36'}))
        m = self.view(0)['sub_missions'][0]
        self.assertTrue(m['interference'] and m['judged_at_end'])
        self.assertNotIn('mission_id', m['targets'])


class BlockMission(V10Base):
    """A58: I1 counts only when the named count mission got at least halfway and was still not achieved."""
    def judge(self, got, achieved=False):
        holder, target = self.ids[0], self.ids[1]
        def setup(g):
            g['players'][target]['missions'][0].update(mission_id='M04', achieved=achieved)
            g['players'][holder]['missions'][0].update(mission_id='I1', targets={'player': target, 'mission_id': 'M04'})
        self.mutate(setup)
        g = self.state()
        with mock.patch.dict(competitive_rules.COUNTS, {'M04': lambda v: (got, 12)}):
            return competitive_rules.mission_complete(g, holder, g['players'][holder]['missions'][0], final=True)

    def test_below_half_does_not_count(self):
        self.assertFalse(self.judge(5))

    def test_half_or_more_then_failed_counts(self):
        self.assertTrue(self.judge(6))

    def test_achieved_target_does_not_count(self):
        self.assertFalse(self.judge(12, achieved=True))

    def test_not_judged_before_the_end(self):
        holder = self.ids[0]
        self.judge(6)
        g = self.state()
        self.assertFalse(competitive_rules.mission_complete(g, holder, g['players'][holder]['missions'][0]))

    def test_every_count_has_a_need_and_relation_missions_are_not_blockable(self):
        self.assertTrue(set(competitive_rules.COUNTS) <= set(competitive.MISSIONS))
        self.assertFalse({'O4', 'O5', 'M12', 'M14', 'M19', 'M25'} & set(competitive_rules.COUNTS))


class Faults(unittest.TestCase):
    """A57. The base harness drives the game (orchestration=engine); the faults are imposed at a movement phase."""
    _base = base.CompetitiveTests
    call, pc, state, mutate, picks, action, advance, error = (
        _base.call, _base.pc, _base.state, _base.mutate, _base.picks, _base.action, _base.advance, _base.error)

    def setUp(self):
        self._base.setUp(self)

    def set_faults(self, *items):
        self.mutate(lambda g: g['rules'].update(fault_plan=list(items)))

    def cycle(self, deduction=True):
        for a in [x for x, p in self.state()['players'].items() if p['mode'] == 'active' and x not in self.state()['spent']
                  and (self.state().get('links') or {}).get(x) != 'down']:
            self.action(a, 'stay' if self.state()['phase'] == 'movement' else 'pass')
        return self.advance()

    def to_cycle(self, n):
        while self.state()['cycle_id'] < n or self.state()['phase'] != 'movement':
            self.cycle()

    @staticmethod
    def creds_of(g):
        c = g['controller']
        return {'game_id': 'case', 'controller_id': c['controller_id'], 'term': c['term'], 'controller_token': c['token']}

    def session(self, aid):
        with self.e.transaction() as db:
            token = self.e.load(db, 'agents', aid)['pool_token']
        return self.call('pool/session', agent_id=aid, pool_token=token)

    def test_a_cut_player_is_the_gms_call_and_comes_back_to_its_kept_seat(self):
        self.set_faults({'kind': 'player', 'cycle': 2, 'return_cycle': 3, 'slot': 0})
        self.to_cycle(2)
        g = self.state()
        cut = next(a for a, v in g['links'].items() if v == 'down')
        self.assertEqual(self.session(cut)['role'], 'disconnected')
        def keep_others(g):   # only the cut player's lease runs out
            g['controller'].update(lease_until=self.now + 20_000)
            for x, p in g['players'].items():
                if x != cut: p['lease_until'] = self.now + 20_000
        self.mutate(keep_others)
        self.now += 10_000   # its lease runs out: the engine does not turn it into an NPC by itself
        self.call('gm/observe', self.gm)
        self.assertEqual(self.state()['players'][cut]['mode'], 'active')
        self.assertIn(cut, self.call('gm/observe', self.gm)['links']['down'])
        self.call('gm/command', self.gm, command_id='h1', operation='HANDLE_PLAYER', agent_id=cut, action='wait')
        self.assertEqual(self.state()['spent'][cut], 'absent')
        self.to_cycle(3)
        g = self.state()
        self.assertEqual((g['links'][cut], g['players'][cut]['mode']), ('up', 'active'))
        self.assertNotIn(cut, g.get('absent') or {})

    def test_the_gm_may_make_a_cut_player_an_npc_and_give_it_back(self):
        self.set_faults({'kind': 'player', 'cycle': 2, 'return_cycle': 3, 'slot': 1})
        self.to_cycle(2)
        cut = next(a for a, v in self.state()['links'].items() if v == 'down')
        mbti = self.state()['characters'][str(self.state()['players'][cut]['character_id'])]['mbti']
        self.call('gm/command', self.gm, command_id='h1', operation='HANDLE_PLAYER', agent_id=cut, action='npc')
        g = self.state()
        cid = str(g['players'][cut]['character_id'])
        self.assertEqual((g['players'][cut]['mode'], g['characters'][cid]['mode'], g['characters'][cid]['mbti']), ('npc', 'npc', mbti))
        self.to_cycle(3)
        self.assertIn(cut, self.call('gm/observe', self.gm)['links']['reconnected_npcs'])
        self.call('gm/command', self.gm, command_id='h2', operation='HANDLE_PLAYER', agent_id=cut, action='restore')
        g = self.state()
        self.assertEqual((g['players'][cut]['mode'], g['characters'][cid]['controller']), ('active', cut))
        self.call('agent/observe', self.pc(cut))     # the same credential works again
        self.assertEqual(self.session(cut)['role'], 'player')

    def test_a_cut_gm_waits_for_the_players_vote_then_a_waiting_agent_takes_over_and_hands_back(self):
        self.set_faults({'kind': 'gm', 'cycle': 2, 'return_cycle': 3})
        original = self.state()['controller']['controller_id']
        self.to_cycle(2)
        self.assertEqual(self.state()['status'], 'gm_absent')
        self.error('disconnected', lambda: self.call('gm/observe', self.gm))
        self.assertEqual(self.session(original)['role'], 'disconnected')
        view = self.call('agent/observe', self.pc(self.ids[0]))['gm_absent']
        self.assertTrue(view['open'] and view['reserves_left'] > 0)
        for i, a in enumerate(self.ids):
            self.call('gm_absent/choose', self.pc(a), choice='appoint' if i else 'wait')
        g = self.state()
        successor = g['controller']['controller_id']
        self.assertNotIn(successor, self.ids + [original])
        self.assertEqual(g['status'], 'running')
        self.gm = self.creds_of(g)
        self.to_cycle(3)
        g = self.state()
        self.assertEqual(g['controller']['controller_id'], original)
        self.gm = self.creds_of(g)
        with self.e.transaction() as db:
            rec = self.e.load(db, 'agents', successor)
        self.assertEqual((rec['pool_role'], rec['pool_status'], rec['busy_game_id']), ('player', 'available', None))

    def test_no_waiting_agent_left_ends_the_game_and_nothing_counts(self):
        self.set_faults({'kind': 'gm', 'cycle': 2})
        def drain(g):
            pass
        with self.e.transaction() as db:   # every waiting pool agent is already in use
            for a in self.pool['agents']:
                rec = self.e.load(db, 'agents', a['agent_id'])
                if rec['pool_role'] == 'player' and rec['agent_id'] not in self.ids:
                    rec['pool_status'] = 'fault'; self.e.put(db, 'agents', rec['agent_id'], rec)
        self.to_cycle(2)
        g = self.state()
        self.assertEqual((g['ended'], g['resolution']), (True, 'gm_exhausted'))
        with self.e.transaction() as db:
            recs = [self.e.load(db, 'agents', a) for a in self.ids]
        self.assertTrue(all(r.get('cumulative_score', 0) == 0 for r in recs))
        self.assertTrue(all(r['history'][-1]['final_score'] == 0 for r in recs))


if __name__ == '__main__':
    unittest.main()
