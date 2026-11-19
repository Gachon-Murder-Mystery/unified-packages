"""FIX-B16 (engine-based prompt) and FIX-B17 (trades wired into B).

The prompt states engine numbers in prose, so each one is pinned to the engine source:
if the engine changes, these tests fail and the prompt has to be revisited.
"""
import importlib.util
import json
import random
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Game-engine'))
import competitive  # noqa: E402

sys.path.insert(0, str(ROOT / 'Agent_Reasoning'))
from test_agent_experience import M, POOLS  # noqa: E402

ENGINE = (ROOT / 'Game-engine' / 'competitive.py').read_text(encoding='utf-8')
PROMPT = M['agent'].RULES_PROMPT


class PromptMatchesEngine(unittest.TestCase):
    def test_scores(self):
        self.assertEqual(competitive.WIN_SCORES, [100, 70, 50, 30])
        self.assertIn('100/70/50/30', PROMPT.RULES)
        self.assertEqual(competitive.WRONG_ACCUSE_SCORE, -20)
        self.assertIn('오답 사망은 −20', PROMPT.RULES)
        self.assertEqual(competitive.REWARDS, {'하': 10, '중': 20, '상': 30})
        self.assertIn('하 10, 중 20, 상 30', PROMPT.RULES)
        self.assertEqual(competitive.RULES['unsolved_score'], -10)   # FIX-A24
        self.assertIn('생존자 전원 −10점', PROMPT.RULES)

    def test_relationship_numbers(self):
        for code in ("self.relation(g, target_id, cid, -5, 'failed_question')",
                     "gain = MBTI_RULES['question_gain'].get(c.get('mbti', ' ')[0], 5) if g['rules'].get('mbti_rules', False) else 5",
                     "self.relation(g, target_id, cid, gain, 'successful_question')",
                     "self.relation(g, target_id, cid, -5, 'interrogation')",
                     "self.relation(g, target_id, cid, -10, 'threat')",
                     "10 if fulfilled else -20",
                     "amount*(1.5 if c['mbti'][0] == 'E' else .5)",
                     "threshold += -1 if relation >= 70 else 1 if relation <= 30 else 0"):
            self.assertIn(code, ENGINE)
        for text in ('관계 +5, 실패하면 −5', '관계 −5', '관계 −10', '+10', '−20', 'E면 1.5배, I면 0.5배',
                     '70 이상이면', '30 이하면 +1'):
            self.assertIn(text, PROMPT.RULES)

    def test_dice_and_explore_yield(self):
        self.assertIn("randint(1, 6) for i in range(3 if prepared else 2)", ENGINE)
        self.assertIn("count = 2 if roll['successes'] == 3 else int(roll['successes'] > 0)", ENGINE)
        self.assertIn("return min(6, max(2, threshold))", ENGINE)
        self.assertIn('성공 0→없음, 1~2→1개, 3→2개', PROMPT.RULES)
        self.assertIn('8 − 스탯(2~6으로 제한)', PROMPT.RULES)

    def test_failure_effects_are_described_as_the_engine_applies_them(self):
        self.assertIn("if blocked: roll['successes'] = 0", ENGINE)        # asking is allowed, it just fails
        self.assertIn('성공 0으로 처리', PROMPT.RULES)
        self.assertIn("possession(info)", ENGINE)                        # threat exposes ID + category only
        self.assertIn('ID와 분류, 내용 아님', PROMPT.RULES)
        self.assertNotIn('hypothesis에 적은 세 후보에 대해서만', PROMPT.RULES)   # the old false rule
        self.assertNotIn('교섭 제안을 겸한다', PROMPT.RULES)

    def test_room_and_trade_rules(self):
        self.assertEqual(competitive.RULES['room_clues'], 'first_come')
        self.assertIn('선착순', PROMPT.RULES)
        self.assertIn("require(aid not in g['spent'] and not self.replacement(g, aid), 'conflict', 'one action per phase')", ENGINE)
        self.assertIn('교섭 제안은 탐문·탐색 단계의 행동이다', PROMPT.RULES)
        # FIX-A25: declared types, possession check, exchange at once, honesty from what was handed over
        self.assertIn("TRADE_TYPES = ('suspect', 'weapon', 'place', 'character_room', 'weapon_room', 'companion')", ENGINE)
        for code in ("'unauthorized', 'you must hold a clue of every declared type'",
                     "'unauthorized', 'you must hold clues matching every requested item'",
                     "self.relation(g, self.party(g, other), self.party(g, x), gain if honest[x] else loss,",
                     "gain = MBTI_RULES['trade_gain'].get('J' if 'J' in xm else 'P' if 'P' in xm else '', 10)",
                     "loss = MBTI_RULES['J_broken'] if 'J' in xm else -20"):
            self.assertIn(code, ENGINE)
        for text in ('유형으로 제안한다', '엔진은 보유만 확인한다', '수락하면 그 즉시', '반드시 성립한다',
                     '정직한 쪽은 변화 없음', '엔진은 누가 정직했는지 알려 주지 않는다', '평판이나 지난 거래 기록을 보여 주지 않는다'):
            self.assertIn(text, PROMPT.RULES)
        self.assertNotIn('after_partner', PROMPT.RULES + PROMPT.TRADE_SYSTEM + PROMPT.DECISION_FORMAT)
        self.assertIn('TALK_LIMIT = 200', ENGINE)
        self.assertIn('200자 이내', PROMPT.RULES)
        self.assertIn("TRADE_REPLACEMENT = ['explore', 'question', 'prepare', 'pass']", ENGINE)
        self.assertIn("self.grant_replacement(g, q['asker_id'], ['explore']", ENGINE)
        self.assertIn('대체 탐색 1회', PROMPT.RULES)
        self.assertIn("1-((x['threshold']-1)/6)**x['dice']", ENGINE)
        self.assertIn('탐색 성공 확률', PROMPT.RULES)


