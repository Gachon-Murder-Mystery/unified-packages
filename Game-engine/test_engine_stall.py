"""FIX-A43: when the engine (or the whole machine) stood still, leases are shifted, not expired."""
import unittest

import test_competitive as base


class EngineStallTests(unittest.TestCase):
    _base = base.CompetitiveTests
    setUp = _base.setUp
    call, pc, state, mutate, picks = _base.call, _base.pc, _base.state, _base.mutate, _base.picks

    def test_a_long_gap_since_the_last_sweep_shifts_leases(self):
        self.e.tick()                      # the watchdog's last sweep before the stall
        self.now += 300                    # nothing ran for five minutes
        self.e.tick()
        g = self.state()
        self.assertTrue(all(p['mode'] == 'active' for p in g['players'].values()))
        self.assertIn('engine_stall', [e['type'] for e in g['events']])

    def test_a_silent_player_still_expires_while_the_engine_runs(self):
        self.e.tick()
        for _ in range(60):                # the engine keeps sweeping; one player never heartbeats
            self.now += 1
            self.call('gm/heartbeat', self.gm)   # the GM stays (else a player is promoted to GM)
            for aid in self.ids[1:]:
                self.call('agent/heartbeat', self.pc(aid))
            self.e.tick()
        g = self.state()
        self.assertEqual(g['players'][self.ids[0]]['mode'], 'npc')
        self.assertTrue(all(g['players'][a]['mode'] == 'active' for a in self.ids[1:]))


if __name__ == '__main__':
    unittest.main()
