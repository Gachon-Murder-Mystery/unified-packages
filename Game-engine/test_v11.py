"""v11 engine (CHANGES A59, A60, user decisions 2026-11-16): the GM may re-send an open request under a new number,
and an expired player lease is a cut link the GM decides about; the link comes back when the agent asks again."""
import unittest

import test_competitive as base
from competitive import Rules
from test_v10 import Faults


class Reissue(unittest.TestCase):   # A59
    _base = base.CompetitiveTests
    call, pc, state, mutate, picks, action, advance, error = (
        _base.call, _base.pc, _base.state, _base.mutate, _base.picks, _base.action, _base.advance, _base.error)

    def setUp(self):
        self._base.setUp(self)
        self.mutate(lambda g: g['rules'].update(orchestration='gm'))
        self.epoch = self.state()['phase_epoch']
        aid = self.ids[0]
        self.aid = aid
        self.first = self.call('gm/command', self.gm, command_id='r1', operation='REQUEST', phase_epoch=self.epoch,
                               agent_ids=[aid])['requested'].get(aid)

    def test_an_open_request_is_not_sent_twice_without_reissue(self):
        self.assertTrue(self.first)
        again = self.call('gm/command', self.gm, command_id='r2', operation='REQUEST', phase_epoch=self.epoch, agent_ids=[self.aid])
        self.assertEqual(again['requested'], {})

    def test_reissue_gives_the_open_request_a_new_number(self):
        new = self.call('gm/command', self.gm, command_id='r2', operation='REQUEST', phase_epoch=self.epoch,
                        agent_ids=[self.aid], reissue=True)['requested'].get(self.aid)
        self.assertTrue(new and new != self.first)
        g = self.state()
        self.assertEqual((g['requests'][self.aid]['request_id'], g['requests'][self.aid]['status']), (new, 'open'))

    def test_no_reissue_after_the_player_acted(self):
        self.action(self.aid, 'stay')
        out = self.call('gm/command', self.gm, command_id='r2', operation='REQUEST', phase_epoch=self.epoch,
                        agent_ids=[self.aid], reissue=True)
        self.assertEqual(out['requested'], {})


class LeaseExpiry(Faults):   # A60
    def expire(self, who):
        def keep_others(g):
            g['controller'].update(lease_until=self.now + 20_000)
            for x, p in g['players'].items():
                if x != who: p['lease_until'] = self.now + 20_000
        self.mutate(keep_others)
        self.now += 10_000
        self.e.tick()   # the engine's periodic sweep is where leases run out

    def test_old_rule_makes_an_npc_at_once(self):
        who = self.ids[0]
        self.expire(who)
        self.assertEqual(self.state()['players'][who]['mode'], 'npc')

    def test_link_rule_marks_the_link_down_for_the_gm(self):
        self.mutate(lambda g: g['rules'].update(lease_expiry='link'))
        who = self.ids[0]
        self.expire(who)
        g = self.state()
        self.assertEqual((g['players'][who]['mode'], g['links'][who]), ('active', 'down'))
        self.assertIn(who, self.call('gm/observe', self.gm)['links']['down'])

    def test_the_agent_asking_again_brings_its_kept_seat_back(self):
        self.mutate(lambda g: g['rules'].update(lease_expiry='link'))
        who = self.ids[0]
        self.expire(who)
        self.call('gm/command', self.gm, command_id='h1', operation='HANDLE_PLAYER', agent_id=who, action='wait')
        self.assertEqual(self.session(who)['role'], 'player')   # its session call is the reconnection
        g = self.state()
        self.assertEqual(g['links'][who], 'up')
        self.assertGreater(g['players'][who]['lease_until'], self.now)
        self.call('agent/observe', self.pc(who))

    def test_after_the_gm_made_it_an_npc_the_gm_decides_about_giving_it_back(self):
        self.mutate(lambda g: g['rules'].update(lease_expiry='link'))
        who = self.ids[0]
        self.expire(who)
        self.call('gm/command', self.gm, command_id='h1', operation='HANDLE_PLAYER', agent_id=who, action='npc')
        self.session(who)
        self.assertIn(who, self.call('gm/observe', self.gm)['links']['reconnected_npcs'])
        self.call('gm/command', self.gm, command_id='h2', operation='HANDLE_PLAYER', agent_id=who, action='restore')
        self.assertEqual(self.state()['players'][who]['mode'], 'active')

    def test_a_planned_cut_is_not_reconnected_by_a_session_call(self):
        self.mutate(lambda g: g['rules'].update(lease_expiry='link'))
        self.set_faults({'kind': 'player', 'cycle': 2, 'slot': 0})
        self.to_cycle(2)
        cut = next(a for a, v in self.state()['links'].items() if v == 'down')
        self.assertEqual(self.session(cut)['role'], 'disconnected')
        self.assertEqual(self.state()['links'][cut], 'down')