class PersonalityIsNotAnAbility(unittest.TestCase):  # FIX-A30/B34 (user 2026-11-08)
    def test_engine_corrections_are_off_by_default(self):
        self.assertIs(competitive.RULES['mbti_rules'], False)
        self.assertNotIn("get('mbti_rules', True)", ENGINE)

    def test_prompt_states_abilities_come_from_the_job(self):
        self.assertIn('성격(MBTI)은 판정·점수·관계 수치에 영향을 주지 않는다. 능력은 직업(스탯·스킬)이 정한다', PROMPT.RULES)
        for text in ('E +8', '절반 확률', '성격 보정', 'mbti_rules', '역제안이 성사될 때까지 행동을 쓰지 않'):
            self.assertNotIn(text, PROMPT.RULES + PROMPT.TRADE_SYSTEM + PROMPT.ANSWER_SYSTEM)
        for text in M['agent'].MBTI_TENDENCY.values():
            self.assertNotRegex(text, '기준선|판정|점수|[+−-][0-9]')

    def test_no_shared_accusation_prescription(self):
        self.assertNotIn('고발하는 편이 낫다', PROMPT.RULES)
        self.assertIn('경향과 반대로 갈 때는 consideration에 그 이유', PROMPT.RULES)


class RenamedApproachAndPairStatements(unittest.TestCase):
    def test_persuade_replaces_the_negotiate_approach(self):
        self.assertIn("require(approach in ('question', 'interrogate', 'threaten', 'persuade')", ENGINE)
        self.assertIn('persuade(설득)', PROMPT.RULES)
        self.assertNotIn('"negotiate"', PROMPT.RULES + PROMPT.DECISION_FORMAT)

    def test_ambiguous_is_a_pair_exclusion(self):
        self.assertIn("ambiguous.append({'kind': 'ambiguous', 'pair': dict(key),", ENGINE)
        self.assertIn('조합 배제', PROMPT.RULES)
        self.assertNotIn('에 관한 것이 아니다', PROMPT.RULES + PROMPT.ANSWER_SYSTEM)


