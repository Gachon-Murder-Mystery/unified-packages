"""v11 (CHANGES B67, B68, I26): request-keyed action slots, the replacement reason in the prompt, the review of a
refused proposal, and the stall watch of the runner."""
import importlib.util
import json
import sys
import threading
import unittest
from pathlib import Path

from test_rules_and_trades import Agent, RuntimeTradeFlow

ROOT = Path(__file__).resolve().parents[1]


def refused_view(tid='trade:abcdef1234567890', words='그건 이미 알아요'):
    return {'trade_id': tid, 'proposer': 'society:me', 'recipient': 'society:x', 'status': 'refused',
            'proposer_character': 0, 'recipient_character': 5, 'cycle': 4,
            'offer_types': [{'type': 'weapon', 'exclude': []}], 'requested': [{'type': 'place', 'exclude': []}],
            'messages': [{'from_character': 5, 'text': words}]}


def notice(reason, tid='34567890'):
    return [{'trade_id': tid, 'status': 'refused', 'gm_notices': [{'reason': reason, 'trade_id': 'trade:x'}]}]


class ActionSlot(unittest.TestCase):   # B67
    def test_two_identical_replacements_are_two_slots(self):
        rt, _ = RuntimeTradeFlow.runtime(RuntimeTradeFlow(), [])
        slot = type(rt).step.__globals__['action_slot']
        rep = {'allowed': ['question', 'explore'], 'reason': 'trade_refused', 'epoch': 35}
        first = {'cycle_id': 12, 'phase': 'interaction', 'phase_epoch': 35, 'replacement': rep,
                 'action_request': {'request_id': 'req:35:431'}}
        second = dict(first, action_request={'request_id': 'req:35:433'})
        self.assertNotEqual(slot(first), slot(second))

    def test_without_a_request_the_replacement_content_is_the_key(self):
        rt, _ = RuntimeTradeFlow.runtime(RuntimeTradeFlow(), [])
        slot = type(rt).step.__globals__['action_slot']
        base = {'cycle_id': 3, 'phase': 'interaction', 'phase_epoch': 9}
        self.assertNotEqual(slot(base), slot(dict(base, replacement={'reason': 'x'})))


class Replacement(unittest.TestCase):   # B68
    def test_the_reason_reaches_now(self):
        a = Agent.make()
        rep = {'allowed': ['question', 'explore'], 'reason': 'trade_refused'}
        self.assertEqual(a._now({'phase': 'interaction', 'replacement': rep})['replacement'], rep)
        self.assertNotIn('replacement', a._now({'phase': 'interaction'}))


