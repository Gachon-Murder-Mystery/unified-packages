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
            store.complete_reflection('g1', {'plan': '새 계획', 'plan_check': '', 'tactics': ['verify_before_accuse'],
                                             'risk': -1, 'operations': [{'op': 'ADD', 'text': '새 통찰'}]})
            self.assertIsNone(store.pending_reflection())
            exp = store.load()
            self.assertEqual(exp['plan_for_this_game'], '새 계획')                 # FIX-B37 Reflexion plan
            self.assertEqual(exp['insights'], [{'no': 1, 'text': '새 통찰', 'score': 2}])   # FIX-B37 ExpeL
            self.assertNotEqual(exp['lessons'][0], '새 계획')                     # facts are never overwritten
            self.assertEqual(pool.state['weights']['risk'], 0)
            self.assertEqual(pool.state['tactics'], ['verify_before_accuse'])     # replaced, not merged

    def test_no_pool_credentials_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            store = M['memory'].ExperienceStore(None, 'society:x', None, d)
            self.assertEqual(store.load()['games_played'], 0)
            self.assertEqual(store.record('g', {}, None)['engine'], 'no_pool_credentials')


class ReflexionExpelTests(unittest.TestCase):
    """FIX-B37 (user decision 2026-11-09): textbook II ch.7 Reflexion + ExpeL in the agent's memory."""

    def test_expel_operations_follow_the_paper_scores(self):
        ops = M['memory'].apply_operations
        items, applied = ops([], [{'op': 'ADD', 'text': 'a'}, {'op': 'ADD', 'text': 'b'}, {'op': 'ADD', 'text': 'c'}], 1)
        self.assertEqual([(x['no'], x['score']) for x in items], [(1, 2), (2, 2)])   # ADD starts at 2, at most 2 per game
        items, applied = ops(items, [{'op': 'AGREE', 'no': 1}, {'op': 'REMOVE', 'no': 2}, {'op': 'AGREE', 'no': 1},
                                     {'op': 'EDIT', 'no': 9, 'text': 'x'}, {'op': 'bogus'}], 2)
        self.assertEqual([(x['no'], x['score']) for x in items], [(1, 3), (2, 1)])   # one op per insight per game
        self.assertEqual(len(applied), 2)
        items, _ = ops(items, [{'op': 'REMOVE', 'no': 2}, {'op': 'EDIT', 'no': 1, 'text': 'a2'}], 3)
        self.assertEqual([(x['no'], x['text'], x['score']) for x in items], [(1, 'a2', 4)])  # score 0 -> dropped
        many = [{'no': i, 'text': str(i), 'score': 1 + i % 3, 'born': 1, 'updated': i} for i in range(1, 13)]
        kept, _ = ops(many, [], 4)
        self.assertEqual(len(kept), M['memory'].INSIGHT_CAP)
        self.assertNotIn(3, [x['no'] for x in kept])                                  # weakest and oldest go first

    def test_pending_reflection_carries_previous_plans_and_insights(self):
        with tempfile.TemporaryDirectory() as d:
            store = M['memory'].ExperienceStore(None, 'society:r', None, d)
            store.record('g1', {'final_combinations': 300, 'definite_clues': 4, 'settled_trades': 1,
                                'trajectory': ['c1 x']}, None)
            store.complete_reflection('g1', {'plan': 'p1', 'plan_check': '', 'operations': [{'op': 'ADD', 'text': 'i1'}]})
            store.record('g2', {'final_combinations': 200, 'trajectory': ['c1 y']}, None)
            pending = store.pending_reflection()
            self.assertEqual(pending['game_id'], 'g2')
            self.assertEqual(pending['previous_reflections'][0]['plan'], 'p1')
            self.assertEqual(pending['previous_reflections'][0]['written_after_game_with']['final_combinations'], 300)
            self.assertEqual(pending['insights'][0]['text'], 'i1')
            store.complete_reflection('g2', None)                                    # LLM failed: insights kept
            self.assertEqual(store.load()['insights'][0]['score'], 2)
            for k in range(3, 7):
                store.record(f'g{k}', {'trajectory': ['c1 z']}, None)
            games = store._local()['games']
            self.assertEqual(sum(1 for g in games if 'trajectory' in g['stats']), M['memory'].TRAJECTORY_GAMES)

    def agent(self, reply=None):
        sent = []

        class Client:
            def request(self, system, user, budget=0):
                sent.append((system, json.loads(user)))
                return reply or '{}', {'output': 5}
        a = M['agent'].LLMAgent('society:t', client=Client(), log_path=Path(tempfile.mkdtemp()) / 'log.jsonl')
        a.sync({'self': {'character_id': 0, 'mbti': 'ISTJ', 'mode': 'active', 'room': 2, 'job': {'name': '탐정'},
                         'stats': {'observe': 5, 'speech': 3, 'pressure': 3}},
                'candidates': POOLS, 'cycle_id': 4, 'phase': 'exploration',
                'information': [{'id': 'c1', 'kind': 'definite', 'category': 'weapon', 'candidate': 3}]})
        return a, sent

    def test_trajectory_records_position_actions_and_outcomes(self):
        a, _ = self.agent()
        a.ingest_result({'action': 'explore', 'room': 2, 'cycle_id': 4, 'roll': {'successes': 1},
                         'acquired_information': [{'id': 'c1', 'kind': 'definite', 'category': 'weapon', 'candidate': 3}],
                         'room_remaining': 0})
        a.ingest_result({'action': 'question', 'cycle_id': 4, 'roll': {'successes': 0}, 'outcome': 'failure',
                         'acquired_information': []}, {'target_id': 5, 'approach': 'persuade'})
        stats = a.game_stats('deadline')
        text = '\n'.join(stats['trajectory'])
        self.assertIn('c4 시작: 칸 2', text)
        self.assertIn('탐색 칸 2', text)
        self.assertIn('질문 → 인물 5 (persuade)', text)
        self.assertEqual(stats['subtask_contrast']['counts']['question'], {'success': 0, 'failure': 1})
        self.assertEqual(stats['definite_clues'], 1)

    def test_reflection_prompt_follows_reflexion_and_expel(self):
        reply = json.dumps({'plan_check': '따랐다', 'plan': '3사이클 안에 거래 제안', 'risk': 0,
                            'operations': [{'op': 'AGREE', 'no': 1}, {'op': 'ADD', 'text': 't'}], 'tactics': ['share_when_stalled']},
                           ensure_ascii=False)
        a, sent = self.agent(reply)
        entry = {'game_id': 'g', 'stats': {'trajectory': ['c1 시작'], 'subtask_contrast': {'counts': {}}},
                 'lessons': ['사실'], 'previous_reflections': [{'plan': 'old'}],
                 'insights': [{'no': 1, 'text': 'i', 'score': 2}]}
        result = a.reflect(entry)
        self.assertEqual(result['plan'], '3사이클 안에 거래 제안')
        self.assertEqual(result['operations'][1], {'op': 'ADD', 'text': 't'})
        system, user = sent[0]
        for word in ('Reflexion', 'ExpeL', 'AGREE', 'REMOVE', 'previous_reflections', '성격을 버리거나'):
            self.assertIn(word, system)
        self.assertEqual(user['trajectory'], ['c1 시작'])
        self.assertNotIn('trajectory', user['this_game'])
        self.assertEqual(user['previous_reflections'], [{'plan': 'old'}])
        self.assertIsNone(self.agent('{}')[0].reflect(entry))                      # nothing usable -> keep facts


