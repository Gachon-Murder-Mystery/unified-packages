"""FIX-B14: B repairs an answer to the engine's success-tier rule before sending it.

disclosure_ok() mirrors competitive.Rules.validate_response; conform_statements() must
always produce something that rule accepts, and keep the model's choice when it can.
Checked against the real engine validator on random option sets and model answers.
"""
import importlib.util
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Game-engine'))
import competitive  # noqa: E402

sys.path.insert(0, str(ROOT / 'Agent_Reasoning'))
spec = importlib.util.spec_from_file_location('_b_adapter_conform', ROOT / 'Agent_Reasoning' / 'adapter.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def case(rng):
    n_def, n_amb, n_ind = rng.randint(0, 3), rng.randint(0, 3), rng.randint(0, 3)
    info = {f'd{i}': {'id': f'd{i}', 'kind': 'definite', 'category': 'suspect', 'candidate': i} for i in range(n_def)}
    info.update({f'n{i}': {'id': f'n{i}', 'kind': 'narrated', 'text': f't{i}', 'narrations': [f't{i}', f'u{i}']}
                 for i in range(n_ind)})
    options = {'definite': [f'd{i}' for i in range(n_def)],
               'ambiguous': [{'kind': 'ambiguous', 'pair': {'suspect': i, c: 3}, 'text': f'x{i}{c}'}   # FIX-B23 pairs
                             for i, c in zip(range(n_amb), ('weapon', 'place', 'weapon'))],
               'indirect': [{'kind': 'indirect', 'clue_id': f'n{i}', 'narrative_options': [f't{i}', f'u{i}']}
                            for i in range(n_ind)]}
    question = {'successes': rng.randint(1, 3), 'response_options': options, 'target_id': 1}
    return question, {'characters': {'1': {'information': info}}}


def model_answer(rng, options):
    pool = ([{'kind': 'definite', 'clue_id': c} for c in options['definite']]
            + [{'kind': 'ambiguous', 'pair': dict(x['pair'])} for x in options['ambiguous']]
            + [{'kind': 'indirect', 'clue_id': x['clue_id'], 'text': x['narrative_options'][1]} for x in options['indirect']])
    picked = [rng.choice(pool) for _ in range(rng.randint(0, 3))] if pool else []
    return picked  # may mix forms and repeat, as the model did


def engine_accepts(g, question, statements):
    rules = object.__new__(competitive.Rules)
    rules.options = lambda _g, _q: question['response_options']
    sent = [dict(s, text=next(x['text'] for x in question['response_options']['ambiguous']
                              if x['pair'] == s['pair'])) if s['kind'] == 'ambiguous' else s
            for s in statements]
    try:
        rules.validate_response(g, question, sent)
        return True
    except competitive.ContractError:
        return False


class ConformanceTests(unittest.TestCase):
    def test_mirror_and_repair_agree_with_the_engine(self):
        rng = random.Random(14)
        for _ in range(3000):
            question, g = case(rng)
            proposed = adapter.legal_statements(model_answer(rng, question['response_options']), question)
            self.assertEqual(adapter.disclosure_ok(proposed, question), engine_accepts(g, question, proposed),
                             (question, proposed))
            repaired = adapter.conform_statements(proposed, question)
            self.assertTrue(engine_accepts(g, question, repaired), (question, proposed, repaired))
            if adapter.disclosure_ok(proposed, question):
                self.assertEqual(repaired, proposed)  # a valid answer is never touched

    def test_mixed_answer_at_two_successes_keeps_the_offered_definite(self):
        rng = random.Random(1)
        question, _ = case(rng)
        question['successes'] = 2
        question['response_options']['definite'] = ['d0', 'd1']
        question['response_options']['ambiguous'] = [{'kind': 'ambiguous', 'pair': {'suspect': 0, 'weapon': 9}, 'text': 'x'}]
        mixed = [{'kind': 'definite', 'clue_id': 'd1'}, {'kind': 'ambiguous', 'pair': {'suspect': 0, 'weapon': 9}}]
        self.assertEqual(adapter.conform_statements(mixed, question), [{'kind': 'definite', 'clue_id': 'd1'}])


if __name__ == '__main__':
    unittest.main()