class TradeMatchingParity(unittest.TestCase):
    def test_b_mirror_equals_engine(self):  # FIX-B29: declared types with exclusions
        rng = random.Random(17)
        types = list(competitive.TRADE_TYPES)
        for _ in range(4000):
            kind = rng.choice(['definite', 'indirect', 'narrated'])
            info = {'kind': kind, 'id': 'x'}
            if kind == 'definite':
                info.update(category=rng.choice(['suspect', 'weapon', 'place']), candidate=rng.randrange(8))
            elif kind == 'indirect':
                sub = rng.choice(['character_room', 'weapon_room', 'companion'])
                info.update(type=sub, room=rng.randrange(8))
                if sub == 'character_room': info.update(character=rng.randrange(12))
                if sub == 'weapon_room': info.update(weapon=rng.randrange(8))
                if sub == 'companion': info.update(character=rng.randrange(12), other=rng.randrange(12))
            kind_t = rng.choice(types)
            item = {'type': kind_t, 'exclude': rng.sample(range(competitive.TYPE_RANGE[kind_t]), rng.randrange(3))}
            self.assertEqual(M['agent'].LLMAgent.spec_matches(info, item), competitive.Rules.type_matches(info, item), (info, item))
        self.assertEqual(M['agent'].TRADE_TYPES, competitive.TRADE_TYPES)
        self.assertEqual(M['agent'].TYPE_RANGE, competitive.TYPE_RANGE)


class Agent:
    @staticmethod
    def make(replies=None):
        a = M['agent'].LLMAgent('society:me', log_path=Path(tempfile.mkdtemp()) / 'l.jsonl')
        a.sync({'self': {'character_id': 0, 'mbti': 'INTP', 'mode': 'active'}, 'candidates': POOLS,
                'cycle_id': 4, 'phase': 'interaction',
                'information': [{'id': 'c1', 'kind': 'definite', 'category': 'weapon', 'candidate': 3},
                                {'id': 'n1', 'kind': 'narrated', 'text': '누군가'}]})
        if replies is not None:
            a.client = object()
            it = iter(replies)
            a._request_complete = lambda *_, **__: (json.dumps(next(it), ensure_ascii=False), None)
            a._count = lambda usage: None
        return a


OBS = {'phase': 'interaction', 'occupants': [{'id': 5, 'mode': 'active'}, {'id': 6, 'mode': 'corpse'}],
       'self': {'character_id': 0, 'mbti': 'INTP'}}


class ProposalTests(unittest.TestCase):
    def test_valid_proposal_becomes_an_engine_body(self):
        body = Agent.make()._trade_proposal({'target_id': 5, 'offer_types': ['weapon'], 'offer_ids': ['c1'],
                                             'requested': [{'type': 'place', 'exclude': [2, 0]}],
                                             'message': '장소 정보가 필요해요'}, OBS)
        self.assertEqual(body, {'target_id': 5, 'offer_types': ['weapon'], 'offer_ids': ['c1'],
                                'requested': [{'type': 'place', 'exclude': [0, 2]}], 'message': '장소 정보가 필요해요'})
        bundle = Agent.make()._trade_proposal({'target_id': 5, 'offer_types': ['weapon'], 'offer_ids': ['c1'],
                                               'requested': ['character_room'] * 3}, OBS)
        self.assertEqual(len(bundle['requested']), 3)   # unequal bundle

    def test_bad_proposals_are_dropped_with_a_reason(self):
        a = Agent.make()
        base_ok = {'target_id': 5, 'offer_types': ['weapon'], 'offer_ids': ['c1'], 'requested': [{'type': 'place'}]}
        for change in ({'target_id': 6},                              # corpse
                       {'offer_types': ['place']},                    # declares a type I do not hold
                       {'offer_ids': ['n1']},                         # narrated: not owned in full
                       {'offer_ids': []},                             # count differs from the declaration
                       {'requested': ['indirect']},                   # old broad type
                       {'requested': [{'type': 'place', 'exclude': [9]}]},   # out of range
                       {'fulfill': 'now'},                            # gone
                       {'message': '가' * 201}):
            trade = dict(base_ok, **change)
            self.assertIsNone(a._trade_proposal(trade, OBS), trade)
            self.assertTrue(a._trade_reason)
        # deception is allowed: declare weapon, seal any clue I hold in full
        a.known_info.append({'id': 'i1', 'kind': 'indirect', 'type': 'companion', 'character': 1, 'other': 2})
        self.assertIsNotNone(a._trade_proposal(dict(base_ok, offer_ids=['i1']), OBS))