class TradeReview(unittest.TestCase):   # B68
    def setUp(self):
        self.a = Agent.make()
        self.a.agent_of_char[5] = 'society:x'
        self.a.record_trade_result(refused_view(), 'society:me')

    def test_a_refusal_the_partner_chose_must_be_reviewed(self):
        self.a.trades_view = notice('refused')
        rows = self.a._reviews_view()
        self.assertEqual([(r['trade'], r['partner'], r['gm_reason']) for r in rows], [('34567890', 5, 'refused')])
        self.assertEqual(rows[0]['their_words'], '그건 이미 알아요')
        self.assertFalse(self.a._review_ok(None)[0])
        self.assertFalse(self.a._review_ok([{'trade': '34567890', 'why_not': '이미 가진 유형'}])[0])
        self.assertTrue(self.a._review_ok([{'trade': '34567890', 'why_not': '이미 가진 유형', 'change': '다른 사람'}])[0])

    def test_the_review_is_kept_in_the_partners_record_and_the_trajectory(self):
        self.a.trades_view = notice('another_chosen')
        self.a._apply_reviews([{'trade': '34567890', 'why_not': '더 좋은 제안이 있었다', 'change': '조건을 올린다'}])
        card = self.a.situation.cards[5]['trades'][-1]
        self.assertEqual(card['my_review'], {'why_not': '더 좋은 제안이 있었다', 'change': '조건을 올린다'})
        self.assertEqual(self.a.trade_reviews_due, [])
        self.assertEqual(self.a.trajectory[-1]['kind'], 'trade_review')

    def test_a_missing_review_is_asked_once(self):
        self.a.trades_view = notice('refused')
        self.a._apply_reviews(None)
        self.assertEqual(self.a.trade_reviews_due, [])
        self.assertNotIn('my_review', self.a.situation.cards[5]['trades'][-1])

    def test_mechanical_failures_are_not_reviewed(self):
        self.a.trades_view = notice('recipient_action_used')
        self.assertEqual(self.a._reviews_view(), [])
        self.assertEqual(self.a.trade_reviews_due, [])
        self.assertTrue(self.a._review_ok(None)[0])

    def test_waits_for_the_gm_notice_for_one_cycle(self):
        self.a.trades_view = []
        self.assertEqual(self.a._reviews_view(), [])
        self.assertEqual(len(self.a.trade_reviews_due), 1)
        self.a.cycle = 6
        self.a._reviews_view()
        self.assertEqual(self.a.trade_reviews_due, [])

    def test_a_proposal_made_to_me_is_not_mine_to_review(self):
        a = Agent.make()
        view = dict(refused_view(), proposer='society:x', recipient='society:me', proposer_character=5, recipient_character=0)
        a.record_trade_result(view, 'society:me')
        self.assertEqual(a.trade_reviews_due, [])

    def test_prompt_lists_it_and_lean_keeps_it(self):
        self.a.trades_view = notice('refused')
        self.assertIn('trade_review_due', self.a.LEAN_SITUATION['movement'])
        self.assertIn('trade_review_due', self.a.LEAN_SITUATION['deduction'])
        self.assertTrue(self.a._situation_block([])['trade_review_due'])


class StallWatch(unittest.TestCase):   # I26
    def setUp(self):
        spec = importlib.util.spec_from_file_location('_run_game_v11', ROOT / 'run_game.py')
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def watch(self, seqs, outage=lambda: False, limit=100):
        t = {'now': 0.0}
        seq = iter(seqs)

        class Stop:   # a fake Event: each wait advances the clock by one poll
            def wait(self_, poll):
                t['now'] += poll
                return t['now'] > 1000
        stalled, called = threading.Event(), []
        self.m.stall_watch(lambda: next(seq, seqs[-1]), Stop(), stalled, lambda: called.append(1), outage,
                           limit=limit, poll=30, clock=lambda: t['now'])
        return stalled.is_set(), called

    def test_no_event_for_the_limit_stops_the_game(self):
        self.assertEqual(self.watch([5]), (True, [1]))

    def test_moving_events_never_stop_it(self):
        self.assertEqual(self.watch(list(range(100))), (False, []))

    def test_a_model_outage_pause_does_not_count(self):
        self.assertEqual(self.watch([5], outage=lambda: True), (False, []))


if __name__ == '__main__':
    unittest.main()


class SeatLost(unittest.TestCase):   # C25
    def worker(self, sessions, error_code):
        sys.path.insert(0, str(ROOT))
        from GM_Agent_Society import agent_runtime
        from GM_Agent_Society.engine_v1 import EngineV1Error
        seq = iter(sessions)

        class Identity:
            credentials = {}
            def call(self, route, **_):
                if route == 'catalog':
                    return {}
                if route == 'pool/session':
                    return next(seq)
                return {}
            def bind(self, **_):
                return self

        class Player:
            def step(self_):
                raise EngineV1Error(401, {'code': error_code, 'error': 'own current player credential required'})
        w = agent_runtime.AgentWorker.__new__(agent_runtime.AgentWorker)
        w.identity, w.factory, w.catalog, w.runtimes = Identity(), (lambda *a: Player()), {}, []
        w.gm = type('G', (), {'gm': None})()
        w.player, w.context = None, None
        return w

    def test_a_seat_taken_by_the_engine_stops_the_runtime_not_the_game(self):
        playing = {'role': 'player', 'context_key': 'g1:a', 'credentials': {}}
        w = self.worker([playing, {'role': 'dormant', 'credentials': None}], 'unauthorized')
        self.assertEqual(w.step(), 'player:seat_lost')
        self.assertIsNone(w.player)

    def test_still_seated_errors_still_surface(self):
        playing = {'role': 'player', 'context_key': 'g1:a', 'credentials': {}}
        w = self.worker([playing, playing], 'unauthorized')
        with self.assertRaises(Exception):
            w.step()

    def test_other_errors_are_not_swallowed(self):
        playing = {'role': 'player', 'context_key': 'g1:a', 'credentials': {}}
        w = self.worker([playing, {'role': 'dormant'}], 'conflict')
        with self.assertRaises(Exception):
            w.step()


