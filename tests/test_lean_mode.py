"""FIX-B58: lean mode (opt-in) keeps only what the movement / deduction choice needs."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_agent_experience import M, PromptTests   # noqa: E402

STATE = {"now": {"room": 2, "people_here": [{"id": 3, "name": "배우", "job": "배우", "agent_tag": "abc123", "thresholds": {"q": 3}}]},
         "cycle": 4, "remaining_cycles": 27, "time_budget": {}, "scoring": {"x": 1}, "standings": [], "talk_room": None,
         "accuse_math": {"score_if_wrong": -20}, "allowed_actions": ["move"], "me": {"mbti": "INTP"},
         "recent_actions": [1], "action_summary": {}, "room_notes": {}, "positions": [], "placement_history": [],
         "relationships": {}, "sub_missions": [], "my_information": [{"id": "c"}], "heard_statements": [],
         "already_disclosed": [], "announcements": [], "question_pairs": [], "trades": [], "belief": {"suspect": {}},
         "experience": {"games_played": 2, "plan_for_this_game": "p", "insights": ["i"], "lessons": ["l"], "recent_games": [1]},
         "situation": {"seek": {}, "new_dealings": [1], "heard_about_case": [2], "notable_elsewhere": {}, "my_notes": {}}}


class LeanTests(unittest.TestCase):
    def test_movement_keeps_what_moving_needs(self):
        out = M["agent"].LLMAgent._lean(M["agent"].LLMAgent, dict(STATE, phase="movement"))
        self.assertIn("placement_history", out)
        self.assertNotIn("my_information", out)
        self.assertNotIn("trades", out)
        self.assertEqual(out["now"]["people_here"][0], {"id": 3, "name": "배우", "job": "배우", "agent_tag": "abc123"})
        self.assertEqual(set(out["situation"]), {"seek", "new_dealings", "my_notes"})
        self.assertEqual(set(out["experience"]), {"games_played", "plan_for_this_game", "insights"})
        self.assertTrue(out["lean"])

    def test_deduction_keeps_what_deciding_needs(self):
        out = M["agent"].LLMAgent._lean(M["agent"].LLMAgent, dict(STATE, phase="deduction"))
        for k in ("accuse_math", "belief", "my_information", "scoring"):
            self.assertIn(k, out)
        for k in ("now", "positions", "trades", "recent_actions"):
            self.assertNotIn(k, out)
        self.assertEqual(set(out["situation"]), {"heard_about_case", "new_dealings", "my_notes"})

    def test_interaction_is_untouched_and_the_default_is_off(self):
        self.assertEqual(M["agent"].LLMAgent._lean(M["agent"].LLMAgent, dict(STATE, phase="interaction")), dict(STATE, phase="interaction"))
        a = PromptTests.agent(PromptTests())
        obs = {"cycle_id": 3, "phase": "movement", "self": a.profile, "allowed_actions": ["move", "stay"]}
        os.environ.pop("GAME_LEAN_PROMPTS", None)
        self.assertNotIn("lean", json.loads(a._user_prompt(obs)))
        os.environ["GAME_LEAN_PROMPTS"] = "1"
        try:
            self.assertTrue(json.loads(a._user_prompt(obs))["lean"])
        finally:
            os.environ.pop("GAME_LEAN_PROMPTS", None)


if __name__ == "__main__":
    unittest.main()
