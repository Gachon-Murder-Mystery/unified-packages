"""Tests for B's experiment-30 fixes (FIX-B1..B8): memory, prompt fields, leverage."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

B = Path(__file__).resolve().parents[1] / 'Agent_Reasoning'


def load_b():
    saved = {k: sys.modules.get(k) for k in ('schema', 'llm', 'agent')}
    try:
        mods = {}
        for name in ('schema', 'llm', 'agent', 'memory'):
            spec = importlib.util.spec_from_file_location(name, B / (name + '.py'))
            m = importlib.util.module_from_spec(spec)
            sys.modules[name] = m
            spec.loader.exec_module(m)
            mods[name] = m
        return mods
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


M = load_b()
POOLS = {'suspect': [{'id': i, 'name': f's{i}'} for i in range(12)],
         'weapon': [{'id': i, 'name': f'w{i}'} for i in range(8)],
         'place': [{'id': i, 'name': f'p{i}'} for i in range(8)]}


class FakePool:
    """pool/learning/get|update and pool/feedback, enough for ExperienceStore."""
    def __init__(self, history):
        self.history, self.version, self.state, self.updates = history, 0, {'tactics': [], 'weights': {}}, []
        self.events = [{'event_id': f'e{i}', 'strategy_consumed': False} for i in range(3)]

    def call(self, route, **d):
        if route == 'pool/learning/get':
            return {'learning': {'version': self.version, 'strategy_state': self.state},
                    'participation_history': self.history, 'experience_events': self.events}
        if route == 'pool/learning/update':
            assert d['expected_version'] == self.version
            self.version += 1
            self.state = d['strategy_state']
            self.updates.append(d)
            for e in self.events:
                e['strategy_consumed'] = e['event_id'] in d['experience_event_ids'] or e['strategy_consumed']
            return {}
        raise AssertionError(route)


class MemoryTests(unittest.TestCase):
    def test_rule_lessons_for_passive_timeout_and_wasted_explores(self):
        rich = {'score_if_wrong': 10, 'score_if_unsolved_and_alive': 0, 'my_mission_points': 30}
        poor = {'score_if_wrong': -20, 'score_if_unsolved_and_alive': 0, 'my_mission_points': 0}
        base = {'resolution': 'deadline', 'accused': False, 'cycles_played': 20, 'wasted_explores': 4}
        lessons, tactics, risk = M['memory'].rule_lessons(dict(base, final_scores=rich))
        self.assertEqual(risk, 1)     # FIX-B21: missions made a wrong accusation no loss, yet no accusation
        self.assertIn('prefer_unexplored_rooms', tactics)
        self.assertEqual(len(lessons), 2)
        lessons, _, risk = M['memory'].rule_lessons(dict(base, final_scores=poor))
        self.assertEqual(risk, 0)     # passing was right by the score table
        # FIX-B32: facts, not prescriptions (the shared "do X" sentences made every agent converge)
        self.assertIn('오답 점수 -20', lessons[-1])
        for text in lessons:
            self.assertNotRegex(text, '편이 낫다|하지 않는다\\.|옮긴다|교섭한다')

    def test_unsolved_game_states_the_cost_and_the_rival(self):  # FIX-B35
        stats = {'resolution': 'deadline', 'accused': False, 'died': False, 'final_score': -10,
                 'score_if_someone_had_won': 20, 'missions_voided_points': 20,
                 'final_combinations': 301, 'closest_rival_combinations': 34}
        lessons, _, _ = M['memory'].rule_lessons(stats)
        self.assertIn('누군가 맞혔다면 너는 20점이었다(차이 30점)', ' '.join(lessons))
        self.assertIn('가장 앞선 경쟁자는 남은 조합 34개까지 좁혔다. 너는 301개였다.', lessons)
        lead, _, _ = M['memory'].rule_lessons(dict(stats, final_combinations=12))
        self.assertIn('네가 가장 앞섰다(남은 조합 12개). 다음 경쟁자는 34개였다.', lead)
        dead, _, _ = M['memory'].rule_lessons(dict(stats, died=True, combinations_at_accuse=300))
        self.assertFalse(any('누군가 맞혔다면' in x for x in dead))   # a wrong accusation's score does not depend on others

    def test_wrong_accusation_with_banked_missions_is_not_punished(self):
        _, tactics, risk = M['memory'].rule_lessons({'died': True, 'accused': True, 'combinations_at_accuse': 300,
                                                     'scores_at_accuse': {'score_if_wrong': 0, 'score_if_unsolved_and_alive': 0}})
        self.assertEqual((tactics, risk), ([], 0))

    def test_rule_lessons_after_wrong_accusation(self):
        _, tactics, risk = M['memory'].rule_lessons({'died': True, 'accused': True, 'combinations_at_accuse': 300})
        self.assertEqual((tactics, risk), ([], -1))   # FIX-B32: no shared tactic pushed, the risk weight still records it

    def test_record_then_load_round_trip_and_engine_update(self):
        with tempfile.TemporaryDirectory() as d:
            history = [{'game_id': 'g1', 'participation_index': 1, 'character_id': 8, 'final_score': 0,
                        'rank': None, 'survived': True, 'sub_missions': []}]
            pool = FakePool(history)
            store = M['memory'].ExperienceStore(pool, 'society:abc', {'characters': [{'id': 8, 'job': '기술자'}]}, d)
            self.assertEqual(store.load()['games_played'], 1)  # engine history alone already counts
            saved = store.record('g1', {'resolution': 'time_out', 'accused': False, 'cycles_played': 13,
                                        'wasted_explores': 3, 'combinations_left': 400,
                                        'final_scores': {'score_if_wrong': 0, 'score_if_unsolved_and_alive': 0}},
                                 {'final_score': 0})
            self.assertTrue(saved['engine'].startswith('updated'))
            self.assertEqual(pool.state['weights']['risk'], 1)
            exp = store.load()
            self.assertEqual(exp['recent_games'][0]['job'], '기술자')
            self.assertEqual(exp['recent_games'][0]['wasted_explores'], 3)
            self.assertTrue(exp['lessons'])
            self.assertIn('prefer_unexplored_rooms', exp['tactics'][0])
            # next game: the LLM refines the pending reflection
            pending = store.pending_reflection()
            self.assertEqual(pending['game_id'], 'g1')
            pool.events.append({'event_id': 'e9', 'strategy_consumed': False})
            store.complete_reflection('g1', ['새 교훈'], ['verify_before_accuse'], -1)
            self.assertIsNone(store.pending_reflection())
            self.assertEqual(store.load()['lessons'][0], '새 교훈')
            self.assertEqual(pool.state['weights']['risk'], 0)

    def test_no_pool_credentials_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            store = M['memory'].ExperienceStore(None, 'society:x', None, d)
            self.assertEqual(store.load()['games_played'], 0)
            self.assertEqual(store.record('g', {}, None)['engine'], 'no_pool_credentials')


class BeliefLeverageTests(unittest.TestCase):
    def test_leverage_lists_what_a_place_exclusion_would_unlock(self):
        b = M['schema'].BeliefState(POOLS)
        b.apply({'id': 'c1', 'kind': 'indirect', 'type': 'character_room', 'character': 5, 'room': 4})
        b.apply({'id': 'c2', 'kind': 'indirect', 'type': 'weapon_room', 'weapon': 2, 'room': 4})
        b.apply({'id': 'c3', 'kind': 'indirect', 'type': 'companion', 'character': 6, 'other': 5})
        top = b.summary()['indirect_leverage'][0]
        self.assertEqual((top['if_place_excluded'], top['suspects_also_excluded'], top['weapons_also_excluded']),
                         (4, [5, 6], [2]))
        b.apply({'id': 'c4', 'kind': 'definite', 'category': 'place', 'candidate': 4})
        self.assertEqual(b.summary()['indirect_leverage'], [])  # unlocked: now real exclusions
        self.assertNotIn(5, b.remaining('suspect'))


class PromptTests(unittest.TestCase):
    def agent(self):
        a = M['agent'].LLMAgent('society:abc', catalog={'missions': [{'id': 'M20', 'name': '비폭력 협상가',
                                                                        'condition': '협박 0회'}]},
                                log_path=Path(tempfile.mkdtemp()) / 'log.jsonl')
        a.sync({'self': {'character_id': 0, 'mbti': 'INTP', 'mode': 'active', 'job': {'name': '탐정'},
                         'stats': {'observe': 5, 'speech': 3, 'pressure': 3}},
                'candidates': POOLS, 'cycle_id': 3, 'phase': 'interaction',
                'sub_missions': [{'mission_id': 'M20', 'tier': '하', 'achieved': False}]})
        return a

    def test_user_prompt_carries_the_new_fields(self):
        a = self.agent()
        a.experience = {'games_played': 2, 'lessons': ['x']}
        obs = {'cycle_id': 3, 'phase': 'interaction', 'self': a.profile,
               'time_budget': {'estimated_remaining_cycles': 4}, 'scoring': {'wrong_accusation': -20},
               'thresholds': {'explore': 2, 'by_target': {}}}
        u = json.loads(a._user_prompt(obs))
        self.assertEqual(u['experience']['games_played'], 2)
        self.assertEqual(u['time_budget']['estimated_remaining_cycles'], 4)
        self.assertEqual(u['accuse_math']['combinations_left'], 768)
        self.assertEqual(u['me']['thresholds']['source'], 'engine')
        self.assertGreaterEqual(len(u['me']['tendencies']), 4)   # FIX-B32: four axes + tension notes
        self.assertEqual(u['sub_missions'][0]['name'], '비폭력 협상가')
        self.assertIn('자정 규칙', a._system_prompt())

    def test_old_engine_falls_back_to_stats_and_says_so(self):
        u = json.loads(self.agent()._user_prompt({'self': {'stats': {'observe': 5}}}))
        self.assertTrue(u['me']['thresholds']['source'].startswith('stats_only'))

    def test_room_notes_mark_exhausted_rooms(self):
        a = self.agent()
        a.ingest_result({'action': 'explore', 'room': 3, 'roll': {'successes': 1}, 'acquired_information': []})
        self.assertTrue(a.room_notes[3]['exhausted'])
        self.assertEqual(a.game_stats('time_out')['wasted_explores'], 1)

    def test_trade_helpers_no_longer_raise(self):  # FIX-B7: remaining() needed a category
        a = self.agent()
        a.known_info = [{'id': 'c1', 'kind': 'definite', 'category': 'suspect', 'candidate': 1}]
        self.assertEqual(a.respond_to_trade({'requested': ['suspect']}), {'decision': 'accept', 'offer_ids': ['c1']})
        self.assertEqual(set(a._best_guess()), {'suspect', 'weapon', 'place'})


class AnswerRaceTests(unittest.TestCase):
    """FIX-B12: options change while the model answers (parallel stepping)."""

    def test_fallback_uses_current_options_and_never_crashes(self):
        stale = {'question_id': 'q1', 'successes': 2, 'asker_character': 3,
                 'response_options': {'definite': [], 'ambiguous': [],
                                      'indirect': [{'clue_id': 'i1', 'narrative_options': ['자정에 누군가 주방에 있었다']}]}}
        fresh = {**stale, 'response_options': {
            'definite': ['d1'],
            'ambiguous': [{'pair': {'place': 0, 'suspect': 0}}, {'pair': {'place': 0, 'weapon': 0}}],
            'indirect': stale['response_options']['indirect']}}

        class Role:
            def __init__(self):
                self.inbox_calls, self.accepted = 0, None

            def bind(self, **_):
                return self

            def call(self, route, **d):
                if route == 'questions/inbox':
                    self.inbox_calls += 1
                    return {'questions': [stale if self.inbox_calls == 1 else fresh]}
                if route == 'questions/respond':
                    kinds = [s['kind'] for s in d['statements']]
                    if kinds != ['definite'] and kinds.count('ambiguous') + kinds.count('indirect') < 2:
                        err = Exception('409 disclosure_required'); err.code = 'disclosure_required'
                        raise err
                    self.accepted = d['statements']
                    return {}
                raise AssertionError(route)

        sys.modules['agent'] = M['agent']
        try:
            spec = importlib.util.spec_from_file_location('_b_adapter_test', B / 'adapter.py')
            adapter = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(adapter)
        finally:
            sys.modules.pop('agent', None)
        role = Role()
        with tempfile.TemporaryDirectory() as d:
            agent = M['agent'].LLMAgent('society:x', log_path=Path(d) / 'l.jsonl')
            runtime = adapter.LLMPlayerRuntime(role, {'agent_id': 'society:x'}, {}, agent=agent, memory_dir=d)
            runtime._answer()
        self.assertEqual(role.accepted, [{'kind': 'definite', 'clue_id': 'd1'}])


class SayDoTests(unittest.TestCase):
    """FIX-B13: arm-20c seed 4/6 deaths: consideration said 'not yet', action said accuse."""

    def agent(self, replies):
        a = PromptTests.agent(PromptTests())
        a.client = object()
        replies = iter(replies)
        a._request_complete = lambda *_, **__: (json.dumps(next(replies), ensure_ascii=False), None)
        a._count = lambda usage: None
        return a

    OBS = {'phase': 'deduction', 'allowed_actions': ['accuse', 'pass'], 'cycle_id': 5}
    HYP = {'suspect': 0, 'weapon': 0, 'place': 0, 'confidence': 0.0015}

    def test_accuse_without_matching_decision_is_never_executed(self):
        bad = {'consideration': '아직 고발하지 않는다', 'current_hypothesis': self.HYP,
               'action': {'type': 'accuse', 'payload': {'suspect': 0, 'weapon': 0, 'place': 0}}}
        a = self.agent([dict(bad, decision='pass'), bad])
        self.assertEqual(a.decide(self.OBS), ('pass', {}))  # both rejected -> rule policy passes
        self.assertEqual(a.say_do_mismatches, {'decision_vs_action': 1, 'decision_missing': 1})

    def test_accuse_must_name_the_hypothesis(self):
        reply = {'consideration': '고발', 'current_hypothesis': self.HYP, 'decision': 'accuse',
                 'action': {'type': 'accuse', 'payload': {'suspect': 1, 'weapon': 0, 'place': 0}}}
        good = dict(reply, action={'type': 'accuse', 'payload': {'suspect': 0, 'weapon': 0, 'place': 0}})
        a = self.agent([reply, good])
        action, payload = a.decide(self.OBS)
        self.assertEqual((action, payload['suspect']), ('accuse', 0))
        self.assertEqual(a.say_do_mismatches, {'accuse_vs_hypothesis': 1})

    def test_consistent_pass_and_other_phases_are_untouched(self):
        a = self.agent([{'consideration': '보류', 'decision': 'pass', 'action': {'type': 'pass', 'payload': {}}}])
        self.assertEqual(a.decide(self.OBS), ('pass', {}))
        a = self.agent([{'consideration': '이동', 'action': {'type': 'stay', 'payload': {}}}])
        self.assertEqual(a.decide({'phase': 'movement', 'allowed_actions': ['move', 'stay']}), ('stay', {}))
        self.assertFalse(getattr(a, 'say_do_mismatches', {}))


if __name__ == '__main__':
    unittest.main()