class GMHandover(unittest.TestCase):   # C27
    def setUp(self):
        sys.path.insert(0, str(ROOT))
        import tempfile
        from GM_Agent_Society import gm_memory
        self.m = gm_memory
        self.dir = tempfile.mkdtemp()

    def test_a_successor_works_from_the_gm_memory_and_never_writes_it(self):
        gm = self.m.GMMemory('society:gm', directory=self.dir)
        gm.learn('no_action', 'wait', True, 30)
        gm.record_game({'game': 1})
        before = (Path(self.dir) / 'gm_society_gm.json').read_text(encoding='utf-8')
        succ = self.m.GMMemory('society:abc', directory=self.dir, context={'game_id': 'g2', 'term': 2})
        self.assertEqual(succ.data['playbook']['no_action']['wait']['tries'], 1)   # inherited
        succ.note_fault('gm_succeeded', {'reason': 'gm_disconnected', 'term': 2}, 8)
        succ.learn('no_action', 're_request', True, 12)
        succ.record_game({'game': 2})
        self.assertEqual((Path(self.dir) / 'gm_society_gm.json').read_text(encoding='utf-8'), before)
        rows = json.loads((Path(self.dir) / 'gm_handover.json').read_text(encoding='utf-8'))
        self.assertEqual(rows[0]['gm_fault'][0]['reason'], 'gm_disconnected')
        self.assertEqual([o['op'] for o in rows[0]['ops']], ['learn', 'game'])

    def test_the_gm_learns_the_handover_once_at_its_next_term(self):
        self.m.GMMemory('society:gm', directory=self.dir).save()
        succ = self.m.GMMemory('society:abc', directory=self.dir, context={'game_id': 'g2', 'term': 2})
        succ.learn('no_action', 're_request', True, 12)
        succ.save()
        gm = self.m.GMMemory('society:gm', directory=self.dir)
        self.assertEqual(gm.data['playbook']['no_action']['re_request']['resolved'], 1)
        self.assertEqual(gm.data['handovers'][0]['from'], 'society:abc')
        again = self.m.GMMemory('society:gm', directory=self.dir)   # nothing learned twice
        self.assertEqual(again.data['playbook']['no_action']['re_request']['tries'], 1)
        succ.learn('player_cut', 'wait', True, 5)   # the successor went on after a first hand-over
        succ.save()
        later = self.m.GMMemory('society:gm', directory=self.dir)
        self.assertEqual(later.data['playbook']['player_cut']['wait']['tries'], 1)
        self.assertEqual(later.data['playbook']['no_action']['re_request']['tries'], 1)

    def test_re_request_is_a_remedy_for_a_silent_player(self):
        self.assertIn('re_request', self.m.REMEDIES['no_action'])


class Disconnected(unittest.TestCase):   # B69
    def test_a_cut_player_keeps_its_runtime_for_when_it_comes_back(self):
        sys.path.insert(0, str(ROOT))
        from GM_Agent_Society import agent_runtime
        sessions = iter([{'role': 'player', 'context_key': 'g1:a', 'credentials': {}},
                         {'role': 'disconnected'},
                         {'role': 'player', 'context_key': 'g1:a', 'credentials': {}}])
        made = []

        class Identity:
            credentials = {}
            def call(self, route, **_):
                return next(sessions) if route == 'pool/session' else {}
            def bind(self, **_):
                return self

        class Player:
            def step(self_):
                return 'ok'
        w = agent_runtime.AgentWorker.__new__(agent_runtime.AgentWorker)
        w.identity, w.catalog, w.runtimes = Identity(), {}, []
        w.factory = lambda *a: made.append(1) or Player()
        w.gm = type('G', (), {'gm': None})()
        w.player, w.context = None, None
        self.assertEqual([w.step(), w.step(), w.step()], ['player:ok', 'disconnected', 'player:ok'])
        self.assertEqual(len(made), 1)   # the same runtime (its in-game memory) after the cut