class DecisionOrderAndRetry(unittest.TestCase):  # FIX-B36 / A32
    OBS = {'phase': 'interaction', 'cycle_id': 4, 'allowed_actions': ['explore', 'question', 'pass'],
           'self': {'character_id': 0, 'mbti': 'INTP', 'room': 3, 'mode': 'active'},
           'occupants': [{'id': 5, 'mode': 'active', 'name': 'x'}],
           'thresholds': {'explore': 4, 'by_target': {'5': {'question': 3}}},
           'action_request': {'issued_by': 'gm', 'phase_epoch': 9,
                              'options': {'explore': {}, 'question': {'targets': [{'id': 5}]}, 'pass': {}}}}

    def test_now_comes_first_with_the_gm_request(self):
        a = Agent.make()
        a.action_log = [{'action': 'question', 'target': 7, 'cycle': 3}]
        u = json.loads(a._user_prompt(dict(self.OBS)))
        self.assertEqual(next(iter(u)), 'now')
        self.assertEqual(u['now']['valid_targets'], [5])
        self.assertEqual(u['now']['gm_request']['phase_epoch'], 9)
        self.assertFalse(u['recent_actions'][-1]['target_here_now'])

    def test_a_rejected_output_is_explained_on_the_retry(self):
        bad = {'consideration': 'x', 'action': {'type': 'question', 'payload': {
            'target_id': 7, 'approach': 'question', 'hypothesis': {'suspect': 0, 'weapon': 0, 'place': 0}}}}
        good = {'consideration': 'y', 'action': {'type': 'explore', 'payload': {}}}
        a = Agent.make([bad, good])
        prompts = []
        replies = iter([bad, good])
        def fake(system, user, budget):
            prompts.append(json.loads(user)); return json.dumps(next(replies), ensure_ascii=False), None
        a._request_complete = fake
        a.candidates = POOLS
        action, _ = a.decide(dict(self.OBS))
        self.assertEqual(action, 'explore')
        self.assertNotIn('previous_output_rejected', prompts[0]['now'])
        self.assertIn('지금 같은 칸', prompts[1]['now']['previous_output_rejected']['reason'])


class BodyAcceptedByEngine(unittest.TestCase):
    def test_proposal_and_counter_bodies_parse_in_the_engine(self):  # the A25 offline run caught a mismatch
        rules = object.__new__(competitive.Rules)
        body = Agent.make()._trade_proposal({'target_id': 5, 'offer_types': ['weapon'], 'offer_ids': ['c1'],
                                             'requested': [{'type': 'place', 'exclude': [1]}]}, OBS)
        rules.type_items(body['offer_types'], declared=True)
        rules.type_items(body['requested'])
        out = Agent.make([{'decision': 'counter', 'offer_ids': ['c1'], 'offer_types': ['weapon'],
                           'requested': [{'type': 'suspect', 'exclude': []}]}]).decide_trade(ResponseTests.TRADE, OBS)
        rules.type_items(out['offer_types'], declared=True)
        rules.type_items(out['requested'])