class ResponseMemoryTests(unittest.TestCase):
    """FIX-B38/B40: what my actions got me, and how each person answered me, reach the decision prompt."""

    def agent(self):
        a = M['agent'].LLMAgent('society:m', log_path=Path(tempfile.mkdtemp()) / 'log.jsonl')
        a.sync({'self': {'character_id': 0, 'mbti': 'ENFP', 'mode': 'active', 'room': 2, 'job': {'name': '탐정'},
                         'stats': {'observe': 5}}, 'candidates': POOLS, 'cycle_id': 5, 'phase': 'interaction',
                'phase_epoch': 9, 'information': []})
        return a

    def test_settled_result_after_an_observation_still_counts_its_clues(self):
        a = self.agent()
        a._known_at_decision = set()          # decide() took the snapshot: I held nothing
        clue = {'id': 'q1', 'kind': 'definite', 'category': 'suspect', 'candidate': 4}
        a.sync({'self': a.profile, 'candidates': POOLS, 'cycle_id': 5, 'phase': 'deduction', 'phase_epoch': 10,
                'information': [clue]})       # the observation lists the answer before the receipt arrives
        a.ingest_result({'action': 'question', 'cycle_id': 5, 'outcome': 'acquired', 'roll': {'successes': 2},
                         'acquired_information': [clue]}, {'target_id': 7, 'approach': 'question'})
        self.assertEqual(a.action_log[-1]['new_clues'], 1)
        self.assertEqual(a.game_stats('deadline')['question_clues'], 1)

    def test_people_here_carry_this_games_dealings(self):
        a = self.agent()
        a.record_trade_result({'trade_id': 't1', 'proposer': 'society:m', 'recipient': 'society:x', 'recipient_character': 7,
                               'status': 'refused', 'cycle': 4, 'offer_types': ['weapon'],
                               'requested': [{'type': 'place', 'exclude': [1]}]}, 'society:m')
        a.hear(7, 'trade', '장소 단서는 없어요.')
        a.ingest_result({'action': 'question', 'cycle_id': 3, 'outcome': 'acquired', 'roll': {'successes': 2},
                         'acquired_information': [{'id': 'w9', 'kind': 'definite', 'category': 'weapon', 'candidate': 2}]},
                        {'target_id': 7, 'approach': 'persuade'})
        d = a._occupant({'id': 7, 'agent_id': 'society:x', 'mode': 'active'})['this_game_with_me']
        self.assertEqual(d['trades_with_me']['count'], {'refused': 1})
        self.assertEqual(d['said']['place']['text'], '장소 단서는 없어요.')
        self.assertEqual(d['my_questions']['clues_from_them'], 1)
        self.assertEqual((d['holdings']['place']['state'], d['holdings']['place']['grade']), ('없음', '가능성'))
        self.assertEqual((d['holdings']['weapon']['state'], d['holdings']['weapon']['grade']), ('있음', '확인'))
        self.assertIsNone(a._occupant({'id': 3, 'mode': 'active'})['this_game_with_me'])