class GMHandoverFaultOnly(unittest.TestCase):   # C27: a GM fault with nothing else learned still reaches the GM
    def test_the_fault_record_alone_is_received(self):
        sys.path.insert(0, str(ROOT))
        import tempfile
        from GM_Agent_Society import gm_memory
        d = tempfile.mkdtemp()
        succ = gm_memory.GMMemory('society:abc', directory=d, context={'game_id': 'g', 'term': 2})
        succ.note_fault('gm_succeeded', {'reason': 'gm_disconnected', 'term': 2}, 8)
        gm = gm_memory.GMMemory('society:gm', directory=d)
        self.assertEqual(gm.data['handovers'][0]['gm_fault'][0]['reason'], 'gm_disconnected')
        self.assertEqual(len(gm_memory.GMMemory('society:gm', directory=d).data['handovers']), 1)   # once


def _gm_modules():
    sys.path.insert(0, str(ROOT))
    from GM_Agent_Society import gm_memory, competitive_gm
    return gm_memory, competitive_gm


class GMLessons(unittest.TestCase):   # C28: feedback with its reason binds the GM's choice and carries on
    def setUp(self):
        import tempfile
        self.m, _ = _gm_modules()
        self.dir = tempfile.mkdtemp()

    def test_a_lesson_binds_the_choice_even_against_the_counts(self):
        gm = self.m.GMMemory('society:gm', directory=self.dir)
        for _ in range(5):
            gm.learn('player_cut', 'wait', True, 1)
        gm.add_lesson('player_cut', 'an empty seat holds every question to it', prefer='npc', avoid=['wait'])
        self.assertEqual({gm.choose('player_cut') for _ in range(20)}, {'npc'})

    def test_the_lesson_carries_to_the_next_game_and_to_a_successor(self):
        gm = self.m.GMMemory('society:gm', directory=self.dir)
        gm.add_lesson('player_cut', 'why', prefer='npc', avoid=['wait'])
        gm.save()
        self.assertEqual(self.m.GMMemory('society:gm', directory=self.dir).choose('player_cut'), 'npc')
        succ = self.m.GMMemory('society:abc', directory=self.dir, context={'game_id': 'g', 'term': 2})
        self.assertEqual(succ.choose('player_cut'), 'npc')

    def test_a_lesson_must_name_real_remedies_and_the_newest_one_counts(self):
        gm = self.m.GMMemory('society:gm', directory=self.dir)
        with self.assertRaises(ValueError):
            gm.add_lesson('player_cut', 'why', prefer='kick')
        gm.add_lesson('player_cut', 'old', prefer='wait')
        gm.add_lesson('player_cut', 'new', prefer='npc')
        self.assertEqual((gm.lesson('player_cut')['why'], len(gm.lessons())), ('new', 2))

    def test_the_feedback_tool_writes_the_pool_gm_memory(self):
        self.m.GMMemory('society:gm', directory=self.dir).save()
        spec = importlib.util.spec_from_file_location('gm_feedback', ROOT / 'tools' / 'gm_feedback.py')
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        tool.main(['--memory', self.dir, '--kind', 'player_cut', '--prefer', 'npc', '--avoid', 'wait', '--why', 'w'])
        data = json.loads((Path(self.dir) / 'gm_society_gm.json').read_text(encoding='utf-8'))
        self.assertEqual(data['lessons'][0]['prefer'], 'npc')