class ResponseTests(unittest.TestCase):
    TRADE = {'trade_id': 't1', 'proposer': 'society:x', 'proposer_character': 5,
             'offer_types': [{'type': 'place', 'exclude': []}], 'requested': [{'type': 'weapon', 'exclude': []}],
             'messages': [{'from_character': 5, 'stage': 'propose', 'text': '무기 하나만'}]}

    def test_accept_with_a_matching_clue(self):
        a = Agent.make([{'decision': 'accept', 'offer_ids': ['c1'], 'message': '좋아요'}])
        out = a.decide_trade(self.TRADE, OBS)
        self.assertEqual(out, {'decision': 'accept', 'offer_ids': ['c1'], 'message': '좋아요'})
        self.assertEqual(a.heard_claims[-1]['text'], '무기 하나만')     # FIX-B31 talk reached my claims

    def test_cannot_accept_without_holding_what_was_asked(self):
        trade = dict(self.TRADE, requested=[{'type': 'weapon', 'exclude': [3]}])   # my only weapon clue is excluded
        a = Agent.make([{'decision': 'accept', 'offer_ids': ['c1']}] * 2)
        self.assertEqual(a.decide_trade(trade, OBS), {'decision': 'refuse'})

    def test_non_matching_offer_is_retried_then_falls_back(self):
        bad = {'decision': 'accept', 'offer_ids': ['n1']}
        a = Agent.make([bad, bad])
        out = a.decide_trade(self.TRADE, OBS)
        self.assertIn(out['decision'], ('accept', 'counter', 'refuse'))
        self.assertEqual(a.model_fallbacks.get('trade_response'), 1)


class ReputationInPrompt(unittest.TestCase):
    def test_occupants_show_only_my_own_memory(self):  # FIX-B30
        a = Agent.make()
        a.peer_reputation = {'society:abcdef123456': {'fulfilled': 0, 'broken': 2}}
        seen = a._occupant({'id': 5, 'mode': 'active', 'agent_id': 'society:abcdef123456', 'my_agent_relationship': 72.5})
        self.assertEqual((seen['agent_tag'], seen['my_history_with_them']), ('123456', {'fulfilled': 0, 'broken': 2}))
        self.assertEqual(seen['my_agent_relationship'], 72.5)   # FIX-B33: the engine's carried-over value reaches the prompt
        self.assertNotIn('reputation', seen)
        self.assertNotIn('agent_tag', a._occupant({'id': 6, 'mode': 'corpse'}))

    def test_i_judge_a_settled_trade_myself(self):  # FIX-B30
        a = Agent.make()
        a.known_info.append({'id': 'w9', 'kind': 'indirect', 'type': 'companion', 'character': 1, 'other': 2})
        view = {'proposer': 'society:me', 'recipient': 'society:x', 'proposer_character': 0, 'recipient_character': 5,
                'status': 'settled', 'cycle': 4, 'offer_types': [{'type': 'weapon', 'exclude': []}],
                'requested': [{'type': 'place', 'exclude': []}], 'received_ids': ['w9'], 'gave_ids': ['c1']}
        a.record_trade_result(view, 'society:me')        # asked for a place clue, got a companion clue
        self.assertEqual(a.peer_reputation['society:x'], {'fulfilled': 0, 'broken': 1})
        self.assertEqual((a.gave_to['society:x'], a.got_from['society:x']), (['c1'], ['w9']))
        self.assertEqual(a.provenance['w9']['via'], 'trade')

    def test_peer_memory_persists_in_the_agents_own_file(self):  # FIX-B30
        store = M['memory'].ExperienceStore(None, 'society:me', directory=tempfile.mkdtemp())
        store.save_peers({'society:x': {'fulfilled': 2, 'broken': 1}})
        self.assertEqual(store.load_peers(), {'society:x': {'fulfilled': 2, 'broken': 1}})