class SituationMemoryTests(unittest.TestCase):
    """FIX-B41/B42 (user decision 2026-11-09): own situation memory, superseded not evicted; words are '가능성'."""

    def mem(self):
        spec = importlib.util.spec_from_file_location('situation_t', B / 'situation.py')
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_nothing_is_evicted_by_count(self):
        S = self.mem()
        s = S.SituationMemory()
        s.heard(5, 3, 'trade', '무기 단서는 갖고 있지 않아요.')
        for k in range(40):                                   # 40 later remarks from others
            s.heard(k % 4, 4 + k // 4, 'question', '오늘 날씨 얘기나 하죠.')
        self.assertEqual(s.view(5)['holdings']['weapon']['state'], '없음')
        self.assertEqual(s.view(5)['said']['weapon']['cycle'], 3)

    def test_confirmed_holding_survives_words_and_nothing_is_flagged_for_me(self):   # FIX-B63
        S = self.mem()
        s = S.SituationMemory()
        s.offered(2, ['weapon'], 6)                           # engine-checked declaration
        s.heard(2, 8, 'trade', '무기 단서가 있어요.')
        s.heard(2, 9, 'trade', '무기는 없습니다.')
        v = s.view(2)
        self.assertEqual((v['holdings']['weapon']['state'], v['holdings']['weapon']['grade']), ('있음', '확인'))
        self.assertNotIn('contradictions', v)                 # the code does not judge; the words stay
        self.assertEqual(v['said']['weapon']['before']['text'], '무기 단서가 있어요.')
        self.assertFalse(hasattr(s, 'notable'))

    def test_possibility_carries_who_it_met_since_and_seek_ranks_confirmed_first(self):
        S = self.mem()
        s = S.SituationMemory()
        s.heard(4, 2, 'trade', '장소 단서 드릴 수 있어요.')
        s.gave_me(9, ['place'], 5, '거래')
        s.placements([{'cycle': 3, 'rooms': {'4': 1, '6': 1, '9': 2}}, {'cycle': 6, 'rooms': {'4': 0, '9': 0}}])
        self.assertEqual(s.view(4)['holdings']['place']['grade'], '가능성')
        self.assertEqual(s.view(4)['holdings']['place']['met_since'], [6, 9])
        self.assertEqual(s.view(4)['last_seen'], {'room': 0, 'cycle': 6})
        rows = s.seek(['place'], here={4})['place']
        self.assertEqual([r['id'] for r in rows], [9, 4])

    def test_topic_words_are_specific(self):
        S = self.mem()
        self.assertEqual(S.topics_of('흉기 위치 단서와 인물 단서'), ['weapon_room', 'suspect'])
        self.assertEqual(S.stance_of('그건 못 드려요'), '없음')

    def test_notes_are_edited_only_by_the_agent(self):
        S = self.mem()
        n = S.Notes()
        for k in range(S.NOTE_CAP):
            n.apply([{'op': 'add', 'text': f'n{k}'}], 1)
        applied, refused = n.apply([{'op': 'add', 'text': 'x'}, {'op': 'pin', 'no': 1}, {'op': 'edit', 'no': 2, 'text': 'e'}], 2)
        self.assertEqual(len(n.items), S.NOTE_CAP)               # full: nothing dropped automatically
        self.assertIn('가득', refused[0]['why'])
        self.assertEqual([x['op'] for x in applied], ['pin', 'edit'])
        self.assertEqual(n.view()['notes'][0]['no'], 1)          # pinned first
        n.apply([{'op': 'drop', 'no': 3}, {'op': 'add', 'text': 'new'}], 3)   # the agent makes room itself
        self.assertEqual(len(n.items), S.NOTE_CAP)
        self.assertIn('new', [x['text'] for x in n.items])

    def test_decision_output_memory_reaches_the_next_prompt(self):
        a = ResponseMemoryTests.agent(self)
        a._memo([{'op': 'add', 'text': '7번은 무기를 아낀다'}, {'op': 'drop', 'no': 99}])
        u = json.loads(a._user_prompt({'self': a.profile, 'cycle_id': 5, 'phase': 'interaction'}))
        self.assertEqual(u['situation']['my_notes']['notes'][0]['text'], '7번은 무기를 아낀다')
        self.assertEqual(u['situation']['memory_feedback']['refused'][0]['why'], '없는 번호')
        self.assertNotIn('heard_claims', u)                       # the FIFO list is gone from the prompt
        stats = a.game_stats('deadline')
        self.assertEqual(stats['final_notes'], ['7번은 무기를 아낀다'])


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
        # [FIX-B52] no computed probability or count reaches the agent
        self.assertNotIn('combinations_left', u['accuse_math'])
        self.assertNotIn('combinations_left', u['belief'])
        self.assertNotIn('total_entropy', u['belief'])
        for cat in ('suspect', 'weapon', 'place'):
            self.assertNotIn('entropy', u['belief'][cat])
            self.assertTrue(all(set(x) == {'id', 'name'} for x in u['belief'][cat]['remaining']))
        self.assertEqual(a._combinations(), 768)   # still kept for research logs
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
        math = {'my_win_chance': 0.0, 'expected_if_accuse': -20, 'expected_if_pass': -10, 'pass_assumption': '다음 기회'}  # FIX-B44
        a = self.agent([{'consideration': '보류', 'decision': 'pass', 'deduction_math': math, 'action': {'type': 'pass', 'payload': {}}}])
        self.assertEqual(a.decide(self.OBS), ('pass', {}))
        a = self.agent([{'consideration': '이동', 'action': {'type': 'stay', 'payload': {}}}])
        self.assertEqual(a.decide({'phase': 'movement', 'allowed_actions': ['move', 'stay']}), ('stay', {}))
        self.assertFalse(getattr(a, 'say_do_mismatches', {}))


class ScoreboardTests(unittest.TestCase):
    """FIX-B46: the GM's private result notice becomes one fact in my lessons and reaches my reflection."""

    def test_scoreboard_fact(self):
        board = {'players': 4, 'my_rank': 3, 'my_tied': 2, 'my_score': -10, 'rows': [
            {'rank': 1, 'score': 40, 'outcome': 'wrong_accusation_dead', 'missions_voided': False, 'me': False},
            {'rank': 2, 'score': 10, 'outcome': 'wrong_accusation_dead', 'missions_voided': False, 'me': False},
            {'rank': 3, 'score': -10, 'outcome': 'unsolved_survivor', 'missions_voided': True, 'me': True},
            {'rank': 3, 'score': -10, 'outcome': 'unsolved_survivor', 'missions_voided': False, 'me': False}]}
        fact = M['memory'].scoreboard_fact(board)
        self.assertIn('4명 중 공동 3위(2명), -10점', fact)
        solo = M['memory'].scoreboard_fact(dict(board, my_rank=1, my_tied=1, my_score=40))
        self.assertIn('4명 중 1위, 40점', solo)
        everyone = M['memory'].scoreboard_fact({'players': 12, 'my_rank': 1, 'my_tied': 12, 'my_score': -10,
                                                'rows': [{'score': -10, 'outcome': 'unsolved_survivor', 'missions_voided': True}] * 12})
        self.assertIn('12명 중 공동 1위(12명)', everyone)
        self.assertIn('오답 사망 2명 10~40점', fact)
        self.assertIn('미해결 생존 2명 -10점(미션 무효 1명)', fact)
        with tempfile.TemporaryDirectory() as d:
            store = M['memory'].ExperienceStore(None, 'society:sb', None, d)
            store.record('g1', {'resolution': 'deadline'}, {'scoreboard': board})
            self.assertIn(fact, store.load()['lessons'])


class DealingReactionTests(unittest.TestCase):
    """FIX-B47: what happened with others must be reacted to (inferred attitude + my affinity change)."""

    def agent(self, replies):
        a = SayDoTests.agent(SayDoTests(), replies)
        a.profile = dict(a.profile or {}, character_id=0)
        a.agent_of_char.update({3: 'society:three', 7: 'society:seven'})
        return a

    OBS = {'phase': 'movement', 'allowed_actions': ['move', 'stay'], 'cycle_id': 6}

    def test_dealings_collect_and_must_be_reacted_to(self):
        good = {'consideration': '이동', 'action': {'type': 'stay', 'payload': {}},
                'reaction': [{'who': 3, 'their_attitude_to_me': 1, 'delta': 4, 'why': '정중하게 부탁했다'},
                             {'who': 7, 'their_attitude_to_me': -2, 'delta': -8, 'why': '협박했다'}]}
        a = self.agent([{'consideration': '이동', 'action': {'type': 'stay', 'payload': {}}}, good])
        a.hear(3, 'question', '부탁드려요, 장소 단서 하나만 나눠 주실 수 있을까요?')
        a._dealing(7, '나에게 threaten 방식으로 질문')
        a._dealing(9, '나에게 question 방식으로 질문')        # unknown pool agent (NPC): no reaction due
        self.assertEqual(sorted(r['who'] for r in a._dealings_view()), [3, 7])
        self.assertEqual(a.decide(self.OBS), ('stay', {}))   # first answer lacked the reaction -> asked again
        self.assertIn('reaction', next(iter(a.reaction_errors)))
        self.assertEqual(a.peer_reputation['society:three']['their_attitude'], 1)
        self.assertEqual(a.peer_reputation['society:seven']['affinity'], 42.0)
        self.assertEqual(a.new_dealings, [])
        self.assertEqual(a._affinity('society:seven')['their_attitude_to_me'], -2)

    def test_a_zero_change_is_fine_but_needs_a_reason(self):
        a = self.agent([])
        a._dealing(3, '거래(상대 제안) 결과: refused')
        self.assertFalse(a._reaction_ok([{'who': 3, 'their_attitude_to_me': 0, 'delta': 0}])[0])
        self.assertTrue(a._reaction_ok([{'who': 3, 'their_attitude_to_me': 0, 'delta': 0, 'why': '사유가 타당했다'}])[0])
        self.assertFalse(a._reaction_ok([{'who': 3, 'their_attitude_to_me': 5, 'delta': 0, 'why': 'x'}])[0])

    def test_seek_shows_relation_and_last_refusal(self):
        a = self.agent([])
        a.peer_reputation['society:three'] = {'fulfilled': 1, 'broken': 0, 'affinity': 61.0, 'why': ['x'], 'their_attitude': 1}
        a.situation.offered(3, ['weapon'], 4)
        a.situation.trade(3, {'cycle': 5, 'status': 'refused', 'requested': [{'type': 'place'}], 'their_words': '지금은 줄 수 없어요'})
        row = a._situation_block(['weapon'])['seek']['weapon'][0]
        self.assertEqual(row['relation']['score'], 61.0)
        self.assertEqual(row['relation']['their_attitude_to_me'], 1)
        self.assertEqual(row['last_refusal']['their_words'], '지금은 줄 수 없어요')

    def test_attitude_persists_in_my_memory(self):
        with tempfile.TemporaryDirectory() as d:
            m = M['memory'].ExperienceStore(None, 'society:me', None, d)
            m.save_peers({'society:x': {'fulfilled': 0, 'broken': 0, 'affinity': 55.0, 'why': ['a'], 'their_attitude': -1}})
            self.assertEqual(m.load_peers()['society:x']['their_attitude'], -1)


class CaseClaimTests(unittest.TestCase):
    """FIX-B45: words about the case reach the decision prompt with my record of the speaker."""

    def test_claims_about_the_case_reach_the_prompt_with_the_speaker_record(self):
        a = PromptTests.agent(PromptTests())
        a.agent_of_char[3] = 'society:three'
        a.peer_reputation['society:three'] = {'fulfilled': 4, 'broken': 0, 'affinity': 68.0, 'why': ['x']}
        a.situation.trade(3, {'cycle': 5, 'status': 'settled'})
        a.situation.heard(3, 6, 'trade', '흉기 후보 5는 정답이 아니에요')
        a.situation.heard(3, 7, 'question', '단서 좀 주세요')            # not about the case: not listed
        a.situation.heard(9, 8, 'answer', '범행 장소는 금고 칸이에요')    # unknown agent: no record
        rows = a._situation_block(['weapon'])['heard_about_case']
        self.assertEqual([r['from'] for r in rows], [9, 3])
        self.assertNotIn('this_game', rows[1])                          # FIX-B62/B63: no code verdicts
        self.assertEqual(rows[1]['speaker'], {'my_affinity': 68.0})
        self.assertIsNone(rows[0]['speaker']['my_affinity'])
        self.assertTrue(all(r['grade'] == '가능성' for r in rows))


class DeductionMathTests(unittest.TestCase):
    """FIX-B44: the agent infers its own expected scores; only the accuse arithmetic is checked and
    the decision stays the model's own (no stand-in on the last attempt)."""
    OBS = SayDoTests.OBS
    HYP = SayDoTests.HYP

    def agent(self, replies, banked=0):
        a = SayDoTests.agent(SayDoTests(), replies)
        a._accuse_math = lambda: {"combinations_left": 200, "score_if_correct_first": 100 + banked,
                                  "score_if_wrong": -20 + banked, "score_if_unsolved_and_alive": -10,
                                  "score_if_someone_else_wins": banked}
        return a

    def reply(self, math, decision='pass'):
        act = {'type': 'accuse', 'payload': {'suspect': 0, 'weapon': 0, 'place': 0}} if decision == 'accuse' else {'type': 'pass', 'payload': {}}
        return {'consideration': '따져 본다', 'current_hypothesis': self.HYP, 'decision': decision,
                'deduction_math': math, 'action': act}

    def test_no_precomputed_chance_in_the_prompt(self):
        a = PromptTests.agent(PromptTests())
        self.assertNotIn('success_if_random', json.dumps(a._accuse_math() or {}))

    def test_right_arithmetic_passes_wrong_is_asked_again(self):
        good = {'my_win_chance': 0.01, 'expected_if_accuse': -18.8, 'expected_if_pass': -10, 'pass_assumption': '마지막 사이클, 미해결'}
        bad = dict(good, expected_if_accuse=5)
        a = self.agent([self.reply(bad), self.reply(good)])
        self.assertEqual(a.decide(self.OBS), ('pass', {}))
        self.assertEqual(list(a.deduction_math_errors), [next(iter(a.deduction_math_errors))])
        self.assertIn('계산이 맞지 않다', next(iter(a.deduction_math_errors)))

    def test_lower_expected_choice_needs_a_reason_and_then_stands(self):
        math = {'my_win_chance': 0.01, 'expected_if_accuse': -18.8, 'expected_if_pass': -10, 'pass_assumption': '미해결'}
        a = self.agent([self.reply(math, 'accuse'), self.reply(dict(math, choice_reason='성격상 먼저 던진다'), 'accuse')])
        self.assertEqual(a.decide(self.OBS)[0], 'accuse')     # free decision, with a stated reason
        self.assertIn('choice_reason', next(iter(a.deduction_math_errors)))

    def test_missions_make_a_wrong_accusation_worth_it(self):
        # all missions done (60): wrong = +40 > unsolved -10, so accusing is the higher expected score
        math = {'my_win_chance': 0.005, 'expected_if_accuse': 40.6, 'expected_if_pass': -10, 'pass_assumption': '미해결이면 미션 무효'}
        a = self.agent([self.reply(math, 'accuse')], banked=60)
        self.assertEqual(a.decide(self.OBS)[0], 'accuse')
        self.assertFalse(getattr(a, 'deduction_math_errors', {}))

    def test_after_the_last_attempt_the_models_decision_stands(self):
        bad = {'my_win_chance': 2, 'expected_if_accuse': 0, 'expected_if_pass': 0, 'pass_assumption': 'x'}
        a = self.agent([self.reply(bad), self.reply(bad)])
        self.assertEqual(a.decide(self.OBS), ('pass', {}))
        self.assertEqual(sum(a.deduction_math_errors.values()), 2)
        self.assertFalse(getattr(a, 'fallbacks', 0))


if __name__ == '__main__':
    unittest.main()