class GMStandIn(Faults):   # A62: an agent that stood in as GM gets the game credited at its own average (fairness)
    def setUp(self):
        super().setUp()
        self.mutate(lambda g: g['rules'].update(gm_stand_in_credit=True))

    def agent(self, aid):
        with self.e.transaction() as db:
            return self.e.load(db, 'agents', aid)

    def stand_in(self, back=None, past=None):
        self.set_faults({'kind': 'gm', 'cycle': 2, **({'return_cycle': back} if back else {})})
        if past:   # every waiting agent has this past record (the successor is drawn among them)
            def setup(db):
                for a in self.pool['agents']:
                    rec = self.e.load(db, 'agents', a['agent_id'])
                    if rec['pool_role'] == 'player' and rec['agent_id'] not in self.ids:
                        rec.update(cumulative_score=past[0],
                                   history=[{'game_id': f'old{i}', 'resolution': 'winner'} for i in range(past[1])])
                        self.e.put(db, 'agents', rec['agent_id'], rec)
            with self.e.transaction() as db:
                setup(db)
        self.to_cycle(2)
        for a in self.ids:
            self.call('gm_absent/choose', self.pc(a), choice='appoint')
        g = self.state()
        self.gm = self.creds_of(g)
        return g['controller']['controller_id']

    def finish(self):
        self.mutate(lambda g: g.update(resolution='deadline'))
        self.call('gm/command', self.gm, operation='FINISH', command_id='end')

    def test_a_stand_in_with_a_past_gets_its_own_average(self):
        who = self.stand_in(past=(30, 2))
        self.finish()
        a = self.agent(who)
        self.assertEqual(len(a['history']), 2)                        # play history (jobs, missions) untouched
        self.assertEqual((a['gm_stand_in'][0]['final_score'], a['gm_stand_in'][0]['score_basis']), (15.0, 'own_average'))
        with self.e.transaction() as db:
            row = {r['agent_id']: r for r in Rules(self.e).standings(db, 'society')}[who]
        self.assertEqual((row['games'], row['average_score'], row['gm_stand_in']), (3, 15.0, 1))

    def test_a_stand_in_without_a_past_gets_the_games_average(self):
        who = self.stand_in()
        self.finish()
        g = self.state()
        scores = [g['final_feedback'][x]['final_score'] for x in self.ids]
        a = self.agent(who)
        self.assertEqual(a['gm_stand_in'][0]['score_basis'], 'game_average')
        self.assertAlmostEqual(a['gm_stand_in'][0]['final_score'], round(sum(scores) / len(scores), 2))
        self.assertEqual(a['history'], [])

    def test_a_stand_in_that_handed_back_still_gets_it(self):
        who = self.stand_in(back=3, past=(30, 2))
        self.to_cycle(3)
        self.gm = self.creds_of(self.state())
        self.assertNotEqual(self.state()['controller']['controller_id'], who)
        self.finish()
        self.assertEqual(self.agent(who)['gm_stand_in'][0]['final_score'], 15.0)

    def test_without_the_rule_nothing_is_credited(self):
        self.mutate(lambda g: g['rules'].update(gm_stand_in_credit=False))
        who = self.stand_in(past=(30, 2))
        self.finish()
        self.assertEqual((self.agent(who).get('gm_stand_in'), self.agent(who)['cumulative_score']), (None, 30))



class GMAbsentWaitCost(Faults):   # A63: each round of waiting for a cut GM costs a deadline cycle
    def setUp(self):
        super().setUp()
        self.mutate(lambda g: g['rules'].update(gm_absent_wait_cost=1, deadline_cycles=4))

    def cut_gm(self):
        self.set_faults({'kind': 'gm', 'cycle': 2})
        self.to_cycle(2)

    def vote(self, choice):
        self.now += 1000   # the next vote is open
        for a in self.ids:
            self.call('gm_absent/choose', self.pc(a), choice=choice)

    def view(self):
        return self.call('agent/observe', self.pc(self.ids[0]))['gm_absent']

    def test_the_rule_is_known_before_any_cut(self):
        d = self.call('agent/observe', self.pc(self.ids[0]))['scoring']['disconnection']
        self.assertEqual(d['gm_absent_wait_cost_cycles'], 1)

    def test_the_view_shows_the_cost_and_what_is_left(self):
        self.cut_gm()
        v = self.view()
        self.assertEqual((v['wait_cost_cycles'], v['remaining_cycles'], v['waited_rounds'], v['cycles_lost_to_waiting']), (1, 3, 0, 0))
        self.vote('wait')
        v = self.view()
        self.assertEqual((v['remaining_cycles'], v['waited_rounds'], v['cycles_lost_to_waiting']), (2, 1, 1))
        self.assertEqual(self.state()['rules']['deadline_cycles'], 3)

    def test_appointing_costs_nothing(self):
        self.cut_gm()
        self.vote('appoint')
        g = self.state()
        self.assertEqual((g['status'], g['rules']['deadline_cycles']), ('running', 4))

    def test_waiting_until_no_cycle_is_left_ends_the_game_at_its_deadline(self):
        self.cut_gm()
        for _ in range(3):
            self.vote('wait')
        g = self.state()
        self.assertEqual((g['ended'], g['resolution'], g['cycles_lost_to_waiting']), (True, 'deadline', 3))

    def test_without_the_rule_waiting_is_free(self):
        self.cut_gm()
        self.mutate(lambda g: g['rules'].update(gm_absent_wait_cost=0))
        self.vote('wait')
        self.assertEqual(self.state()['rules']['deadline_cycles'], 4)
        self.assertEqual(self.view()['remaining_cycles'], 3)