class RuntimeTradeFlow(unittest.TestCase):
    def runtime(self, inbox):
        spec = importlib.util.spec_from_file_location('_b_adapter_trades', ROOT / 'Agent_Reasoning' / 'adapter.py')
        sys.modules['agent'] = M['agent']
        try:
            adapter = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(adapter)
        finally:
            sys.modules.pop('agent', None)
        calls = []

        class Role:
            def bind(self, **_): return self
            def call(self, route, **d):
                calls.append((route, d))
                return {'trades': inbox} if route == 'trades/inbox' else {}
        agent = Agent.make([{'decision': 'accept', 'offer_ids': ['c1']}])
        rt = adapter.LLMPlayerRuntime(Role(), {'agent_id': 'society:me'}, {}, agent=agent, memory_dir=tempfile.mkdtemp())
        return rt, calls

    @staticmethod
    def trade(status, partner_done=False):
        return {'trade_id': 't1', 'proposer': 'society:x', 'recipient': 'society:me', 'status': status,
                'proposer_character': 5, 'recipient_character': 0, 'offer_types': [{'type': 'place', 'exclude': []}],
                'requested': [{'type': 'weapon', 'exclude': []}], 'messages': []}

    def test_accepting_seals_my_clues_and_spends_the_action(self):
        obs = dict(OBS, phase_epoch=7, self={'id': 'society:me', 'character_id': 0, 'mbti': 'INTP'})
        rt, calls = self.runtime([self.trade('proposed')])
        rt._trades(obs)
        respond = [d for r, d in calls if r == 'trades/respond']
        self.assertEqual((respond[0]['decision'], respond[0]['offer_ids']), ('accept', ['c1']))
        self.assertNotIn('fulfill', respond[0])
        self.assertNotIn('trades/transfer', [r for r, _ in calls])     # the engine settles both sides
        self.assertEqual(rt._paid_epoch, 7)
        rt._trades(obs)                                                  # never answers the same proposal twice
        self.assertEqual(len([r for r, _ in calls if r == 'trades/respond']), 1)

    def test_a_second_proposal_is_refused_once_the_action_is_spent(self):
        obs = dict(OBS, phase_epoch=7, self={'id': 'society:me', 'character_id': 0, 'mbti': 'INTP'})
        second = dict(self.trade('proposed'), trade_id='t2')
        rt, calls = self.runtime([second])
        rt._paid_epoch = 7
        rt._trades(obs)
        respond = [d for r, d in calls if r == 'trades/respond']
        self.assertEqual(respond[0]['decision'], 'refuse')
        self.assertEqual(rt.trade_activity['refuse_no_action_left'], 1)

    def test_a_counter_to_my_own_proposal_can_still_be_answered(self):
        obs = dict(OBS, phase_epoch=7, self={'id': 'society:me', 'character_id': 0, 'mbti': 'INTP'})
        countered = self.trade('proposed')                      # after a counter I am the recipient again
        rt, calls = self.runtime([countered])
        rt._paid_epoch, rt._my_decision['t1'] = 7, 'now'
        rt._trades(obs)
        self.assertEqual([d for r, d in calls if r == 'trades/respond'][0]['decision'], 'accept')

    def test_a_second_counter_from_the_same_proposer_is_answered(self):  # FIX-B27
        """x proposes, I counter, x counters back: x is the proposer again, but this is a new round
        (the engine reset the deadline) and I must answer it, not leave it to the error timeout."""
        obs = dict(OBS, phase_epoch=7, self={'id': 'society:me', 'character_id': 0, 'mbti': 'INTP'})
        inbox = [dict(self.trade('proposed'), deadline=100.0)]
        rt, calls = self.runtime(inbox)
        rt.agent._request_complete = lambda *_, **__: (json.dumps({'decision': 'accept', 'offer_ids': ['c1']}), None)
        rt._trades(obs)
        inbox[0] = dict(self.trade('proposed'), deadline=160.0)   # countered back to me
        rt._paid_epoch, rt._my_decision['t1'] = 7, 'now'
        rt._trades(obs)
        self.assertEqual(len([r for r, _ in calls if r == 'trades/respond']), 2)
        rt._trades(obs)                                            # still once per round
        self.assertEqual(len([r for r, _ in calls if r == 'trades/respond']), 2)


if __name__ == '__main__':
    unittest.main()