class GMTimeScore(unittest.TestCase):   # C29: a smooth game wastes no time
    def setUp(self):
        self.m, self.cg = _gm_modules()

    def test_a_fix_that_took_long_scores_less(self):
        gm = self.m.GMMemory('society:gm', directory='')
        gm.learn('no_action', 'wait', True, 0, scale=100)
        gm.learn('no_action', 're_request', True, 300, scale=100)
        book = gm.data['playbook']['no_action']
        self.assertGreater(book['wait']['a'], book['re_request']['a'])
        self.assertAlmostEqual(book['re_request']['a'] + book['re_request']['b'], 1.0, places=3)

    def test_the_game_time_score_counts_stage_time_over_the_usual(self):
        gm = self.m.GMMemory('society:gm', directory='')
        gm.data['norms']['interaction:negotiation'] = [60.0] * 10
        t = [0.0]
        w = self.m.IncidentWatch(gm, clock=lambda: t[0])
        def v(epoch, n):
            return {'phase': 'interaction', 'stage': 'negotiation', 'phase_epoch': epoch, 'active_agents': ['a'],
                    'spent': {'a': 1} if n else {}, 'needs_request': [], 'overdue': [], 'questions': []}
        w.observe(v(1, 0)); t[0] = 60; w.observe(v(2, 0))     # usual: 60 s
        t[0] = 300; w.observe(v(3, 0))                        # 240 s: 180 s wasted
        s = w.summary()['time']
        # [C31] a slow stage with no incident is outside the GM's authority: recorded, not scored
        self.assertEqual((s['actual_s'], s['typical_s'], s['wasted_s'], s['score']), (300, 120, 180, 100))
        self.assertEqual((s['accountable_s'], s['outside_authority_s']), (0, 180))
        row = gm.time_score('g', s['actual_s'], s['typical_s'], s['wasted_s'], s['causes'])
        self.assertEqual(gm.data['time_scores'][-1]['score'], row['score'])

    def test_only_incident_time_within_authority_is_scored(self):   # C31
        gm = self.m.GMMemory('society:gm', directory='')
        row = gm.time_score('g', 1000, 600, 400, {'player_cut': 250, 'game_paused': 40})
        self.assertEqual((row['score'], row['accountable_s'], row['outside_authority_s']), (75, 250, 150))

    def test_old_rows_are_rescored_and_keep_the_old_score(self):   # C31
        gm = self.m.GMMemory('society:gm', directory='')
        gm.data['time_scores'] = [{'game_id': 'g', 'score': 60, 'actual_s': 3403, 'typical_s': 2029,
                                   'wasted_s': 1374, 'causes': {}}]
        gm.rescore_time()
        row = gm.data['time_scores'][0]
        self.assertEqual((row['score'], row['score_c29'], row['outside_authority_s']), (100, 60, 1374))

    def test_unanswered_questions_to_a_waited_player_cost_the_wait_and_reopen_the_decision(self):
        import types
        gm = self.m.GMMemory('society:gm', directory='')
        stub = types.SimpleNamespace(memory=gm, ledger={'cycle': 12, 'incidents': []},
                                     watch=types.SimpleNamespace(open={'5:answer_pending': {
                                         'kind': 'answer_pending', 'subject': [3], 'since': 0.0}}),
                                     link_decisions={'society:x': ('player_cut', 'wait', 0.0)})
        self.cg.CompetitiveGM._wait_costs(stub, {'characters': [{'controller': 'society:x', 'character_id': 3}]})
        self.assertNotIn('society:x', stub.link_decisions)          # decided again at the next tick
        self.assertIn('society:x', stub.wait_failed)
        self.assertEqual(gm.data['playbook']['player_cut']['wait']['tries'], 1)
        self.assertEqual(stub.ledger['incidents'][0]['kind'], 'wait_cost')
        self.assertEqual(gm.choose('player_cut', exclude=('wait',)), 'npc')


class GMTermEndSaves(unittest.TestCase):   # C30: a fenced GM term keeps what it learned
    def test_memory_is_saved_when_the_term_is_taken_away(self):
        import types
        _, cg = _gm_modules()
        from GM_Agent_Society.engine_v1 import AuthorityLost
        saved = []
        def tick():
            raise AuthorityLost('fenced')
        gm = types.SimpleNamespace(reported=False, tick=tick, memory=types.SimpleNamespace(save=lambda: saved.append(1)))
        worker = types.SimpleNamespace(gm=gm, poll=lambda: gm)
        self.assertEqual(cg.CompetitiveGMWorker.step(worker), 'fenced')
        self.assertEqual((saved, worker.gm), ([1], None))
