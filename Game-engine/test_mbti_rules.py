"""FIX-A51 (user decision 2026-11-12): no stat bonus by personality type. The FIX-A29 corrections table and its
switches were removed; the old tests of those corrections went with them."""
import tempfile
import unittest
from pathlib import Path

import competitive
import competitive_rules
from integration import ExternalEngine, ContractError


class NoPersonalityStats(unittest.TestCase):
    def test_table_and_switches_are_gone(self):
        self.assertFalse(hasattr(competitive, 'MBTI_RULES'))
        self.assertNotIn('mbti_rules', competitive.RULES)
        self.assertNotIn('ei_relationship_multiplier', competitive.RULES)

    def test_old_off_value_is_accepted_and_on_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = ExternalEngine(str(Path(tmp) / 'g.db'), lambda: 1000)
            op = {'operator_token': e.operator_token}
            e.handle('pool/bootstrap', op)
            e.handle('games/create', op | {'game_id': 'off', 'seed': 1, 'rules': {'mbti_rules': False}})
            with self.assertRaises(ContractError):
                e.handle('games/create', op | {'game_id': 'on', 'seed': 1, 'rules': {'mbti_rules': True}})

    def test_mission_difficulty_never_reads_the_type(self):
        for mid, m in competitive_rules.MISSIONS.items():
            self.assertFalse([t for t in m['easier'] + m['harder'] if len(t) == 1 and t in 'EISNTFJP'], mid)
        self.assertEqual({competitive_rules.tier('M45', 7, mbti) for mbti in competitive_rules.MBTI}, {competitive_rules.tier('M45', 7, 'ENTJ')})


if __name__ == '__main__':
    unittest.main()
