"""FIX-B51 (own memory saved regardless of the GM report) and FIX-B55 (A40 standings + post-game sharing)."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Agent_Reasoning'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapter import LLMPlayerRuntime          # noqa: E402
from test_agent_experience import M, SayDoTests   # noqa: E402

STANDINGS = [{'agent_id': 'society:aaaaaa', 'cumulative_score': 120, 'games': 3},
             {'agent_id': 'society:three3', 'cumulative_score': 40, 'games': 3},
             {'agent_id': 'society:seven7', 'cumulative_score': 0, 'games': 3}]


class Saver:
    def __init__(self):
        self.saved = []
    def save_peers(self, peers):
        self.saved.append({k: dict(v) for k, v in peers.items()})


class OwnMemoryTests(unittest.TestCase):
    def runtime(self, role_fails):
        r = object.__new__(LLMPlayerRuntime)
        r.memory = Saver()
        r.agent = SimpleNamespace(affinity_dirty=True, peer_reputation={'society:x': {'affinity': 61.0}},
                                  affinity_report=lambda: {'society:x': 61.0})
        r._reported_relations = None
        def call(route, **kw):
            if role_fails:
                raise RuntimeError('409 inactive')
            return {}
        r.role = SimpleNamespace(call=call)
        return r

    def test_my_memory_is_saved_even_when_the_report_fails(self):   # B51
        r = self.runtime(role_fails=True)
        r._report_relations({})
        self.assertEqual(len(r.memory.saved), 1)
        self.assertFalse(r.agent.affinity_dirty)

    def test_a_change_without_a_new_score_is_saved_too(self):   # B51: reasons / their_attitude only
        r = self.runtime(role_fails=False)
        r._reported_relations = {'society:x': 61.0}   # the score did not change: no report
        r._report_relations({})
        self.assertEqual(len(r.memory.saved), 1)

    def test_gifts_persist_in_my_memory(self):   # B55
        with tempfile.TemporaryDirectory() as d:
            m = M['memory'].ExperienceStore(None, 'society:me', None, d)
            m.save_peers({'society:x': {'fulfilled': 1, 'broken': 0, 'gifts_received': 30, 'gifts_given': 0}})
            self.assertEqual(m.load_peers()['society:x'], {'fulfilled': 1, 'broken': 0, 'gifts_received': 30})


class ShareDecisionTests(unittest.TestCase):
    def agent(self, replies):
        a = SayDoTests.agent(SayDoTests(), replies)
        a.agent_id = 'society:aaaaaa'
        a.profile = dict(a.profile or {}, character_id=0)
        a.agent_of_char.update({3: 'society:three3', 7: 'society:seven7'})
        return a

    def test_a_valid_share_is_returned_with_full_agent_ids(self):
        a = self.agent([{'consideration': '도와준 사람에게', 'shares': [{'agent': 'three3', 'points': 30, 'why': '정직한 거래'}]}])
        out = a.decide_share({'final_score': 110, 'rank': 1}, STANDINGS)
        self.assertEqual(out['shares'], [{'agent_id': 'society:three3', 'points': 30}])

    def test_giving_nothing_is_fine(self):
        a = self.agent([{'consideration': '내 점수를 지킨다', 'shares': []}])
        self.assertEqual(a.decide_share({'final_score': 110, 'rank': 1}, STANDINGS)['shares'], [])

    def test_over_the_budget_or_to_myself_is_asked_again_then_nothing(self):
        bad = {'consideration': 'x', 'shares': [{'agent': 'three3', 'points': 200}]}
        mine = {'consideration': 'x', 'shares': [{'agent': 'aaaaaa', 'points': 5}]}
        self.assertEqual(self.agent([bad, mine]).decide_share({'final_score': 110, 'rank': 1}, STANDINGS)['shares'], [])
        good = {'consideration': 'x', 'shares': [{'agent': 'seven7', 'points': 10}]}
        self.assertEqual(self.agent([bad, good]).decide_share({'final_score': 110, 'rank': 1}, STANDINGS)['shares'],
                         [{'agent_id': 'society:seven7', 'points': 10}])

    def test_standings_and_gifts_reach_the_prompt(self):
        a = self.agent([])
        rows = a._standings({'self': {'standings': STANDINGS}})
        self.assertEqual(rows[0], {'agent_tag': 'aaaaaa', 'score': 120, 'games': 3, 'me': True})
        a.peer_reputation['society:three3'] = {'fulfilled': 0, 'broken': 0, 'gifts_received': 30}
        self.assertEqual(a._affinity('society:three3')['post_game_shares'], {'gifts_received': 30})


class ShareHookTests(unittest.TestCase):
    """B55: the runner's post-game hook asks only a first-ranked player, then posts its decision."""
    def runtime(self, rank):
        calls = []
        r = object.__new__(LLMPlayerRuntime)
        r.session = {'game_id': 'g1', 'agent_id': 'society:aaaaaa'}
        r.memory = Saver()
        def call(route, **kw):
            calls.append((route, kw))
            if route == 'pool/feedback':
                return {'feedback': {'society:aaaaaa': {'rank': rank, 'final_score': 110}}}
            if route == 'pool/standings':
                return {'standings': STANDINGS}
            if route == 'pool/share':
                return {'shared': 30, 'shares': kw['shares']}
            raise AssertionError(route)
        r.pool = SimpleNamespace(call=call)
        r.agent = SimpleNamespace(peer_reputation={}, decide_share=lambda fb, st: {
            'shares': [{'agent_id': 'society:three3', 'points': 30}], 'consideration': 'x'})
        return r, calls

    def test_only_the_first_ranked_shares_and_remembers_it(self):
        r, calls = self.runtime(rank=1)
        r.share_after_game({})
        self.assertEqual(calls[-1], ('pool/share', {'game_id': 'g1', 'shares': [{'agent_id': 'society:three3', 'points': 30}]}))
        self.assertEqual(r.agent.peer_reputation['society:three3']['gifts_given'], 30)
        r2, calls2 = self.runtime(rank=2)
        self.assertIsNone(r2.share_after_game({}))
        self.assertEqual([c[0] for c in calls2], ['pool/feedback'])


if __name__ == '__main__':
    unittest.main()
