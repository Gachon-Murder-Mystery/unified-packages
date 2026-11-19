"""Engine infer() and B's BeliefState must eliminate exactly the same candidates.

H2 counts a belief that eliminates more than the engine can prove as a hallucination,
so the two closures must stay rule-for-rule identical (FIX-A5 added reverse inference
to both). Random clue subsets from real cases, compared candidate by candidate.
"""
import importlib.util
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Game-engine'))
import competitive_rules  # noqa: E402

spec = importlib.util.spec_from_file_location('_b_schema', ROOT / 'Agent_Reasoning' / 'schema.py')
schema = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schema)
POOLS = {'suspect': list(range(12)), 'weapon': list(range(8)), 'place': list(range(8))}


class ParityTests(unittest.TestCase):
    def test_random_subsets_match(self):
        rng = random.Random(30)
        reverse_fired = 0
        for seed in range(1, 41):
            _, _, clues = competitive_rules.build_case(seed)
            for size in (5, 10, 20, 30, 40, 50):
                held = rng.sample(clues, min(size, len(clues)))
                # FIX-A18/B23: answers also carry pair exclusions ("X together with W is not it")
                for n in range(rng.randint(0, 6)):
                    c1, c2 = rng.sample(['suspect', 'weapon', 'place'], 2)
                    held.append({'id': f'pair:{seed}:{size}:{n}', 'kind': 'ambiguous',
                                 'pair': {c1: rng.randrange(len(POOLS[c1])), c2: rng.randrange(len(POOLS[c2]))}})
                engine = {c: {int(k) for k in v} for c, v in competitive_rules.infer(held).items()}
                belief = schema.BeliefState(POOLS)
                for clue in held:
                    belief.apply(clue)
                mine = {c: set(belief.eliminated[c]) for c in POOLS}
                self.assertEqual(engine, mine, f'seed {seed}, {size} clues')
                definite_places = {c['candidate'] for c in held if c['kind'] == 'definite' and c['category'] == 'place'}
                reverse_fired += len(mine['place']) > len(definite_places)
        self.assertGreater(reverse_fired, 0, 'reverse inference never fired; test would not cover it')

    def test_pair_exclusion_bites_once_the_other_side_is_settled(self):
        # weapons 0..6 excluded -> weapon is 7; "suspect 4 with weapon 7 is not it" -> suspect 4 is out
        held = [{'id': f'w{i}', 'kind': 'definite', 'category': 'weapon', 'candidate': i} for i in range(7)]
        held.append({'id': 'pair:q:0', 'kind': 'ambiguous', 'pair': {'suspect': 4, 'weapon': 7}})
        self.assertIn('4', competitive_rules.infer(held)['suspect'])
        belief = schema.BeliefState(POOLS)
        for item in held:
            belief.apply(item)
        self.assertNotIn(4, belief.remaining('suspect'))
        # before the weapon is settled the pair only removes triples
        early = schema.BeliefState(POOLS)
        early.apply({'id': 'pair:q:1', 'kind': 'ambiguous', 'pair': {'suspect': 4, 'weapon': 7}})
        self.assertEqual(early.combinations(), 12 * 8 * 8 - 8)
        self.assertEqual(early.summary()['pair_exclusions'], [{'suspect': 4, 'weapon': 7}])

    def test_reverse_inference_narrows_the_scene(self):
        # suspects 0..10 excluded, suspect 11 open and seen in room 2 -> scene must be 2
        held = [{'id': f'd{i}', 'kind': 'definite', 'category': 'suspect', 'candidate': i} for i in range(11)]
        held.append({'id': 'r', 'kind': 'indirect', 'type': 'character_room', 'character': 11, 'room': 2})
        self.assertEqual(sorted(int(k) for k in competitive_rules.infer(held)['place']), [0, 1, 3, 4, 5, 6, 7])
        # two open suspects in rooms 2 and 5 -> scene is 2 or 5
        held = [{'id': f'd{i}', 'kind': 'definite', 'category': 'suspect', 'candidate': i} for i in range(10)]
        held += [{'id': 'a', 'kind': 'indirect', 'type': 'character_room', 'character': 10, 'room': 2},
                 {'id': 'b', 'kind': 'indirect', 'type': 'character_room', 'character': 11, 'room': 5}]
        self.assertEqual(sorted(int(k) for k in competitive_rules.infer(held)['place']), [0, 1, 3, 4, 6, 7])


if __name__ == '__main__':
    unittest.main()