class ScenarioOffset(Faults):   # A64: a scenario's average counts only its own games
    def test_games_before_the_scenario_do_not_dilute_its_average(self):
        aid = self.ids[0]
        with self.e.transaction() as db:
            a = self.e.load(db, 'agents', aid)
            a.update(cumulative_score=40, scenario_games_offset=5,
                     history=[{'game_id': f'g{i}', 'resolution': 'winner'} for i in range(7)])   # 5 before, 2 in it
            self.e.put(db, 'agents', aid, a)
            row = {r['agent_id']: r for r in Rules(self.e).standings(db, 'society')}[aid]
        self.assertEqual((row['games'], row['average_score']), (2, 20.0))


if __name__ == '__main__':
    unittest.main()


class SitsOut(Faults):   # A61: a player made an NPC after a cut sits the game out (fairness)
    def setUp(self):
        super().setUp()
        self.mutate(lambda g: g['rules'].update(cut_player_sits_out=True, lease_expiry='link'))

    def cut_and_npc(self, back=None):
        self.set_faults({'kind': 'player', 'cycle': 2, 'slot': 0, **({'return_cycle': back} if back else {})})
        self.to_cycle(2)
        who = next(a for a, v in self.state()['links'].items() if v == 'down')
        self.call('gm/command', self.gm, command_id='h1', operation='HANDLE_PLAYER', agent_id=who, action='npc')
        return who

    def agent(self, aid):
        with self.e.transaction() as db:
            return self.e.load(db, 'agents', aid)

    def test_the_gm_may_make_a_cut_player_an_npc_and_give_it_back(self):   # inherited (A57): forbidden by this rule
        self.test_no_restore_after_reconnecting()

    def test_no_restore_after_reconnecting(self):
        who = self.cut_and_npc(back=3)
        self.to_cycle(3)
        self.assertIn(who, self.call('gm/observe', self.gm)['links']['reconnected_npcs'])
        self.error('conflict', lambda: self.call('gm/command', self.gm, command_id='h2', operation='HANDLE_PLAYER',
                                                 agent_id=who, action='restore'))

    def test_the_game_does_not_count_for_it_but_it_still_gets_its_feedback(self):
        who = self.cut_and_npc()
        self.mutate(lambda g: g.update(resolution='deadline'))
        self.call('gm/command', self.gm, operation='FINISH', command_id='end')
        a = self.agent(who)
        self.assertEqual((a.get('history'), a.get('cumulative_score', 0)), ([], 0))
        self.assertEqual(a['sat_out'][0]['game_id'], 'case')
        other = self.agent(next(x for x in self.ids if x != who))
        self.assertEqual(len(other['history']), 1)                       # the others played a counted game
        with self.e.transaction() as db:
            rows = {r['agent_id']: r for r in Rules(self.e).standings(db, 'society')}
        self.assertEqual((rows[who]['games'], rows[who]['sat_out']), (0, 1))
        fb = self.call('pool/feedback', agent_id=who, pool_token=a['pool_token'], game_id='case')['feedback'][who]
        self.assertEqual((fb['participated'], fb['final_score']), (False, 0))

    def test_standings_rank_by_the_average_of_counted_games(self):
        def setup(db):
            for aid, total, n in ((self.ids[0], 100, 2), (self.ids[1], 90, 1)):
                a = self.e.load(db, 'agents', aid)
                a.update(cumulative_score=total, history=[{'game_id': f'g{i}', 'resolution': 'winner'} for i in range(n)])
                self.e.put(db, 'agents', aid, a)
        with self.e.transaction() as db:
            setup(db)
            rows = Rules(self.e).standings(db, 'society')
        self.assertEqual([r['agent_id'] for r in rows[:2]], [self.ids[1], self.ids[0]])   # 90/1 > 100/2
