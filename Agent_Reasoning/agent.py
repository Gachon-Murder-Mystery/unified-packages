"""LLM reasoning agent for competitive-v1.3. One observation in, one action out.

The engine owns rules, dice and disclosure; this file only chooses. Payload
shapes follow Game-engine/competitive.py `Rules.action` exactly, since a wrong
field is rejected as `invalid_input` and wastes the phase.

A belief_mode switch exists for the H2 comparison.
  True  -> structured belief (remaining candidates; no probabilities or counts, B52) goes into the prompt
  False -> only raw clue text goes in; the model judges on its own

An unheld ID in evidence_used is recorded rather than rejected: that is exactly
what H2 measures (hallucinated evidence).
"""
import importlib.util
import json
import os
import random
import re
import time
from pathlib import Path

from schema import BeliefState
from llm import GatewayError

_situation_spec = importlib.util.spec_from_file_location(
    "_team_b_situation", Path(__file__).with_name("situation.py"))    # [FIX-B41]
situation = importlib.util.module_from_spec(_situation_spec)
_situation_spec.loader.exec_module(situation)

SCHEMA_VERSION = "b-0.5"


def _load_rules():
    """rules_prompt.py by path: run_game.py swaps B modules in sys.modules by name."""
    spec = importlib.util.spec_from_file_location("_team_b_rules_prompt", Path(__file__).with_name("rules_prompt.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RULES_PROMPT = _load_rules()  # [FIX-B16]
TRADE_BUDGET = 2048
# [FIX-B29] declared-type trades (engine FIX-A25): mirror of Game-engine TRADE_TYPES / TYPE_RANGE
TRADE_TYPES = ("suspect", "weapon", "place", "character_room", "weapon_room", "companion")
# [FIX-B37] trajectory text for the Reflexion log
TYPE_KO = {"suspect": "인물", "weapon": "무기", "place": "장소", "character_room": "인물 위치",
           "weapon_room": "흉기 위치", "companion": "동행"}
TRAJECTORY_LIMIT = 160
TYPE_RANGE = {"suspect": 12, "weapon": 8, "place": 8, "character_room": 12, "weapon_room": 8, "companion": 12}
TALK_LIMIT = 200   # [FIX-B31] free talk on questions, answers and trades
PLACEMENT_CYCLES = 6
TRADE_CATEGORIES = ("suspect", "weapon", "place", "indirect")
LOG_PATH = Path("decisions.jsonl")

CATEGORIES = ("suspect", "weapon", "place")
SIZES = {"suspect": 12, "weapon": 8, "place": 8}
APPROACHES = ("question", "interrogate", "threaten", "persuade")
STAT_BY_APPROACH = {"explore": "observe", "question": "speech", "persuade": "speech",
                    "interrogate": "pressure", "threaten": "pressure"}
FALLBACK = {"movement": ("stay", {}), "interaction": ("pass", {}), "deduction": ("pass", {})}
DECIDE_BUDGET = 4096
ANSWER_BUDGET = 2048
REFLECT_BUDGET = 2048
# [FIX-B11] Reasoning cap. A truncated answer used to be retried at 2x and 4x the budget
# (4096 -> 16384 tokens), so a long-thinking agent was simply given more. Now the first
# budget is the cap; GAME_MAX_BUDGET (tokens) restores escalation up to that value.
MAX_BUDGET = int(os.environ.get("GAME_MAX_BUDGET", "0") or 0)
RECENT_ACTIONS = 8
# [FIX-B5] GAME_SPEC v1.3 B7: MBTI goes into the prompt as tendencies, not orders.
# [FIX-B32 -> B34] user decisions 2026-11-08: the type is how the agent leans at the game's choice
# points. It is NOT an ability: rolls, scores and relationship numbers come from the job and the rules,
# never from the type.
# [FIX-B57] user decision 2026-11-10: every text states the CAUSE (what the type values, what it trusts,
# how it thinks and feels); the behaviour and its consequences are left to follow from that. Removed: the
# "그래서 ..." cost sentences, reactions stated as results ("크게 흔들린다", "서운해하지 않는다") and
# prescribed behaviours ("탐색한다", "더 내준다"). The user's definitions (B43, B48-B50, B56) are kept.
MBTI_TENDENCY = {
    # [FIX-B70] user decision 2026-11-18 (personality probe I25): S may form and weigh hypotheses, it only does not ACT
    # on them before confirmation ("그 위에 추론을 쌓지 않고" read as no imagination); E lost a warmth clause
    # ("상대가 편하게 느끼는지를 자연스럽게 신경 쓰고") that leaked agreeableness (F) into E. Old texts: CHANGES B70.
    # [FIX-B48] user decision 2026-11-09: E/I is the size of the company (people_here), not liking people
    "E": "E: 사람들과 어울리는 것 자체에서 힘을 얻고, 관계를 넓혀 가는 것을 즐긴다. 사람이 많은 자리도 편하다. "
         "자기 역할(직업)과 상관없이 누구와도 통하는 공통 화제를 찾는 것을 좋아하는데, 지금 모두의 공통 화제는 이 사건과 추리다. "
         "처음 보는 사람에게도 먼저 말을 거는 것이 자연스럽고, 남이 나를 어떻게 평가하는지보다 어울림 자체에 마음이 가 있다",
    # [FIX-B64] user decision 2026-11-12: no head count (12 people in 8 rooms rarely make a crowd of four)
    "I": "I: 힘을 안에서 얻는다. 몇 사람과 한 무리로 깊이 이야기하는 자리는 E와 다름없이 편하다. "
         "여러 사람이 모여 대화가 이리저리 갈라지는 자리는 주목받는 것이 부담스럽고 흩어지는 대화가 피곤하다. "
         "마음은 맡은 역할(직업)과 내 관심사(내가 필요한 단서 유형)에 가 있어, 여러 사람의 관심사보다 내 관심사를 두고 하는 이야기가 편하다. "
         "직업이 비슷한 사람과는 통하는 것이 많다고 느끼고, 완전히 다른 직업의 사람은 낯설다. "
         "익숙하지 않은 형태의 대화가 어떻게 흘러갔는지를 곱씹어 둔다",
    # [FIX-B49] user decision 2026-11-09: S = reality, N = ideals (reasoning and outward attitude)
    # [FIX-B57] user, 2026-11-10: both want a better chance; S builds certainty, N values acting first
    "S": "S: 현실을 추구한다. 경험으로 확인했거나 확신하는 사실을 믿을 만하다고 여기고, 불확실하고 불안정한 상태가 불편하다"
         "(그 불편함을 피할지 직접 풀지는 다른 성향에 따라 다르다). 현실적이고 실리적인 것에 가치를 둔다. "
         "확인되지 않은 말이나 가설은 아직 가능성일 뿐이라, 가설을 세워 따져 볼 수는 있어도 확인되기 전에는 그것을 근거로 움직이지 않고(상상하지 못하는 것은 아니다), 확실하게 해 두고 싶은 마음이 크다. "
         "확신을 갖고 움직이는 것을 중요하게 여긴다. 남의 말도 문자 그대로 받아들이고, 말할 때도 구체적인 사실을 중요하게 여긴다",
    "N": "N: 이상을 추구한다. 직관과 가능성을 믿는다(숙고하지 않는 것은 아니다). 간접 단서, 담화, 패턴에서 가능성을 읽고, "
         "말의 결과 맥락에서 상대의 속뜻을 읽는다. 사람의 말을 비교적 쉽게 믿는다. "
         "확신이 다 서기 전이라도 먼저 잡는 기회에 가치를 두고, 떠오른 가능성을 밀고 나가는 데서 힘을 얻는다. "
         "그만큼 자기 믿음과 예측에 마음을 많이 싣는다",
    # [FIX-B43] user decision 2026-11-09: T = reason, F = feeling. F builds the relationship first and
    # the practical gain follows from it; T builds relationships by exchanging benefits, and T's fork is
    # short-term gain vs people-first exchange (chosen by its own reflection, not fixed here).
    "T": "T: 근거와 손익으로 판단한다. 설득도 근거와 조건으로 하는 것이 맞다고 여기고, 관계도 이익을 주고받는 것이라 여긴다. "
         "그 이익을 보는 눈은 두 갈래다. 매 거래의 손익(단기 실리), 또는 결국 사람이 남는다는 믿음"
         "(먼저 내주고 약속을 지켜 오래 주고받을 상대를 만드는 교류). 어느 쪽을 믿을지는 네 경험(회고)이 정한다. "
         "사람을 볼 때는 말보다 행동(이행, 기만, 거절 사유의 타당성)을 근거로 삼는다",
    "F": "F: 마음으로 판단한다. 관계가 먼저이고, 실리는 쌓인 관계에서 따라온다고 여긴다. "
         "마음을 나누는 것(공감, 고마움과 서운함의 표현)을 중요하게 여기고, 친밀한 사람과의 관계는 눈앞의 손해보다 소중하다. "
         "상대를 몰아붙이는 방식(심문·협박)은 관계를 해친다고 여겨 싫어한다. "
         "말투와 태도(존중, 따뜻함, 위압)에서 상대가 나를 어떻게 보는지에 마음이 가고, 서운함은 쉽게 잊히지 않는다",
    # [FIX-B49] user decision 2026-11-09: J = planner, P = improviser
    # [FIX-B56] user decision 2026-11-10: a tendency states the cause, never the result. J's plan is about its own
    # actions (what to try), not about others' decisions; it holds an agreement binding. How J reacts to a refusal
    # or to a broken agreement is left to follow from that (B49 had "reacts strongly to whoever disturbs the plan",
    # which made a partner's free refusal a disruption: compulsion, not a tendency).
    "J": "J: 계획형이다. 판 초반에 내가 할 일의 순서와 방법을 정해 메모로 남기고, 그것을 지켜 나가는 데서 안정감을 느낀다. "
         "계획은 어디까지나 내가 할 행동(무엇을 시도할지)에 대한 것이고, 상대가 어떻게 응할지는 상대의 몫이라 계획에 넣지 않는다. "
         "약속을 무겁게 여긴다. 한번 합의하거나 선언한 것은 손해가 나도 지키고, 상대도 합의한 것은 지킬 것이라 여긴다. "
         "일을 매듭짓기를 좋아해, 응답은 되도록 수락이나 거절로 분명히 끝낸다. 판을 돌아볼 때는 결과보다 내가 계획대로 했는지를 먼저 따진다",
    "P": "P: 즉흥형이다. 정해 둔 계획이 없고, 계획 자체가 쓸모없다고 느낀다. 상황은 늘 바뀐다고 보아 "
         "'이번엔 이렇게 하자'보다 '지금 이러니 이렇게 해 보자'가 자연스럽다. 판단을 미뤄 두고 확신을 더 쌓기보다 지금 손에 잡히는 것을 "
         "붙잡는 편이 낫다고 여긴다(S와 함께라면 눈앞에서 확인되는 구체적 사실을, N과 함께라면 떠오른 가능성을). "
         "선택지를 열어 두는 것을 좋아하고, 떠오르는 대로 말하는 것이 편하다(드러나는 정도는 다른 성향에 따라 다르다). "
         "조건은 주고받으며 맞춰 가는 것이라 여겨 흥정을 즐긴다",
}
# [FIX-B43] my own out-of-game relationship to each pool agent (affinity): starts at 50, only I change it
AFFINITY_START = 50.0
IMPRESSION_STEP = 10         # one impression moves it by at most this much
IMPRESSIONS_PER_TURN = 2
AFFINITY_WHY_KEEP = 3
DEALINGS_KEEP = 24          # [FIX-B47] events kept until my next decision reacts to them
MBTI_TENSION = {   # [FIX-B57] causes too: what the two leanings together value
    "ES": "E이면서 S: 사람을 좋아하되 말보다 확인된 것을 믿어, 직접 확인한 것을 주고받는 사이를 편하게 여긴다",
    "FP": "F이면서 P: 조건은 유연하게 주고받는 것이라 여기지만, 관계가 좋은 상대를 속이는 일은 마음이 허락하지 않는다",
    "NJ": "N이면서 J: 결론은 직관으로 빨리 서지만, 계획한 때와 근거가 갖춰져야 움직일 마음이 놓인다",
    "TJ": "T이면서 J: 손익으로 따지지만, 한 번 한 약속은 손익보다 무겁다고 여긴다",
    "IN": "I이면서 N: 드러나지 않게 생각을 굴리는 것이 편하고, 받은 간접 단서와 조합 배제에서 혼자 답을 읽어 내는 데 끌린다",
    "SF": "S이면서 F: 확인을 중시하되, 캐묻는 것은 관계가 허락하는 상대에게만 괜찮다고 여긴다",
    # [FIX-B50] user decision 2026-11-09: the splits S and N leave to combinations, made explicit (Keirsey)
    "SJ": "S이면서 J: 검증된 방식과 확인된 상대가 믿을 만하다고 여기고, 불확실한 길이 불편하다",
    "SP": "S이면서 P: 불확실은 미리 세운 계획보다 지금 눈앞에서 확인하는 것으로 푸는 것이 맞다고 여긴다",
    "NT": "N이면서 T: 믿음도 판단의 근거라서, 한번 깨진 믿음은 근거로 삼을 수 없다고 여긴다",
    "NF": "N이면서 F: 믿음은 관계의 일부라서, 깨지더라도 관계를 되살릴 여지가 있다고 여긴다",
    "NP": "N이면서 P: 무엇이든 단정하지 않고 가능성으로 열어 두는 것이 자연스럽다",
}
TACTIC_NAMES = ("verify_before_accuse", "ask_before_duplicate_search", "share_when_stalled",
                "parallelize_independent_tasks", "prefer_unexplored_rooms", "request_before_escalation")


class LLMAgent:
    def __init__(self, agent_id, client=None, belief_mode=True, catalog=None, log_path=LOG_PATH):
        self.agent_id = agent_id
        self.client = client
        self.belief_mode = belief_mode
        self.catalog = catalog or {}
        self.log_path = Path(log_path)

        self.initialized = False
        self.profile = {}
        self.belief = None
        self.known_info = []        # clues I hold, as the engine sent them
        self.applied_seen = []      # clue ids already folded into belief
        self.disclosed = []         # clue ids I have handed to someone
        self.candidates = {}
        self.relationships = {}
        self.sub_missions = []
        self.cycle = 0
        self.phase = None
        self.turn = 0
        self.inactive = False
        self._tokens = 0            # local tally if the client has no counter
        self._last_roll = None
        # [FIX-B1] cross-game experience block, set by the runtime before the first decision
        self.experience = None
        # [FIX-B4] within-game memory the prompt never had
        self.action_log = []        # my own past actions and their outcomes
        self.room_notes = {}        # room -> {"explores", "found", "exhausted"}
        self.clues_by_cycle = {}    # cycle -> number of clues held (stall detection)
        self.accused = False
        self.combinations_at_accuse = None
        self.last_observation = {}
        self.model_fallbacks = {}   # [FIX-B9] kind -> count of rule-based stand-ins for the model
        self.model_calls = 0
        self.trades_view = []       # [FIX-B17] my trades as the runtime last saw them
        self.peer_reputation = {}   # [FIX-B20/B30] agent_id -> how that agent treated me (MY memory, all games)
        self.last_trade = None      # [FIX-B17] proposal attached to the last interaction decision
        # [FIX-B30] the engine keeps no trade history for players: I remember it myself
        self.provenance = {}        # clue_id -> {"via", "from_character", "cycle"}
        self.gave_to = {}           # agent_id -> clue ids I handed to that agent (this game)
        self.got_from = {}          # agent_id -> clue ids that agent handed to me (this game)
        self.trade_log = []         # my judged trades (this game)
        self.heard_claims = []      # [FIX-B31] what others told me (unverified)
        self.last_answer_message = None
        # [FIX-B37] Reflexion trial log: cycle-by-cycle position, action, outcome (this game)
        self.trajectory = []
        self._traced_cycle = None
        self._known_at_decision = None   # [FIX-B38] clue ids I held when I chose my last action
        # [FIX-B41/B42] my own memory of the situation (not the clues) and my own notes
        self.situation = situation.SituationMemory()
        self.notes = situation.Notes()
        self.memory_feedback = None
        # [FIX-B43] my own out-of-game relationship to other pool agents lives in peer_reputation
        # [agent_id]["affinity"] (starts at 50, only I change it); who plays which character this game
        self.agent_of_char = {}
        self.talk_intent = False      # [FIX-B54] my wish to join this cycle's talk room (movement output)
        self._talk_seen = set()       # [FIX-B54] talk-room lines I already heard
        self.affinity_dirty = False
        self.pending_game_reactions = []   # [FIX-B61]
        self.new_dealings = []     # [FIX-B47] what happened with others since my last decision
        self.trade_reviews_due = []   # [FIX-B68] my proposals the partner chose not to take, to review next decision

    # ---------- token accounting (deduction rank: fewest wins) ----------

    @property
    def output_tokens(self):
        totals = getattr(self.client, "usage_totals", None)
        if callable(totals):
            return int(totals().get("output") or 0)
        return self._tokens

    def _count(self, usage):
        if isinstance(usage, dict):
            self._tokens += int(usage.get("output") or 0)

    # ---------- stage 1: state sync ----------

    def sync(self, observation):
        me = observation.get("self") or {}

        if not self.initialized:
            self.profile = dict(me)
            self.initialized = True
        else:
            if me.get("personality") != self.profile.get("personality"):
                self._log({"event": "profile_changed",
                           "before": self.profile.get("personality"),
                           "after": me.get("personality")})
            self.profile = dict(me)

        if observation.get("candidates"):
            self.candidates = observation["candidates"]
        if self.belief_mode and self.belief is None and self.candidates:
            self.belief = BeliefState(self.candidates, agent_id=self.agent_id)

        self.relationships = observation.get("relationships") or {}
        self.sub_missions = observation.get("sub_missions") or []
        self.cycle = observation.get("cycle_id", self.cycle)
        self.phase = observation.get("phase")

        mode = me.get("mode", "active")
        if mode != "active" and not self.inactive:
            self.inactive = True
            self._log({"event": "mode_changed", "mode": mode})
        elif mode == "active":
            self.inactive = False

        self.known_info = observation.get("information") or []
        # [FIX-B30] where each clue came from (results and trades relabel it precisely)
        first = not self.provenance
        for item in self.known_info:
            key = self._item_id(item)
            if key not in self.provenance:
                self.provenance[key] = {"via": "start" if first else "unknown", "cycle": self.cycle}
        if self.belief_mode and self.belief is not None:
            held = {self._item_id(i) for i in self.known_info}
            # [FIX-B23] pair exclusions I was told in answers are facts too
            heard = [s for s in observation.get("statements") or [] if s.get("pair") and s.get("id")]
            for item in self.known_info + (observation.get("shared_information") or []) + heard:
                key = self._item_id(item)
                if key in self.applied_seen:
                    continue
                self.belief.apply(item, source="own" if key in held else "heard")
                self.applied_seen.append(key)
                self._log({"event": "clue_seen", "cycle": self.cycle, "clue_id": key,
                           "kind": item.get("kind"),
                           "source": "own" if key in held else "shared"})
        self.clues_by_cycle[self.cycle] = len(self.known_info)
        self.situation.placements(observation.get("placements"))                    # [FIX-B41]
        self.ingest_talk((observation.get("self") or {}).get("talk"))                 # [FIX-B54]
        self.situation.pairs(observation.get("question_pairs"), self.cycle)
        if self.cycle != self._traced_cycle and mode == "active":   # [FIX-B37]
            self._traced_cycle = self.cycle
            definite = sum(1 for i in self.known_info if isinstance(i, dict) and i.get("kind") == "definite")
            indirect = sum(1 for i in self.known_info if isinstance(i, dict) and i.get("kind") == "indirect")
            # [FIX-B52] the trajectory feeds reflection, so it carries my own clue counts, not a computed combination count
            self._trace("cycle", f"시작: 칸 {me.get('room')}, 확실 단서 {definite}, 간접 단서 {indirect}")
        self.last_observation = observation
        self.turn += 1

    # ---------- stage 2: prompts ----------

    def _system_prompt(self):
        # [FIX-B16] engine-based rules (rules_prompt.py says what changed and why)
        return RULES_PROMPT.DECISION_FORMAT + RULES_PROMPT.RULES

    def _thresholds(self, observation, stats):
        """[FIX-B3] Prefer the engine's effective thresholds (skills, room, relation).
        Older engines do not send them; then fall back to stats only and say so."""
        engine = observation.get("thresholds")
        if isinstance(engine, dict):
            return dict(engine, source="engine")
        return {**{a: min(6, max(2, 8 - (stats.get(s) or 0))) for a, s in STAT_BY_APPROACH.items()},
                "source": "stats_only (skills/room/relation not applied)"}

    def _accuse_math(self):
        if self.belief is None:
            return None
        # [FIX-B44] no precomputed chance. [FIX-B52] no combination count either: the agent counts
        # and weighs its own clues. self._combinations() is for research logs only.
        out = {}
        # [FIX-B10] What each choice is worth under this game's scoring (FIX-A4: sub-missions
        # are void for survivors of an unsolved game, kept after a wrong accusation).
        scoring = (getattr(self, "last_observation", None) or {}).get("scoring")
        if isinstance(scoring, dict):
            rewards = scoring.get("sub_mission_rewards") or {}
            banked = sum(rewards.get(m.get("tier"), 0) for m in self.sub_missions if m.get("achieved"))
            void = scoring.get("sub_missions_if_unsolved") == "void for survivors"
            out.update(my_mission_points=banked,
                       score_if_correct_first=(scoring.get("win_by_rank") or [100])[0] + banked,
                       score_if_wrong=scoring.get("wrong_accusation", -20) + banked,
                       score_if_unsolved_and_alive=scoring.get("unsolved_survivor", 0) + (0 if void else banked),
                       # [FIX-B28] someone else solving ends the game: survivors score 0 and keep missions
                       score_if_someone_else_wins=0 + (banked if scoring.get("sub_missions_if_someone_wins", "kept") == "kept" else 0))
        return out

    def _combinations(self):
        """[FIX-B52] research only (journal, game stats); never put into a prompt."""
        return self.belief.combinations() if self.belief is not None else None

    def _standings(self, observation):
        """[FIX-B55/A40] Cumulative scores of every pool player at the start of this game (public)."""
        rows = ((observation.get("self") or {}).get("standings")) or []
        if not rows:
            return None
        me = self.agent_id
        return [{"agent_tag": r["agent_id"][-6:], "score": r["cumulative_score"], "games": r["games"],
                 **({"me": True} if r["agent_id"] == me else {})} for r in rows]

    # ---------- [FIX-B54] talk rooms ----------
    def ingest_talk(self, view):
        """Lines of my talk room I have not heard yet go to my memory (cards, claims, dealings to react to)."""
        if not view or not view.get("joined"):
            return
        for line in view.get("log") or []:
            key = (self.cycle, line.get("round"), line.get("from_character"), line.get("text"))
            if key in self._talk_seen or line.get("from_character") == self._me_id():
                continue
            self._talk_seen.add(key)
            self.hear(line.get("from_character"), "담화방", line.get("text"))

    def chat(self, observation, view):
        """One line in my talk room, as this game's character. Returns {"say", "leave"} or None (model failed)."""
        self.ingest_talk(view)
        me = observation.get("self") or {}
        members = set(view.get("members") or [])
        people = [self._occupant(x) for x in observation.get("occupants") or [] if x.get("id") in members and x.get("id") != me.get("character_id")]
        prompt = json.dumps({
            "me": {"character_id": me.get("character_id"), "name": me.get("name"), "job": (me.get("job") or {}).get("name"),
                   "skill": me.get("skill"), "stats": me.get("stats"), "mbti": me.get("mbti"),
                   "tendencies": self._tendencies(me.get("mbti"))},
            "cycle": self.cycle, "round": view.get("round"), "rounds": view.get("rounds"), "must_speak": view.get("must_speak"),
            "members": view.get("members"), "left": view.get("left"), "log": view.get("log"),
            "people": people, "my_notes": self.notes.view(), "standings": self._standings(observation),
            "trade_proposals": me.get("trade_proposals"), "experience_insights": (self.experience or {}).get("insights"),
        }, ensure_ascii=False)
        if self.client is None:
            return {"say": "...", "leave": view.get("round", 1) > 1}
        for attempt in (1, 2):
            try:
                text, usage = self._request_complete(RULES_PROMPT.TALK_SYSTEM, prompt, budget=TRADE_BUDGET)
                self._count(usage)
            except GatewayError as exc:
                self._log({"event": "llm_error", "cycle": self.cycle, "context": "talk", "reason": str(exc)[:200]})
                return None
            parsed = self._parse(text) or {}
            say = parsed.get("say")
            say = say.strip() if isinstance(say, str) else ""
            leave = parsed.get("leave") is True
            if len(say) <= TALK_LIMIT and (say or (leave and not view.get("must_speak"))):
                self._log({"event": "talk_line", "cycle": self.cycle, "round": view.get("round"), "say": say, "leave": leave,
                           "consideration": parsed.get("consideration"), "attempt": attempt})
                return {"say": say or None, "leave": leave}
            self._log({"event": "invalid_talk", "cycle": self.cycle, "attempt": attempt, "raw": str(text)[:200]})
            prompt = json.dumps(dict(json.loads(prompt), last_error="say는 200자 이내. 첫 라운드(must_speak)에는 반드시 말한다"),
                                ensure_ascii=False)
        return None

    def decide_share(self, feedback, standings):
        """[FIX-B55/A40] I ranked first: give part of my score of this game to other pool agents, or not.
        Returns {"shares": [{"agent_id", "points"}], "consideration"}. Falls back to giving nothing."""
        budget = int(feedback.get("final_score") or 0)
        tag_to_aid = {r["agent_id"][-6:]: r["agent_id"] for r in standings}
        me = self.agent_id
        people = []
        for cid, aid in self.agent_of_char.items():
            if aid == me or cid == self._me_id():
                continue
            people.append({"agent_tag": aid[-6:], "character": cid, "my_affinity": self._affinity(aid),
                           "this_game_with_me": self.situation.view(cid, me=self._me_id())})
        prompt = json.dumps({
            "me": dict(self._me_type(), character_id=self._me_id(),
                       name=(self.profile or {}).get("name"), job=((self.profile or {}).get("job") or {}).get("name")),
            "my_score_this_game": budget,
            "standings_before_sharing": [{"agent_tag": r["agent_id"][-6:], "score": r["cumulative_score"], "games": r["games"],
                                          **({"me": True} if r["agent_id"] == me else {})} for r in standings],
            "players_of_this_game": people,
            "my_notes": self.notes.view(),
        }, ensure_ascii=False)
        out = {"shares": [], "consideration": None}
        if self.client is None:
            return out
        for attempt in (1, 2):
            try:
                text, usage = self._request_complete(RULES_PROMPT.SHARE_SYSTEM, prompt, budget=TRADE_BUDGET)
                self._count(usage)
            except GatewayError as exc:
                self._log({"event": "llm_error", "context": "share", "reason": str(exc)[:200]})
                break
            parsed = self._parse(text) or {}
            shares, total, ok = [], 0, isinstance(parsed.get("shares"), list)
            for x in parsed.get("shares") or []:
                aid = tag_to_aid.get(str((x or {}).get("agent") or ""))
                pts = (x or {}).get("points")
                if not aid or aid == me or type(pts) is not int or pts <= 0 or any(s["agent_id"] == aid for s in shares):
                    ok = False
                    break
                shares.append({"agent_id": aid, "points": pts})
                total += pts
            if ok and total <= budget:
                out = {"shares": shares, "consideration": parsed.get("consideration"),
                       "why": {s["agent_id"][-6:]: str((x or {}).get("why") or "")[:200]
                               for s, x in zip(shares, parsed.get("shares") or [])}}
                self._log({"event": "share_decision", "budget": budget, "shares": [dict(s, agent=s["agent_id"][-6:]) for s in shares],
                           "consideration": parsed.get("consideration"), "attempt": attempt})
                return out
            self._log({"event": "invalid_share", "attempt": attempt, "raw": str(text)[:200]})
            prompt = json.dumps(dict(json.loads(prompt), last_error=f"shares는 standings의 다른 agent_tag와 양의 정수 points, 합은 {budget} 이하"),
                                ensure_ascii=False)
        return out

    def _missions(self):
        """[FIX-B6] Sub-missions arrived as bare IDs ("M20"); attach name and condition."""
        catalog = {m.get("id"): m for m in (self.catalog.get("missions") or []) if isinstance(m, dict)}
        out = []
        for m in self.sub_missions:
            info = catalog.get(m.get("mission_id"), {})
            out.append(dict(m, name=info.get("name"), condition=info.get("condition")))
        return out

    def _me_type(self):
        me = (self.last_observation or {}).get("self") or {}
        return {"mbti": me.get("mbti"), "tendencies": self._tendencies(me.get("mbti"))}

    def _tendencies(self, mbti):
        out = [MBTI_TENDENCY[x] for x in (mbti or "") if x in MBTI_TENDENCY]
        # [FIX-B32] axes that pull apart: the natural compromise of that combination (not an order)
        out += [text for pair, text in MBTI_TENSION.items() if all(x in (mbti or "") for x in pair)]
        return out

    def _now(self, observation, feedback=None):
        """[FIX-B36] What is available in MY room right now, given before any memory: the people I can
        ask or trade with (legal targets, with the engine's thresholds for each), corpses, the search."""
        me = observation.get("self") or {}
        occ = observation.get("occupants") or []
        living = [x for x in occ if x.get("mode") != "corpse"]
        engine = observation.get("thresholds") or {}
        by_target = engine.get("by_target") or {}
        people = []
        for x in living:
            item = self._occupant(x)
            item["my_relationship"] = self.relationships.get(str(x.get("id")))
            item["thresholds"] = by_target.get(str(x.get("id")))
            people.append(item)
        now = {"cycle": observation.get("cycle_id"), "phase": observation.get("phase"),
               "my_room": me.get("room"), "allowed_actions": observation.get("allowed_actions") or [],
               "people_here": people, "valid_targets": [x.get("id") for x in living],
               "corpses_here": [x.get("id") for x in occ if x.get("mode") == "corpse"],
               "explore_here": {"threshold": engine.get("explore"), "corpse_threshold": engine.get("explore_corpse"),
                                "last_seen": self.room_notes.get(me.get("room"))},
               "prepared": me.get("prepared")}
        request = observation.get("action_request") or {}
        if request:  # [FIX-B36/A32] the GM's request: the only actions and targets I may choose now
            now["gm_request"] = {"phase_epoch": request.get("phase_epoch"), "options": request.get("options")}
            q = (request.get("options") or {}).get("question")
            if q is not None:
                now["valid_targets"] = [x.get("id") for x in q.get("targets") or []]
        if observation.get("replacement"):   # [FIX-B68] the rules promise the reason; it was never passed on
            now["replacement"] = observation["replacement"]
        if feedback:
            now["previous_output_rejected"] = feedback
        return now

    def _user_prompt(self, observation, feedback=None):
        me = observation.get("self") or {}
        stats = me.get("stats") or {}
        here = {x.get("id") for x in observation.get("occupants") or [] if x.get("mode") != "corpse"}
        state = {
            "now": self._now(observation, feedback),                 # [FIX-B36] first: what my room offers
            "cycle": observation.get("cycle_id"),
            "remaining_cycles": observation.get("remaining_cycles"),
            "time_budget": observation.get("time_budget"),      # [FIX-B2]
            "scoring": observation.get("scoring"),              # [FIX-B2]
            "standings": self._standings(observation),          # [FIX-B55] cumulative scores (public)
            "talk_room": me.get("talk"),                         # [FIX-B54] my room's talk this cycle
            "accuse_math": self._accuse_math(),                 # [FIX-B2]
            "phase": observation.get("phase"),
            "allowed_actions": observation.get("allowed_actions") or [],
            "me": {
                "character_id": me.get("character_id"),
                "name": me.get("name"),
                "job": (me.get("job") or {}).get("name"),
                "mbti": me.get("mbti"),
                "tendencies": self._tendencies(me.get("mbti")),   # [FIX-B5]
                "room": me.get("room"),
                "prepared": me.get("prepared"),
                "stats": stats,
                "skill": me.get("skill"),
                "thresholds": self._thresholds(observation, stats),   # [FIX-B3]
                "trade_proposals": me.get("trade_proposals"),        # [FIX-B53] my own count; only I see it
            },
            # [FIX-B4] my past actions; [FIX-B36] past targets say whether they are in my room NOW
            # [FIX-B41] the last 4 in full, every earlier one in the per-kind summary (nothing silently lost)
            "recent_actions": [dict(a, target_here_now=a.get("target") in here) if a.get("target") is not None else a
                               for a in self.action_log[-4:]],
            "action_summary": self._action_summary(),
            "room_notes": {str(k): v for k, v in sorted(self.room_notes.items())},
            "experience": self.experience,                          # [FIX-B1]
            "positions": [{"id": x.get("id"), "room": x.get("room"), "mode": x.get("mode")}
                          for x in observation.get("positions") or []],
            "relationships": self.relationships,
            "sub_missions": self._missions(),                       # [FIX-B6]
            "my_information": self._my_info(),                       # [FIX-B30] with provenance
            "heard_statements": (observation.get("statements") or [])[-8:],
            "already_disclosed": self.disclosed,
            "announcements": observation.get("announcements") or [],
            "question_pairs": (observation.get("question_pairs") or [])[-10:],
        }
        # [FIX-B17] open trades and those closed this cycle; closed ones live on in the cards (B41)
        state["trades"] = [t for t in self.trades_view if t.get("status") == "proposed" or t.get("cycle") == self.cycle]
        # [FIX-B18] public placement per cycle (FIX-A13), room -> characters, latest last
        state["placement_history"] = [
            {"cycle": x.get("cycle"),
             "rooms": {str(r): sorted(int(c) for c, rr in (x.get("rooms") or {}).items() if rr == r)
                       for r in sorted(set((x.get("rooms") or {}).values()))}}
            for x in (observation.get("placements") or [])[-PLACEMENT_CYCLES:]]
        if self.belief_mode and self.belief is not None:
            belief = self.belief.summary()
            # [FIX-B16] remaining used to be listed by ID with equal p, and the model took the
            # first one (weapon 0 in 59% of question hypotheses). The order carries no meaning.
            rng = random.Random(f"{self.agent_id}:{self.cycle}:{observation.get('phase')}")
            for key in CATEGORIES:
                part = belief.get(key)
                if isinstance(part, dict) and isinstance(part.get("remaining"), list):
                    part["remaining"] = rng.sample(part["remaining"], len(part["remaining"]))
            state["belief"] = belief
            needed = [k for k in situation.DEFINITE if len((belief.get(k) or {}).get("remaining") or []) > 1]
        else:
            needed = list(situation.DEFINITE)
            state["candidates"] = {k: [c.get("name") for c in (self.candidates.get(k) or [])]
                                   for k in CATEGORIES}
        if self._last_roll:
            state["last_roll"] = self._last_roll
        state["situation"] = self._situation_block(needed + list(situation.INDIRECT), here)   # [FIX-B41/B42]
        if os.environ.get("GAME_LEAN_PROMPTS") == "1":   # [FIX-B58] opt-in lean mode
            state = self._lean(state)
        return json.dumps(state, ensure_ascii=False)

    # [FIX-B58] user request 2026-11-10: a lean variant to measure tokens saved against what the agents lose.
    # Movement chooses a room; deduction chooses accuse or pass. Each gets only what that choice needs.
    LEAN_KEEP = {
        "movement": ("now", "cycle", "remaining_cycles", "phase", "allowed_actions", "me", "positions", "placement_history",
                     "room_notes", "belief", "sub_missions", "experience", "situation", "standings"),
        "deduction": ("cycle", "remaining_cycles", "time_budget", "phase", "allowed_actions", "scoring", "standings",
                      "accuse_math", "me", "belief", "my_information", "heard_statements", "sub_missions", "experience",
                      "talk_room", "announcements", "situation"),
    }
    LEAN_SITUATION = {"movement": ("seek", "new_dealings", "trade_review_due", "my_notes", "memory_feedback"),
                      "deduction": ("heard_about_case", "new_dealings", "trade_review_due", "my_notes", "memory_feedback")}

    def _lean(self, state):
        phase = state.get("phase")
        keep = self.LEAN_KEEP.get(phase)
        if not keep:
            return state
        out = {k: state[k] for k in keep if k in state}
        if "now" in out:   # where I am and who is here; the per-target thresholds matter only for acting
            now = out["now"] or {}
            out["now"] = {"room": now.get("room"), "people_here": [{k: p.get(k) for k in ("id", "name", "job", "agent_tag")}
                                                                   for p in now.get("people_here") or []]}
        if isinstance(out.get("experience"), dict):   # my plan and insights; past games' details stay in memory
            out["experience"] = {k: out["experience"][k] for k in ("games_played", "plan_for_this_game", "insights")
                                 if k in out["experience"]}
        if isinstance(out.get("situation"), dict):
            out["situation"] = {k: v for k, v in out["situation"].items() if k in self.LEAN_SITUATION[phase]}
        out["lean"] = True
        return out

    # ---------- [FIX-B41/B42] my situation memory and notes ----------
    def _me_id(self):
        return (self.profile or {}).get("character_id")

    def _case_claims(self):
        """[FIX-B45] Words about the case with my own record of each speaker (never certain evidence)."""
        rows = self.situation.case_claims(self._me_id())
        for r in rows:
            aid = self.agent_of_char.get(r["from"])
            rec = self.peer_reputation.get(aid) or {} if aid else {}
            r["speaker"] = {
                            "my_affinity": self._affinity(aid)["score"] if aid else None}
        return rows or None

    def _seek_with_relation(self, needed, here):
        """[FIX-B47] who holds what (practical gain) next to the relationship, so both weigh the choice."""
        seek = self.situation.seek(needed, set(here), self._me_id()) or {}
        for rows in seek.values():
            for row in rows:
                aid = self.agent_of_char.get(row["id"])
                if aid:
                    row["relation"] = self._affinity(aid)
                card = self.situation.cards.get(row["id"]) or {}
                refused = [t for t in card.get("trades", []) if t.get("status") == "refused"]
                if refused:
                    row["last_refusal"] = {k: refused[-1].get(k) for k in ("cycle", "requested", "their_words", "my_review")
                                           if refused[-1].get(k)}   # [FIX-B68] with my own review of it
        return seek or None

    def _situation_block(self, needed, here=()):
        return {"seek": self._seek_with_relation(needed, here),
                "new_dealings": self._dealings_view() or None,             # [FIX-B47] react to each (required)
                "trade_review_due": self._reviews_view() or None,          # [FIX-B68] review each (required)
                "heard_about_case": self._case_claims(),                    # [FIX-B45]
                "my_notes": self.notes.view(),
                **({"memory_feedback": self.memory_feedback} if self.memory_feedback else {})}

    def _action_summary(self):
        out = {}
        for a in self.action_log:
            s = out.setdefault(a["action"], {"count": 0, "with_new_clues": 0, "new_clues": 0})
            s["count"] += 1
            s["with_new_clues"] += int((a.get("new_clues") or 0) > 0)
            s["new_clues"] += a.get("new_clues") or 0
        return out

    def _type_keys(self, ids):
        held = {self._item_id(i): i for i in self.known_info if isinstance(i, dict)}
        out = []
        for cid in ids or []:
            info = held.get(cid) or {}
            out.append(info.get("category") if info.get("kind") == "definite" else info.get("type"))
        return [k for k in out if k]

    def _memo(self, ops, impressions=None):
        """Apply the memory operations of an accepted output; refusals are shown next time."""
        refused_impressions = self._impress(impressions)
        if not ops:
            self.memory_feedback = {"impression_refused": refused_impressions} if refused_impressions else None
            return
        applied, refused = self.notes.apply(ops, self.cycle)
        feedback = {}
        if refused:
            feedback["refused"] = refused
        if refused_impressions:
            feedback["impression_refused"] = refused_impressions
        self.memory_feedback = feedback or None
        self._log({"event": "memory_ops", "cycle": self.cycle, "applied": applied, "refused": refused})

    def _affinity(self, aid):
        rec = self.peer_reputation.get(aid) or {}
        out = {"score": round(float(rec.get("affinity", AFFINITY_START)), 1), "why": list(rec.get("why") or [])[-2:]}
        if "their_attitude" in rec:   # [FIX-B47] how I think they see me (my inference)
            out["their_attitude_to_me"] = rec["their_attitude"]
        gifts = {g: rec[g] for g in ("gifts_received", "gifts_given") if rec.get(g)}   # [FIX-B55] post-game shares
        if gifts:
            out["post_game_shares"] = gifts
        return out

    # ---------- [FIX-B47] dealings with others -> my reaction ----------
    def _dealing(self, who, what, text=None):
        if who is None or who == self._me_id() or not isinstance(who, int):
            return
        item = {"who": who, "cycle": self.cycle, "what": what}
        if text:
            item["words"] = str(text)[:TALK_LIMIT]
        self.new_dealings.append(item)
        del self.new_dealings[:-DEALINGS_KEEP]

    def _dealings_view(self):
        """Grouped by person, players only (an NPC has no pool agent to feel about)."""
        out = {}
        for d in self.new_dealings:
            aid = self.agent_of_char.get(d["who"])
            if not aid:
                continue
            row = out.setdefault(d["who"], {"who": d["who"], "events": [], "my_affinity": self._affinity(aid)})
            row["events"].append({k: v for k, v in d.items() if k != "who"})
        for row in out.values():
            row["events"] = row["events"][-4:]
        return list(out.values())

    def _reaction_ok(self, reaction):
        due = {r["who"] for r in self._dealings_view()}
        if not due:
            return True, ""
        if not isinstance(reaction, list):
            return False, f"reaction 필수: new_dealings의 사람마다 {{who, their_attitude_to_me, delta, why}} ({sorted(due)})"
        got = set()
        for r in reaction:
            if not isinstance(r, dict):
                continue
            att, delta, why = r.get("their_attitude_to_me"), r.get("delta"), str(r.get("why") or "").strip()
            if (r.get("who") in due and isinstance(att, (int, float)) and not isinstance(att, bool) and -2 <= att <= 2
                    and isinstance(delta, (int, float)) and not isinstance(delta, bool) and -IMPRESSION_STEP <= delta <= IMPRESSION_STEP and why):
                got.add(r["who"])
        missing = sorted(due - got)
        if missing:
            return False, (f"reaction 누락 또는 형식 오류: {missing}. their_attitude_to_me는 -2~2, "
                           f"delta는 -{IMPRESSION_STEP}~{IMPRESSION_STEP}(0 가능), why 필수")
        return True, ""

    def _apply_reaction(self, reaction):
        due = {r["who"] for r in self._dealings_view()}
        for r in reaction if isinstance(reaction, list) else []:
            if not isinstance(r, dict) or r.get("who") not in due:
                continue
            aid = self.agent_of_char.get(r["who"])
            att, delta, why = r.get("their_attitude_to_me"), r.get("delta"), str(r.get("why") or "").strip()[:80]
            if not aid or not isinstance(att, (int, float)) or not isinstance(delta, (int, float)) or not why:
                continue
            rec = self.peer_reputation.setdefault(aid, {"fulfilled": 0, "broken": 0})
            before = float(rec.get("affinity", AFFINITY_START))
            delta = max(-IMPRESSION_STEP, min(IMPRESSION_STEP, float(delta)))
            rec["affinity"] = max(0.0, min(100.0, before + delta))
            rec["their_attitude"] = max(-2, min(2, round(float(att))))
            # [FIX-B61/A52] the same choice moves my in-game relationship (start 50 every game); the adapter sends it
            self.pending_game_reactions.append({"character_id": r["who"], "delta": delta})
            if delta:
                rec["why"] = (list(rec.get("why") or []) + [why])[-AFFINITY_WHY_KEEP:]
            self.affinity_dirty = True
            self._log({"event": "reaction", "cycle": self.cycle, "who": r["who"], "agent": aid[-6:],
                       "their_attitude_to_me": rec["their_attitude"], "before": before, "after": rec["affinity"], "why": why,
                       "delta": delta})   # [I17 note] logged from 2026-11-12 so a rollback can replay capped changes
        self.new_dealings = []

    # ---------- [FIX-B68] user decision 2026-11-16: review a refused proposal before the next action ----------
    # Reflexion inside a game (Textbook II ch.7): the failure signal is the refusal, the free-form feedback is the
    # partner's words and the GM's reason. Only failures the partner chose are reviewed ('refused', 'another_chosen');
    # mechanical ones (the partner's action already used, a ring that did not resolve) have nothing to review.
    # The code never says why it failed: the agent infers it; the code checks only that a review is there.
    REVIEW_REASONS = ("refused", "another_chosen")

    def _reviews_view(self):
        rows, keep = [], []
        notices = {t.get("trade_id"): t.get("gm_notices") or [] for t in self.trades_view}
        for item in self.trade_reviews_due:
            told = notices.get(item["trade"]) or []
            reason = (told[-1] or {}).get("reason") if told else None
            if reason is None:   # the GM's notice may come a moment later; give up after a cycle
                if (item.get("cycle") or 0) >= (self.cycle or 0) - 1:
                    keep.append(item)
                continue
            if reason not in self.REVIEW_REASONS:
                continue
            keep.append(item)
            row = dict(item, gm_reason=reason)
            if not row.get("their_words") and (told[-1] or {}).get("their_message"):
                row["their_words"] = str(told[-1]["their_message"])[:TALK_LIMIT]
            aid = self.agent_of_char.get(item["partner"])
            if aid:
                row["relation"] = self._affinity(aid)
            rows.append(row)
        self.trade_reviews_due = keep
        return rows

    def _review_ok(self, reviews):
        due = {r["trade"] for r in self._reviews_view()}
        if not due:
            return True, ""
        got = {r.get("trade") for r in reviews if isinstance(r, dict) and str(r.get("why_not") or "").strip()
               and str(r.get("change") or "").strip()} if isinstance(reviews, list) else set()
        missing = sorted(due - got)
        if missing:
            return False, (f"trade_review 필수: situation.trade_review_due의 거래마다 "
                           f"{{trade, why_not, change}} (누락 {missing})")
        return True, ""

    def _apply_reviews(self, reviews):
        shown = {r["trade"]: r for r in self._reviews_view()}
        for r in reviews if isinstance(reviews, list) else []:
            if not isinstance(r, dict) or r.get("trade") not in shown:
                continue
            why, change = str(r.get("why_not") or "").strip()[:120], str(r.get("change") or "").strip()[:120]
            if not why or not change:
                continue
            item = shown[r["trade"]]
            card = self.situation.cards.get(item["partner"]) or {}
            for t in reversed(card.get("trades", [])):
                if t.get("trade_id") == item["trade"]:
                    t["my_review"] = {"why_not": why, "change": change}
                    break
            self._trace("trade_review", f"거래 실패 회고(인물 {item['partner']}, {item['gm_reason']}): "
                                        f"내 추정 {why} / 바꿀 것 {change}", ok=False)
            self._log({"event": "trade_review", "cycle": self.cycle, "trade": item["trade"], "partner": item["partner"],
                       "gm_reason": item["gm_reason"], "why_not": why, "change": change})
        # reviewed or not, a refusal is asked about once: the next decision has its own
        self.trade_reviews_due = [i for i in self.trade_reviews_due if i["trade"] not in shown]

    def _impress(self, ops):
        """[FIX-B43] My own impression of another player's pool agent: up when I feel grateful or close,
        down when I feel let down or harmed. Only I see and change it; it lasts across games."""
        if not ops:
            return []
        refused = []
        for op in (ops if isinstance(ops, list) else [ops])[:IMPRESSIONS_PER_TURN]:
            if not isinstance(op, dict):
                refused.append({"op": op, "reason": "형식"})
                continue
            who, delta, why = op.get("who"), op.get("delta"), str(op.get("why") or "").strip()[:80]
            aid = self.agent_of_char.get(who) if isinstance(who, int) else None
            if aid is None or who == self._me_id():
                refused.append({"who": who, "reason": "이번 판에서 만난 플레이어(캐릭터 번호)만"})
                continue
            if not isinstance(delta, (int, float)) or isinstance(delta, bool) or not -IMPRESSION_STEP <= delta <= IMPRESSION_STEP or not why:
                refused.append({"who": who, "reason": f"delta는 -{IMPRESSION_STEP}~{IMPRESSION_STEP}, why 필수"})
                continue
            rec = self.peer_reputation.setdefault(aid, {"fulfilled": 0, "broken": 0})
            before = float(rec.get("affinity", AFFINITY_START))
            rec["affinity"] = max(0.0, min(100.0, before + float(delta)))
            rec["why"] = (list(rec.get("why") or []) + [why])[-AFFINITY_WHY_KEEP:]
            self.affinity_dirty = True
            self._log({"event": "impression", "cycle": self.cycle, "who": who, "agent": aid[-6:],
                       "before": before, "after": rec["affinity"], "why": why})
        return refused

    def affinity_report(self):
        """[FIX-B43/A35] What I tell the GM: my affinity to the pool agents I met in this game."""
        me = self._me_id()
        return {aid: round(float((self.peer_reputation.get(aid) or {}).get("affinity", AFFINITY_START)), 1)
                for cid, aid in self.agent_of_char.items() if cid != me}

    # ---------- stage 3: decision ----------

    def _request_complete(self, system, user, budget):
        # Retry the whole structured decision, never execute or splice a fragment.
        for attempt in range(1, 4):
            self._log({"event":"generation_attempt", "attempt":attempt, "budget":budget})
            try:
                result = self.client.request(system, user, budget=budget)
                self.model_calls = getattr(self, "model_calls", 0) + 1  # [FIX-B9]
                self._log({"event":"generation_accepted", "attempt":attempt, "budget":budget})
                return result
            except Exception as exc:
                code = getattr(exc, "code", None)
                body = getattr(exc, "body", {})
                if isinstance(body, dict): code = body.get("code", code)
                cut = code in ("truncated", "llm_truncated")
                self._log({"event":"generation_failed", "attempt":attempt, "budget":budget,
                           "code":code, "truncated":cut, "reason":str(exc)})
                cap = MAX_BUDGET or getattr(self, "max_budget", None) or budget
                if cut and attempt < 3 and budget < cap:
                    budget = min(cap, budget * 2)
                    continue
                if isinstance(exc, GatewayError): raise
                raise GatewayError(str(exc), "truncated" if cut else "http") from exc

    def decide(self, observation):
        self._known_at_decision = {self._item_id(i) for i in self.known_info}   # [FIX-B38]
        self.last_utterance = None
        self.last_trade = None
        allowed = observation.get("allowed_actions") or []
        phase = observation.get("phase") or "interaction"
        if not allowed:
            return FALLBACK.get(phase, ("pass", {}))
        if self.client is None:
            return self._rule_action(observation, allowed)

        started = time.time()
        budget, attempt, last, feedback = DECIDE_BUDGET, 0, "", None
        while attempt < 2:
            attempt += 1
            try:
                text, usage = self._request_complete(self._system_prompt(),
                                                  self._user_prompt(observation, feedback), budget=budget)
                self._count(usage)
            except GatewayError as exc:
                code = getattr(exc, "code", "http")
                self._log({"event": "llm_error", "cycle": self.cycle, "phase": phase,
                           "code": code, "reason": str(exc)[:200]})
                # Gateway faults are excluded from the H2 denominator.
                kind = "reasoning_cap" if code == "truncated" else "gateway"  # [FIX-B11]
                return self._fallback(observation, allowed, kind, attempt, started)
            parsed = self._parse(text)
            last = text[:300]
            if parsed is None:  # [FIX-B36] say why, instead of resending the same prompt
                feedback = {"reason": "출력이 JSON 객체 하나가 아니어서 읽을 수 없었다. 형식대로 JSON 객체 하나만 출력한다."}
                continue
            action = (parsed.get("action") or {}).get("type")
            payload = (parsed.get("action") or {}).get("payload") or {}
            ok, reason = self._validate(action, payload, observation)
            if ok and phase == "deduction":
                ok, reason = self._consistent(parsed, action, payload)
                if not ok:
                    counts = self.__dict__.setdefault("say_do_mismatches", {})
                    counts[reason] = counts.get(reason, 0) + 1
                else:   # [FIX-B44] my own expected-score reasoning, arithmetic checked
                    math_ok, math_reason = self._deduction_math_ok(parsed, action)
                    self._log({"event": "deduction_math", "cycle": self.cycle, "attempt": attempt,
                               "ok": math_ok, "reason": math_reason, "math": parsed.get("deduction_math")})
                    if not math_ok:
                        counts = self.__dict__.setdefault("deduction_math_errors", {})
                        counts[math_reason] = counts.get(math_reason, 0) + 1
                        if attempt < 2:      # say what is wrong and ask once more
                            ok, reason = False, math_reason
                        # on the last attempt the model's own decision stands (never a stand-in)
            if ok and self._dealings_view():   # [FIX-B47] react to what happened with each person
                r_ok, r_reason = self._reaction_ok(parsed.get("reaction"))
                self._log({"event": "reaction_check", "cycle": self.cycle, "attempt": attempt, "ok": r_ok, "reason": r_reason})
                if not r_ok:
                    counts = self.__dict__.setdefault("reaction_errors", {})
                    counts[r_reason[:40]] = counts.get(r_reason[:40], 0) + 1
                    if attempt < 2:
                        ok, reason = False, r_reason
            if ok and self.trade_reviews_due:   # [FIX-B68] review each refused proposal of mine
                v_ok, v_reason = self._review_ok(parsed.get("trade_review"))
                self._log({"event": "trade_review_check", "cycle": self.cycle, "attempt": attempt, "ok": v_ok, "reason": v_reason})
                if not v_ok:
                    counts = self.__dict__.setdefault("trade_review_errors", {})
                    counts[v_reason[:40]] = counts.get(v_reason[:40], 0) + 1
                    if attempt < 2:
                        ok, reason = False, v_reason
            if not ok:
                self._log({"event": "invalid_output", "cycle": self.cycle, "phase": phase,
                           "action": action, "reason": reason, "attempt": attempt})
                feedback = {"rejected_action": {"type": action, "payload": payload}, "reason": reason,  # [FIX-B36]
                            "valid_targets_now": [x.get("id") for x in observation.get("occupants") or []
                                                  if x.get("mode") != "corpse"],
                            "allowed_actions": allowed}
                continue
            payload = self._finalize(action, payload)
            self._memo(parsed.get("memory"), parsed.get("impression"))         # [FIX-B42/B43]
            if self.new_dealings:
                self._apply_reaction(parsed.get("reaction"))                   # [FIX-B47]
            if self.trade_reviews_due:
                self._apply_reviews(parsed.get("trade_review"))                # [FIX-B68]
            self.last_utterance = parsed.get("utterance")
            if action == "trade":  # [FIX-B22] a proposal is this phase's action
                payload = self._trade_proposal(payload, observation)
                self.last_trade = payload
            self._record(parsed, action, payload, observation, attempt, started, usage)
            return action, payload

        self._log({"event": "invalid_output", "cycle": self.cycle, "phase": phase,
                   "reason": "unparseable", "raw": last})
        return self._fallback(observation, allowed, "invalid_output", attempt, started)

    def _finalize(self, action, payload):
        if action == "accuse":
            # The engine ranks correct deductions by this number, fewest first.
            payload["output_tokens"] = self.output_tokens
            self.accused = True
            math_ = self._accuse_math() or {}
            self.combinations_at_accuse = self._combinations()     # [FIX-B52] research only
            self._trace("accuse", "추리 도전")   # [FIX-B37]
            self.accuse_scores = {k: math_.get(k) for k in ("score_if_wrong", "score_if_unsolved_and_alive",
                                                             "my_mission_points")}  # [FIX-B21]
        return payload

    def _record(self, parsed, action, payload, observation, attempt, started, usage):
        if observation.get("phase") == "movement":   # [FIX-B54] my talk intent for this cycle
            self.talk_intent = parsed.get("talk") is True
        held = {self._item_id(i) for i in self.known_info}
        used = [x for x in (parsed.get("evidence_used") or []) if isinstance(x, str)]
        self._log({"event": "decision", "cycle": self.cycle, "phase": observation.get("phase"),
                   "action": action, "payload": payload,
                   "evidence_used": used,
                   "ungrounded_evidence": [x for x in used if x not in held],
                   "hypothesis": parsed.get("current_hypothesis"),
                   "combinations_left": self._combinations(),   # [FIX-B52] research only, not in the prompt
                   "attempt": attempt, "fallback": None,
                   "belief_mode": self.belief_mode, "usage": usage,
                   "elapsed": round(time.time() - started, 3)})
        if parsed.get("consideration"):
            self._log({"event": "consideration", "cycle": self.cycle,
                       "context": "action", "text": str(parsed["consideration"])})

    def _fallback(self, observation, allowed, kind, attempt, started):
        # [FIX-B9] Count it. The runtime's `fallbacks` never saw these, so a game in which
        # every model call failed still reported player_fallbacks = 0.
        counts = self.__dict__.setdefault("model_fallbacks", {})
        counts[kind] = counts.get(kind, 0) + 1
        action, payload = self._rule_action(observation, allowed)
        self._log({"event": "decision", "cycle": self.cycle, "phase": observation.get("phase"),
                   "action": action, "payload": payload, "evidence_used": [],
                   "ungrounded_evidence": [], "hypothesis": None,
                   "attempt": attempt, "fallback": kind,
                   "belief_mode": self.belief_mode, "usage": None,
                   "elapsed": round(time.time() - started, 3)})
        return action, payload

    def _rule_action(self, observation, allowed):
        """No-LLM policy: never waste a phase, never gamble on accuse."""
        me = observation.get("self") or {}
        if "explore" in allowed:
            corpse = next((x for x in observation.get("occupants") or []
                           if x.get("mode") == "corpse"), None)
            return "explore", ({"target_id": corpse["id"]} if corpse else {})
        if "question" in allowed:
            living = [x for x in observation.get("occupants") or []
                      if x.get("mode") != "corpse"]
            if living:
                return "question", {"target_id": living[0]["id"], "approach": "question",
                                    "hypothesis": self._best_guess()}
            return "prepare", {}
        if "move" in allowed:
            return "move", {"room": ((me.get("room") or 0) + 1) % 8}
        return FALLBACK.get(observation.get("phase") or "interaction", ("pass", {}))

    def _best_guess(self):
        if self.belief is not None:
            remaining = self._remaining_all()
            return {k: (remaining.get(k) or [0])[0] for k in CATEGORIES}
        return {k: 0 for k in CATEGORIES}

    def _remaining_all(self):
        """[FIX-B7] BeliefState.remaining(category) needs a category; three call sites
        called it with none and would raise TypeError if reached."""
        return {c: self.belief.remaining(c) for c in CATEGORIES}

    def _deduction_math_ok(self, parsed, action):
        """[FIX-B44] deduction_math: my_win_chance (my own estimate, not checked), expected_if_accuse
        (must equal chance x score_if_correct_first + (1 - chance) x score_if_wrong), expected_if_pass
        with its assumption, and a reason when I choose the lower expected score."""
        m = parsed.get("deduction_math")
        if not isinstance(m, dict):
            return False, "deduction_math 필수: my_win_chance, expected_if_accuse, expected_if_pass, pass_assumption"
        p, ea, ep = m.get("my_win_chance"), m.get("expected_if_accuse"), m.get("expected_if_pass")
        numbers = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (p, ea, ep))
        if not numbers or not 0 <= p <= 1:
            return False, "my_win_chance는 0~1, expected_if_accuse와 expected_if_pass는 숫자"
        if not str(m.get("pass_assumption") or "").strip():
            return False, "pass_assumption 필수: 넘기면 어떻게 될지 가정(마지막 사이클이면 미해결, 아니면 다음 기회)"
        am = self._accuse_math() or {}
        win, wrong = am.get("score_if_correct_first"), am.get("score_if_wrong")
        if isinstance(win, (int, float)) and isinstance(wrong, (int, float)):
            want = p * win + (1 - p) * wrong
            if abs(ea - want) > max(1.0, 0.02 * abs(want)):
                return False, (f"expected_if_accuse 계산이 맞지 않다: my_win_chance {p} × score_if_correct_first {win} + "
                               f"(1 − {p}) × score_if_wrong {wrong} = {round(want, 2)}")
        lower = (action == "accuse" and ea < ep) or (action == "pass" and ep < ea)
        if lower and not str(m.get("choice_reason") or "").strip():
            return False, "기대 점수가 낮은 쪽을 고른다면 choice_reason에 그 이유를 쓴다"
        return True, ""

    @staticmethod
    def _consistent(parsed, action, payload):
        """[FIX-B13] An accusation kills on a miss, so it must be the model's stated conclusion.
        Rejected outputs are re-asked once; a second failure falls back to the rule policy,
        which never accuses."""
        decision = parsed.get("decision")
        if decision not in ("accuse", "pass"):
            return False, "decision_missing"
        if decision != action:
            return False, "decision_vs_action"
        if action == "accuse":
            hypothesis = parsed.get("current_hypothesis") or {}
            if any(hypothesis.get(k) != payload.get(k) for k in CATEGORIES):
                return False, "accuse_vs_hypothesis"
        return True, ""

    def _validate(self, action, payload, observation):
        allowed = observation.get("allowed_actions") or []
        if action not in allowed:
            return False, f"{action}은 이번 단계에 허용되지 않는다"
        if not isinstance(payload, dict):
            return False, "payload는 객체여야 한다"
        occupants = {x.get("id"): x for x in observation.get("occupants") or []}

        if action == "move":
            if set(payload) != {"room"} or not self._in_range(payload.get("room"), 8):
                return False, "move는 room 0~7 하나만 받는다"
        elif action in ("stay", "pass", "prepare"):
            if payload:
                return False, f"{action}에는 payload가 없다"
        elif action == "explore":
            if not set(payload) <= {"target_id"}:
                return False, "explore는 target_id 외의 필드를 받지 않는다"
            if "target_id" in payload:
                target = occupants.get(payload["target_id"])
                if target is None or target.get("mode") != "corpse":
                    return False, "explore의 target_id는 같은 방의 시체여야 한다"
        elif action == "question":
            if not set(payload) <= {"target_id", "approach", "hypothesis", "message"}:
                return False, "question은 target_id/approach/hypothesis(+message)만 받는다"
            if not self._talk_ok(payload.get("message")):
                return False, f"message는 {TALK_LIMIT}자 이하 문자열이다"
            target = occupants.get(payload.get("target_id"))
            if target is None or target.get("mode") == "corpse":
                return False, f"question 대상은 지금 같은 칸의 살아있는 인물이어야 한다(지금 같은 칸: {sorted(k for k, v in occupants.items() if v.get('mode') != 'corpse')})"
            if payload.get("approach") not in APPROACHES:
                return False, "approach는 question/interrogate/threaten/persuade 중 하나다"
            hypothesis = payload.get("hypothesis")
            if not isinstance(hypothesis, dict) or set(hypothesis) != set(CATEGORIES):
                return False, "hypothesis는 suspect/weapon/place 세 값이 모두 필요하다"
            for key in CATEGORIES:
                if not self._in_range(hypothesis[key], SIZES[key]):
                    return False, f"hypothesis.{key} 값이 범위를 벗어났다"
        elif action == "accuse":
            if not set(payload) <= set(CATEGORIES) | {"confidence", "elapsed_seconds"}:
                return False, "accuse는 suspect/weapon/place만 지정한다"
            for key in CATEGORIES:
                if not self._in_range(payload.get(key), SIZES[key]):
                    return False, f"{key} 값이 후보 범위를 벗어났다"
        elif action == "trade":  # [FIX-B22]
            if self._trade_proposal(payload, observation) is None:
                return False, getattr(self, "_trade_reason", None) or "trade 형식 오류"  # [FIX-B29]
        else:
            return False, f"알 수 없는 행동 {action}"
        return True, ""

    # ---------- stage 4: answering another player's question ----------

    def answer_question(self, question):
        """Pick the form of disclosure. The engine already fixed the amount, and
        checks it for exact equality, so neither more nor less is accepted."""
        self.last_answer_message = None
        if question.get("asker_message"):   # [FIX-B31]
            self.hear(question.get("asker_character"), "question", question["asker_message"])
        self.situation.asked_me(question.get("asker_character"), self.cycle, question.get("approach"))   # [FIX-B41]
        if question.get("asker_id") and question.get("asker_character") is not None:   # [FIX-B43]
            self.agent_of_char[question["asker_character"]] = question["asker_id"]
        self._dealing(question.get("asker_character"), f"나에게 {question.get('approach')} 방식으로 질문")   # [FIX-B47]
        options = dict(question.get("response_options") or {}, _extra_vague=question.get("extra_vague", 0))
        successes = question.get("successes", 0)
        if successes <= 0:
            return []
        if self.client is None:
            return self._required(options, successes)

        prompt = json.dumps({
            "asker_id": question.get("asker_id"),
            "approach": question.get("approach"),
            "asker_hypothesis": question.get("hypothesis"),
            "successes": successes,
            "extra_vague_slots": question.get("extra_vague", 0),   # [FIX-B24]
            "asker_message": question.get("asker_message"),          # [FIX-B31] unverified
            "me": self._me_type(),                                    # [FIX-B32] my type decides how much I disclose
            "asker_card": self.situation.view(question.get("asker_character"), me=self._me_id()),   # [FIX-B41]
            "my_affinity_to_asker": self._affinity(question["asker_id"]) if question.get("asker_id") else None,   # [FIX-B43]
            "my_notes": self.notes.view(),
            "relationship_to_asker": self.relationships.get(str(question.get("asker_character"))),
            "my_information": [self._readable(i) for i in self.known_info],
            "already_disclosed": self.disclosed,
            "response_options": options,
        }, ensure_ascii=False)
        system = RULES_PROMPT.ANSWER_SYSTEM  # [FIX-B16]
        try:
            text, usage = self._request_complete(system, prompt, budget=ANSWER_BUDGET)
            self._count(usage)
        except GatewayError as exc:
            self._log({"event": "llm_error", "cycle": self.cycle, "context": "question",
                       "code": getattr(exc, "code", "http"), "reason": str(exc)[:200]})
            counts = self.__dict__.setdefault("model_fallbacks", {})  # [FIX-B9]
            counts["answer_gateway"] = counts.get("answer_gateway", 0) + 1
            return self._required(options, successes)

        parsed = self._parse(text)
        statements = (parsed or {}).get("statements")
        if not isinstance(statements, list):
            statements = self._required(options, successes)
        message = (parsed or {}).get("message")
        self.last_answer_message = message if self._talk_ok(message) and message else None  # [FIX-B31]
        for s in statements:
            if isinstance(s, dict) and s.get("clue_id") and s["clue_id"] not in self.disclosed:
                self.disclosed.append(s["clue_id"])
        self._log({"event": "question_response", "cycle": self.cycle,
                   "asker_id": question.get("asker_id"), "approach": question.get("approach"),
                   "successes": successes, "statements": statements,
                   "held": len(self.known_info), "usage": usage})
        if parsed and parsed.get("consideration"):
            self._log({"event": "consideration", "cycle": self.cycle,
                       "context": "question", "text": str(parsed["consideration"])})
        return statements

    @staticmethod
    def _required(options, successes):
        """Mirror of the engine's disclosure tier, used when the model is unavailable."""
        definite = [{"kind": "definite", "clue_id": c} for c in (options.get("definite") or [])[:1]]
        vague = [{"kind": "ambiguous", "pair": dict(x["pair"])} for x in options.get("ambiguous") or []]  # [FIX-B23]
        vague += [{"kind": "indirect", "clue_id": x["clue_id"], "text": x["narrative_options"][0]}
                  for x in options.get("indirect") or []]
        extra = options.get("_extra_vague", 0)  # [FIX-B24] set by answer_question from the question
        if successes <= 0:
            return []
        if successes == 1:
            return vague[:1 + extra] or definite
        if successes == 2:
            return (definite + vague[:extra]) if definite else vague[:2 + extra]
        return definite + vague[:2 + extra]

    # ---------- stage 5: result intake ----------

    def ingest_result(self, result, payload=None):
        gained = []
        known = {self._item_id(i) for i in self.known_info}
        # [FIX-B38] The runner waits for the settled receipt (room searches settle at the end of the
        # phase, answers come later), and an observation in between already lists the new clues.
        # Counting "not in known_info" then gave 0 for every search and question: runs/v8-main-30c
        # agents remembered 0 clues from 2,536 questions (engine: 833) and twice the engine's wasted
        # searches, in recent_actions, room_notes and the end-of-game facts. Count against what I
        # held when I chose the action.
        before = self._known_at_decision if self._known_at_decision is not None else set(known)
        self._known_at_decision = None
        for item in result.get("acquired_information") or []:
            key = self._item_id(item)
            if key not in before and key not in gained and key in known:
                gained.append(key)
        for item in result.get("acquired_information") or []:
            key = self._item_id(item)
            if key not in known:
                self.known_info.append(item)
                known.add(key)
                gained.append(key)
            if self.belief_mode and self.belief is not None and key not in self.applied_seen:
                self.belief.apply(item, source="own")
                self.applied_seen.append(key)
        self._last_roll = result.get("roll")
        via = result.get("action") or "unknown"
        for item in result.get("acquired_information") or []:   # [FIX-B30]
            self.label(self._item_id(item), via, (payload or {}).get("target_id") if via == "question" else None)
        if result.get("answer_message"):                            # [FIX-B31]
            self.hear((payload or {}).get("target_id"), "answer", result["answer_message"])
        self._remember(result, payload or {}, gained)
        self._log({"event": "result", "cycle": self.cycle,
                   "action": result.get("action"), "outcome": result.get("outcome"),
                   "roll": result.get("roll"), "acquired": gained,
                   "held": len(result.get("held_information_ids") or self.known_info)})

    def _remember(self, result, payload, gained):
        """[FIX-B4] Keep my own action history and what each room still holds for me.
        An explore that rolled successes but returned nothing means the room has no clue
        left that I do not already hold (clues are depleted per player)."""
        action = result.get("action")
        if action in ("move", "stay", "pass"):
            return
        roll = result.get("roll") or {}
        entry = {"cycle": result.get("cycle_id", self.cycle), "action": action,
                 "room": result.get("room"), "outcome": result.get("outcome"),
                 "successes": roll.get("successes"), "new_clues": len(gained)}
        if action == "question":
            entry.update(target=payload.get("target_id"), approach=payload.get("approach"))
        self.action_log.append(entry)
        got = self._types(gained)                                         # [FIX-B37]
        if action == "explore":
            self._trace("explore", f"탐색 칸 {result.get('room')}: 성공 {roll.get('successes')}, "
                        f"새 단서 {got or '없음'}" + (f", 칸에 남은 단서 {result['room_remaining']}"
                                                      if "room_remaining" in result else ""), ok=bool(gained))
        elif action == "question":
            self._trace("question", f"질문 → 인물 {payload.get('target_id')} ({payload.get('approach')}): "
                        f"{result.get('outcome')}, 새 단서 {got or '없음'}", ok=bool(gained))
            target = payload.get("target_id")                                  # [FIX-B41]
            self.situation.question(target, {"cycle": entry["cycle"], "approach": payload.get("approach"),
                                             "outcome": result.get("outcome"), "new_clues": len(gained)})
            self.situation.gave_me(target, self._type_keys(gained), entry["cycle"], "질문 답변")
            self._dealing(target, f"내 질문({payload.get('approach')})에 답함: {result.get('outcome')}, 새 단서 {len(gained)}개")   # [FIX-B47]
        else:
            self._trace(action or "other", f"{action}: {result.get('outcome')}, 새 단서 {got or '없음'}",
                        ok=bool(gained) if action in ("prepare",) else None)
        if action == "explore" and result.get("room") is not None and payload.get("target_id") is None:
            note = self.room_notes.setdefault(result["room"], {"explores": 0, "found": 0, "exhausted": False})
            note["explores"] += 1
            note["found"] += len(gained)
            if "room_remaining" in result:  # [FIX-B16] the engine now reports it (FIX-A9)
                note.update(room_remaining=result["room_remaining"], checked_cycle=self.cycle,
                            exhausted=result["room_remaining"] == 0)
            elif (roll.get("successes") or 0) > 0 and not result.get("acquired_information"):
                note["exhausted"] = True

    # ---------- [FIX-B1] game end and reflection ----------

    def game_stats(self, resolution):
        """Deterministic summary of this game, stored for the next one (no LLM)."""
        explores = [a for a in self.action_log if a["action"] == "explore"]
        questions = [a for a in self.action_log if a["action"] == "question"]
        math_ = self._accuse_math() or {}
        stalled = best = 0
        previous = None
        for cycle in sorted(self.clues_by_cycle):
            held = self.clues_by_cycle[cycle]
            stalled = stalled + 1 if previous is not None and held <= previous else 0
            best, previous = max(best, stalled), held
        me = (self.last_observation.get("self") or {})
        return {"resolution": resolution, "cycles_played": self.cycle,
                "character_id": me.get("character_id"), "job": (me.get("job") or {}).get("name"),
                "mbti": me.get("mbti"),
                "accused": self.accused, "died": me.get("mode") == "corpse" or None,
                "combinations_at_accuse": self.combinations_at_accuse,
                "scores_at_accuse": getattr(self, "accuse_scores", None),            # [FIX-B21]
                "final_scores": {k: math_.get(k) for k in ("score_if_wrong", "score_if_unsolved_and_alive",
                                                            "my_mission_points")},
                "combinations_left": self._combinations(),     # [FIX-B52] research only
                "clues_held": len(self.known_info),
                "explores": len(explores),
                "wasted_explores": sum(1 for a in explores if (a["successes"] or 0) > 0 and a["new_clues"] == 0),
                "questions": len(questions),
                "question_clues": sum(a["new_clues"] for a in questions),
                "stalled_cycles": best,
                # [FIX-B37] inputs of the Reflexion/ExpeL reflection
                "definite_clues": sum(1 for i in self.known_info if isinstance(i, dict) and i.get("kind") == "definite"),
                "settled_trades": sum(1 for t in self.trade_log if t.get("status") == "settled"),
                "trajectory": [x["text"] for x in self.trajectory],
                "final_notes": [x["text"] for x in self.notes.items],              # [FIX-B42]
                "memory_ops": sum(len(x["applied"]) for x in self.notes.log),
                "situation": {"characters_with_evidence": sum(1 for c in self.situation.cards.values() if c["holdings"])},
                "subtask_contrast": self._contrast()}

    # ---------- [FIX-B37] Reflexion trial log ----------
    def _trace(self, kind, text, ok=None):
        self.trajectory.append({"cycle": self.cycle, "kind": kind, "ok": ok, "text": f"c{self.cycle} {text}"})
        if len(self.trajectory) > TRAJECTORY_LIMIT:
            self.trajectory = self.trajectory[-TRAJECTORY_LIMIT:]

    def _types(self, ids):
        held = {self._item_id(i): i for i in self.known_info if isinstance(i, dict)}
        out = []
        for cid in ids or []:
            info = held.get(cid) or {}
            kind = info.get("category") if info.get("kind") == "definite" else info.get("type")
            out.append(TYPE_KO.get(kind, "단서"))
        return "+".join(out)

    @staticmethod
    def _spec_types(spec):
        out = []
        for x in spec or []:
            out.append(x.get("type") if isinstance(x, dict) else x)
        return out

    def _contrast(self):
        """ExpeL contrasts successful and failed trials. No one has won a game yet, so the unit
        is the sub-task (the textbook allows it): explores, questions and trades that paid or not."""
        counts, good, bad = {}, [], []
        for x in self.trajectory:
            if x["ok"] is None:
                continue
            c = counts.setdefault(x["kind"], {"success": 0, "failure": 0})
            c["success" if x["ok"] else "failure"] += 1
        for kind in counts:
            good += [x["text"] for x in self.trajectory if x["kind"] == kind and x["ok"]][-2:]
            bad += [x["text"] for x in self.trajectory if x["kind"] == kind and x["ok"] is False][-2:]
        return {"counts": counts, "successes": good, "failures": bad}

    def reflect(self, entry):
        """One LLM call at the start of the next game (the engine refuses calls after a
        game has ended). [FIX-B37] Textbook II ch.7: Reflexion (trajectory -> plan, checked
        against my last reflections) and ExpeL (AGREE/EDIT/REMOVE/ADD on my insight list from
        contrasting sub-task successes and failures), in one call.
        Returns {"plan", "plan_check", "operations", "tactics", "risk"} or None."""
        if self.client is None or not entry:
            return None
        system = ("너는 방금 끝난 살인 미스터리 게임을 돌아보는 플레이어다. JSON 하나만 출력한다.\n"
                  "[1단계 회고(Reflexion)] trajectory는 이번 판 너의 사이클별 위치·행동·결과 기록이다. "
                  "환경을 요약하지 말고 네가 택한 전략과 경로를 돌아본다. 무엇을 했고 무엇을 빠뜨렸는지 구체적인 행동"
                  "(몇 사이클에 어떤 행동을, 어떤 유형의 단서·상대를 두고)을 짚어, 그 실수를 피하는 새 계획을 세운다. "
                  "예: A와 B는 했지만 C를 빠뜨렸다면 C를 언제 어떻게 할지 계획한다.\n"
                  # [FIX-B46] what the scoreboard is; what to learn from it is the agent's own call
                  "official_feedback.scoreboard는 GM이 나에게만 알려준 이번 판 결과다. 나 말고는 익명이고, 각 행은 결과 유형"
                  "(정답, 오답 사망, 미해결 생존, 남이 맞혀 생존)과 점수 구성(기본 점수, 미션 점수, 미션 무효)이다. "
                  "점수가 같으면 등수도 같고, tied는 그 등수를 함께 가진 인원이다(공동 순위).\n"
                  "previous_reflections가 있으면 먼저 그 계획을 이번 판에서 따랐는지, 그 결과 지표(남은 조합, 확실 단서, 성사 거래, 점수)가 "
                  "그 계획을 쓴 판보다 나아졌는지 판단해 plan_check에 쓴다.\n"
                  "[2단계 통찰 갱신(ExpeL)] subtask_contrast의 성공 사례와 실패 사례를 대조하고, 기존 통찰 목록(insights, 번호와 점수)을 보고 연산을 고른다. "
                  "AGREE 번호: 기존 통찰이 이번 판에도 강하게 맞았다. EDIT 번호 + 새 문장: 일반적이지 않거나 보완할 수 있다. "
                  "REMOVE 번호: 다른 통찰과 모순·중복되거나 이번 판에서 틀린 것으로 드러났다. ADD + 문장: 기존과 다르고 다른 판에도 쓸 새 통찰(최대 2개). "
                  "언급하지 않은 통찰은 그대로 남는다. 통찰은 다른 사건·다른 직업에서도 쓸 수 있는 일반적이고 높은 수준의 규칙으로 쓴다.\n"
                  "너의 성격(me.mbti, me.tendencies)은 모든 게임에서 같다. 계획과 통찰은 네 성격의 장점을 살리고 약점을 보완하는 방향으로 쓰고, "
                  "성격을 버리거나 반대 성격처럼 행동하라는 내용(예: I가 '더 적극적으로 나서라')은 쓰지 않는다.\n"
                  # [FIX-B43] T's fork is learned, not fixed
                  "T라면 plan에 다음 판에서 갈 길(단기 실리 / 교류)과 그 이유를 이번 판 경험으로 밝힌다. "
                  # [FIX-B49]
                  "plan은 네 성향(me.tendencies)이 중요하게 여기는 것에 비추어 쓴다.\n"   # [FIX-B57] no per-type prescription
                  "사건 정답·인물 이름·인물 번호·방 번호처럼 이번 사건에만 해당하는 사실은 쓰지 마라. 다음 게임은 다른 사건이고 다른 캐릭터를 맡을 수 있다.\n"
                  '{"plan_check": "지난 계획의 이행과 효과 1~2문장(지난 회고가 없으면 빈 문자열)", '
                  '"plan": "다음 게임 계획 2~3문장, 구체적인 행동으로", '
                  '"operations": [{"op": "AGREE", "no": 1}, {"op": "EDIT", "no": 2, "text": "..."}, {"op": "REMOVE", "no": 3}, {"op": "ADD", "text": "..."}], '
                  '"tactics": [아래 목록에서 0~3개], "risk": -1|0|1}\n'
                  f"tactics 목록: {', '.join(TACTIC_NAMES)}\n"
                  "risk: 다음 게임에서 고발을 더 과감히(+1), 그대로(0), 더 신중히(-1).")
        stats = dict(entry.get("stats") or {})
        trajectory = stats.pop("trajectory", None) or []
        contrast = stats.pop("subtask_contrast", None)
        user = json.dumps({"me": self._me_type(),
                           "goal": "범인·흉기·장소를 남보다 먼저 정확히 고발해 맞히는 것. 아무도 못 맞히면 생존자 모두 손해를 본다.",
                           "this_game": stats, "official_feedback": entry.get("feedback"),   # [FIX-B46] incl. scoreboard
                           "game_facts": entry.get("lessons"), "trajectory": trajectory,
                           "subtask_contrast": contrast,
                           "my_notes_at_end": stats.pop("final_notes", None),          # [FIX-B42]
                           "previous_reflections": entry.get("previous_reflections") or [],
                           "insights": entry.get("insights") or []}, ensure_ascii=False)
        try:
            text, usage = self._request_complete(system, user, budget=REFLECT_BUDGET)
            self._count(usage)
        except Exception as exc:
            self._log({"event": "reflection_failed", "reason": str(exc)[:200]})
            return None
        parsed = self._parse(text) or {}
        plan = str(parsed.get("plan") or "").strip()[:500]
        operations = [op for op in parsed.get("operations") or [] if isinstance(op, dict)][:8]
        tactics = [t for t in parsed.get("tactics") or [] if t in TACTIC_NAMES][:3]
        risk = parsed.get("risk") if parsed.get("risk") in (-1, 0, 1) else 0
        result = {"plan": plan, "plan_check": str(parsed.get("plan_check") or "").strip()[:400],
                  "operations": operations, "tactics": tactics, "risk": risk}
        self._log({"event": "reflection", "game_id": entry.get("game_id"), **result})
        return result if plan or operations else None

    # ---------- stage 6: negotiation (not an action; no phase cost) ----------

    def _occupant(self, x):
        """[FIX-B20] Who is in my room, and for players: which pool agent (short tag), its public
        trade record over all games, and how it treated me in our own past trades."""
        out = {"id": x.get("id"), "name": x.get("name"), "mode": x.get("mode"), "job": x.get("job")}
        aid = x.get("agent_id")
        if aid:   # [FIX-B30] the engine shows no record: only what I remember of that pool agent
            self.agent_of_char[x.get("id")] = aid
            out.update(agent_tag=aid[-6:],
                       # [FIX-B43] my own feeling about that pool agent (A34: the engine keeps none)
                       my_affinity=self._affinity(aid),
                       they_got_from_me=self.gave_to.get(aid, []), i_got_from_them=self.got_from.get(aid, []))
        out["this_game_with_me"] = self.situation.view(x.get("id"), me=self._me_id())   # [FIX-B40/B41]
        return out

    # ---------- [FIX-B30/B31] my own memory: provenance, trades, talk ----------

    def label(self, clue_id, via, from_character=None):
        if clue_id is None:
            return
        current = self.provenance.get(clue_id)
        if current is None or current.get("via") in ("unknown", None) or via == "trade":
            self.provenance[clue_id] = {"via": via, "from_character": from_character, "cycle": self.cycle}

    def _my_info(self):
        out = []
        for i in self.known_info:
            item = self._readable(i)
            src = self.provenance.get(self._item_id(i))
            if src:
                item["via"] = src.get("via")
                if src.get("from_character") is not None:
                    item["from_character"] = src["from_character"]
            out.append(item)
        return out

    def hear(self, from_character, context, text):
        if not isinstance(text, str) or not text.strip():
            return
        claim = {"cycle": self.cycle, "from_character": from_character, "context": context, "text": text[:TALK_LIMIT]}
        self.situation.heard(from_character, self.cycle, context, text[:TALK_LIMIT])   # [FIX-B41]
        self._dealing(from_character, f"{context}에서 내게 한 말", text)                 # [FIX-B47]
        if claim not in self.heard_claims:
            self.heard_claims.append(claim)
            self.heard_claims = self.heard_claims[-400:]   # raw log; the prompt reads the cards (B41)
            self._log({"event": "claim_heard", **claim})

    @staticmethod
    def _talk_ok(message):
        return message is None or (isinstance(message, str) and len(message) <= TALK_LIMIT)

    def record_trade_result(self, view, me):
        """A trade of mine closed this cycle. [FIX-B62] user decision 2026-11-12: code never judges for the
        agent. Until B62 this compared what I received with the declaration and wrote "honest"/"deceived"
        into my memory, my dealings and my per-partner record. Now only the facts are kept (what was
        declared or requested, what arrived); whether the partner kept its word is mine to decide."""
        partner = view["recipient"] if view.get("proposer") == me else view.get("proposer")
        partner_character = view.get("recipient_character") if view.get("proposer") == me else view.get("proposer_character")
        entry = {"cycle": view.get("cycle"), "partner_character": partner_character, "status": view.get("status"),
                 "offer_types": view.get("offer_types"), "requested": view.get("requested"),
                 "i_was": "proposer" if view.get("proposer") == me else "recipient"}
        if view.get("status") == "settled":
            spec = view.get("requested") if view.get("proposer") == me else view.get("offer_types")
            self.gave_to.setdefault(partner, []).extend(view.get("gave_ids") or [])
            self.got_from.setdefault(partner, []).extend(view.get("received_ids") or [])
            for cid in view.get("received_ids") or []:
                self.label(cid, "trade", partner_character)
            entry.update(received=view.get("received_ids"), gave=view.get("gave_ids"),
                         promised_to_me="+".join(TYPE_KO.get(t, str(t)) for t in self._spec_types(spec)),
                         received_types=self._types(view.get("received_ids") or []) or "없음")
        offer = "+".join(TYPE_KO.get(t, str(t)) for t in self._spec_types(view.get("offer_types")))
        want = "+".join(TYPE_KO.get(t, str(t)) for t in self._spec_types(view.get("requested")))
        text = (f"거래({'내가 제안' if entry['i_was'] == 'proposer' else '제안받음'}, 상대 인물 {partner_character}): "
                f"제안자 제공 {offer or '?'} / 요청 {want or '?'} → {view.get('status')}")
        if view.get("status") == "settled":
            text += f", 나에게 약속된 것 {entry['promised_to_me'] or '?'} / 실제로 받은 것 {entry['received_types']}"
        self._trace("trade", text, ok=view.get("status") == "settled")      # [FIX-B37]
        their_words = next((m.get("text") for m in reversed(view.get("messages") or [])
                            if m.get("from_character") == partner_character and m.get("text")), None)
        tid = str(view.get("trade_id") or "")[-8:]
        if entry["i_was"] == "proposer" and view.get("status") == "refused" and tid:   # [FIX-B68] reviewed next decision
            self.trade_reviews_due.append({"trade": tid, "partner": partner_character, "cycle": view.get("cycle", self.cycle),
                                           "i_offered": offer or "?", "i_requested": want or "?",
                                           **({"their_words": str(their_words)[:TALK_LIMIT]} if their_words else {})})
        self.situation.trade(partner_character, {                            # [FIX-B41]
            "trade_id": tid,                                                 # [FIX-B68] my review attaches here
            "cycle": view.get("cycle", self.cycle), "i_was": entry["i_was"], "status": view.get("status"),
            "offer": self._spec_types(view.get("offer_types")), "requested": view.get("requested"),
            **({"their_words": str(their_words)[:TALK_LIMIT]} if their_words else {}),   # [FIX-B47] e.g. a refusal's reason
            **({"promised_to_me": entry["promised_to_me"], "received": entry["received_types"]}
               if view.get("status") == "settled" else {})})
        outcome = (f"성사 — 약속된 것 {entry['promised_to_me'] or '?'}, 받은 것 {entry['received_types']}"
                   if view.get("status") == "settled" else view.get("status"))
        self._dealing(partner_character, f"거래({'내 제안' if entry['i_was'] == 'proposer' else '상대 제안'}) 결과: {outcome}")   # [FIX-B47]
        if entry["i_was"] == "recipient":   # their declared offer passed the engine's possession check
            self.situation.offered(partner_character, self._spec_types(view.get("offer_types")), view.get("cycle", self.cycle))
        if view.get("status") == "settled":
            self.situation.gave_me(partner_character, self._type_keys(view.get("received_ids")), view.get("cycle", self.cycle), "거래")
            self.situation.got_from_me(partner_character, self._type_keys(view.get("gave_ids")), view.get("cycle", self.cycle))
        self.trade_log.append(entry)
        self.trade_log = self.trade_log[-40:]   # [FIX-B40] per-partner records need the whole game
        self._log({"event": "trade_closed", **entry})

    def _full_clues(self):
        return {i.get("id"): i for i in self.known_info if i.get("kind") in ("definite", "indirect")}

    @staticmethod
    def spec_matches(info, item):
        """[FIX-B29] Mirror of the engine's Rules.type_matches ({"type", "exclude"})."""
        kind, ex = item["type"], set(item.get("exclude") or [])
        if kind in ("suspect", "weapon", "place"):
            return info.get("kind") == "definite" and info.get("category") == kind and info.get("candidate") not in ex
        if info.get("kind") != "indirect" or info.get("type") != kind:
            return False
        if kind == "character_room":
            return info.get("character") not in ex
        if kind == "weapon_room":
            return info.get("weapon") not in ex
        return info.get("character") not in ex and info.get("other") not in ex

    @classmethod
    def assign(cls, infos, items):
        """[FIX-B24] One distinct clue per requested item (mirror of the engine's assign_typed)."""
        import itertools
        if len(infos) > 12:
            infos = [x for x in infos if any(cls.spec_matches(x, i) for i in items)]
        for combo in itertools.permutations(infos, len(items)):
            if all(cls.spec_matches(x, item) for x, item in zip(combo, items)):
                return [x["id"] for x in combo]
        return None

    def _items(self, raw, declared=False):
        """[FIX-B29] 1-3 items {"type", "exclude"} (declared offers carry no exclusions)."""
        if not isinstance(raw, list) or not 1 <= len(raw) <= 3:
            return None
        out = []
        for item in raw:
            if isinstance(item, str):
                item = {"type": item}
            if not isinstance(item, dict) or item.get("type") not in TRADE_TYPES:
                return None
            ex = [] if declared else (item.get("exclude") or [])
            if not isinstance(ex, list) or len(set(ex)) != len(ex) or \
                    not all(self._in_range(v, TYPE_RANGE[item["type"]]) for v in ex):
                return None
            out.append({"type": item["type"], "exclude": sorted(ex)})
        return out

    def _offer_ids(self, raw):
        ids = raw if isinstance(raw, list) else [raw] if isinstance(raw, str) else None
        full = self._full_clues()
        if not ids or len(ids) > 3 or len(set(ids)) != len(ids) or any(i not in full for i in ids):
            return None
        return list(ids)

    def _trade_proposal(self, trade, observation):
        """[FIX-B29] The model's trade action -> a trades/propose body, or None (reason kept for the re-ask)."""
        reason = None
        living = {x.get("id") for x in observation.get("occupants") or [] if x.get("mode") != "corpse"}
        if not isinstance(trade, dict):
            reason = "trade는 객체여야 한다"
        elif not set(trade) <= {"target_id", "offer_types", "offer_ids", "requested", "message"}:
            reason = "trade는 target_id, offer_types, offer_ids, requested, message만 받는다"
        elif trade.get("target_id") not in living:
            reason = "target_id는 같은 칸의 살아있는 인물이어야 한다"
        else:
            declared = self._items(trade.get("offer_types"), declared=True)
            items = self._items(trade.get("requested"))
            offer = self._offer_ids(trade.get("offer_ids"))
            if declared is None:
                reason = "offer_types: 줄 유형 1~3개 (suspect/weapon/place/character_room/weapon_room/companion)"
            elif items is None:
                reason = "requested: 받을 항목 1~3개, 각 {\"type\": 유형, \"exclude\": [번호]}"
            elif offer is None or len(offer) != len(declared):
                reason = "offer_ids: 실제로 넘길 내 단서 ID를 offer_types와 같은 개수로"
            elif self.assign(list(self._full_clues().values()), declared) is None:
                reason = "offer_types: 선언한 유형마다 그 유형의 단서를 실제로 갖고 있어야 한다"
            elif not self._talk_ok(trade.get("message")):
                reason = f"message는 {TALK_LIMIT}자 이하 문자열이다"
        self._trade_reason = reason
        if reason:
            self._log({"event": "invalid_trade", "cycle": self.cycle, "reason": reason, "trade": trade})
            return None
        # the engine takes a declaration as type names (exclusions belong to requests only)
        body = {"target_id": trade["target_id"], "offer_types": [x["type"] for x in declared], "offer_ids": offer, "requested": items}
        if trade.get("message"):
            body["message"] = trade["message"]
        return body

    def decide_trade(self, trade, observation, context=None, others=None):
        for t in [trade] + list(others or []):
            self._dealing(t.get("proposer_character"), "나에게 거래를 제안")   # [FIX-B47]
        return self._decide_trade(trade, observation, context, others)

    def _decide_trade(self, trade, observation, context=None, others=None):
        """[FIX-B29] Answer an incoming declared-type proposal: accept (seal my clues for their request,
        the exchange happens at once), counter (my declaration + request + sealed clues) or refuse."""
        mine = list(self._full_clues().values())
        items = self._items(trade.get("requested")) or []
        partner = trade.get("proposer")
        everyone = [trade] + list(others or [])   # [FIX-B65] all proposals to me this round
        for t in everyone:
            for m in t.get("messages") or []:
                if m.get("from_character") == t.get("proposer_character"):
                    self.hear(m.get("from_character"), "trade", m.get("text"))
            # [FIX-B41] a declared offer reaches me only after the engine checked the proposer holds each type
            self.situation.offered(t.get("proposer_character"), self._spec_types(t.get("offer_types")), self.cycle)
        if self.client is None:
            return dict(self.respond_to_trade(trade), trade_id=trade.get("trade_id"))
        prompt = json.dumps({
            "incoming_trade": {"from_character": trade.get("proposer_character"), "agent_tag": (partner or "")[-6:],
                               "they_offer_types": trade.get("offer_types"), "they_request": items,
                               "messages": trade.get("messages") or []},
            "cycle": self.cycle, "remaining_cycles": observation.get("remaining_cycles"),
            "me": {"character_id": (observation.get("self") or {}).get("character_id"),
                   "mbti": (observation.get("self") or {}).get("mbti"),
                   "tendencies": self._tendencies((observation.get("self") or {}).get("mbti")),
                   "trade_proposals": (observation.get("self") or {}).get("trade_proposals")},   # [FIX-B53]

            "relationship_to_them": self.relationships.get(str(trade.get("proposer_character"))),
            "my_affinity_to_them": self._affinity(partner) if partner else None,          # [FIX-B43]
            "they_got_from_me": self.gave_to.get(partner, []), "i_got_from_them": self.got_from.get(partner, []),
            # [FIX-B65] the other proposals to me now; choose_ref picks which one an accept/counter answers
            "other_proposals_to_me": [{"ref": t.get("trade_id", "")[-8:], "from_character": t.get("proposer_character"),
                                       "agent_tag": (t.get("proposer") or "")[-6:], "they_offer_types": t.get("offer_types"),
                                       "they_request": self._items(t.get("requested")), "messages": t.get("messages") or [],
                                       "my_affinity_to_them": self._affinity(t.get("proposer")) if t.get("proposer") else None,
                                       "clues_fitting_their_request": self.assign(mine, self._items(t.get("requested")) or [])
                                       if self._items(t.get("requested")) else None}
                                      for t in everyone[1:]] or None,
            "my_information": self._my_info(),
            "clues_fitting_their_request": self.assign(mine, items) if items else None,
            "belief": self.belief.summary() if self.belief is not None else None,
            "partner_card": self.situation.view(trade.get("proposer_character"), me=self._me_id()),   # [FIX-B41]
            "my_notes": self.notes.view(),                                                          # [FIX-B42]
            "if_you_accept": context or {},
            "sub_missions": self._missions(),
            "experience": self.experience,
        }, ensure_ascii=False)
        full = self._full_clues()
        for attempt in (1, 2):
            try:
                text, usage = self._request_complete(RULES_PROMPT.TRADE_SYSTEM, prompt, budget=TRADE_BUDGET)
                self._count(usage)
            except GatewayError as exc:
                self._log({"event": "llm_error", "cycle": self.cycle, "context": "trade", "reason": str(exc)[:200]})
                break
            parsed = self._parse(text) or {}
            decision = parsed.get("decision")
            picked = next((t for t in everyone[1:] if parsed.get("choose_ref") and
                           str(t.get("trade_id", "")).endswith(str(parsed.get("choose_ref")))), trade)   # [FIX-B65]
            items = self._items(picked.get("requested")) or []
            offer = self._offer_ids(parsed.get("offer_ids"))
            message = parsed.get("message") if self._talk_ok(parsed.get("message")) else None
            out = None
            if decision == "refuse":
                out = {"decision": "refuse"}
            elif decision == "accept" and offer and len(offer) == len(items) and self.assign(mine, items) is not None:
                out = {"decision": "accept", "offer_ids": offer}
            elif decision == "counter" and offer and (((observation.get("self") or {}).get("trade_proposals") or {}).get("left", 1) > 0):   # [FIX-B53]
                declared = self._items(parsed.get("offer_types"), declared=True)
                wanted = self._items(parsed.get("requested"))
                if declared and wanted and len(offer) == len(declared) and self.assign(mine, declared) is not None:
                    out = {"decision": "counter", "offer_ids": offer, "offer_types": [x["type"] for x in declared],
                           "requested": wanted}
            if out is None:
                self._log({"event": "invalid_trade_response", "cycle": self.cycle, "attempt": attempt,
                           "raw": str(text)[:200]})
                continue
            if message:
                out["message"] = message
            honest = None
            if out["decision"] == "accept":
                honest = self.assign([full[i] for i in offer], items) is not None
            out["trade_id"] = picked.get("trade_id")
            self._log({"event": "trade_response", "cycle": self.cycle, "trade_id": picked.get("trade_id"),
                       "decision": out["decision"], "sealed_honest": honest, "proposals_seen": len(everyone),
                       "consideration": str(parsed.get("consideration", ""))[:300]})
            self._memo(parsed.get("memory"), parsed.get("impression"))         # [FIX-B42/B43]
            self.last_utterance = parsed.get("utterance")
            return out
        counts = self.__dict__.setdefault("model_fallbacks", {})
        counts["trade_response"] = counts.get("trade_response", 0) + 1
        return dict(self.respond_to_trade(trade), trade_id=trade.get("trade_id"))

    def decide_gm_absent(self, observation):
        """[FIX-B66] The GM's link is cut (engine A57): wait for it or call a waiting pool agent as the new GM."""
        view = observation.get("gm_absent") or {}
        if self.client is None:
            return {"choice": "wait"}
        me = observation.get("self") or {}
        prompt = json.dumps({"gm_absent": view, "cycle": observation.get("cycle_id"),
                             "remaining_cycles": observation.get("remaining_cycles"),
                             "me": {"character_id": me.get("character_id"), "mbti": me.get("mbti"),
                                    "tendencies": self._tendencies(me.get("mbti"))},
                             "belief": self.belief.summary() if self.belief is not None else None,
                             "my_notes": self.notes.view(), "experience": self.experience}, ensure_ascii=False)
        try:
            text, usage = self._request_complete(RULES_PROMPT.GM_ABSENT_SYSTEM, prompt, budget=TRADE_BUDGET)
            self._count(usage)
            parsed = self._parse(text) or {}
        except GatewayError as exc:
            self._log({"event": "llm_error", "cycle": self.cycle, "context": "gm_absent", "reason": str(exc)[:200]})
            parsed = {}
        choice = parsed.get("choice") if parsed.get("choice") in ("wait", "appoint") else None
        self._log({"event": "gm_absent_choice", "cycle": self.cycle, "round": view.get("round"), "choice": choice,
                   "consideration": str(parsed.get("consideration", ""))[:300]})
        if choice is None:
            counts = self.__dict__.setdefault("model_fallbacks", {})
            counts["gm_absent"] = counts.get("gm_absent", 0) + 1
            return {"choice": "wait"}
        out = {"choice": choice}
        if parsed.get("message") and self._talk_ok(parsed.get("message")):
            out["message"] = parsed["message"]
        return out

    def respond_to_trade(self, trade):
        """Rule fallback: accept with clues that fit the request, otherwise refuse."""
        items = self._items(trade.get("requested")) or []
        ids = self.assign(list(self._full_clues().values()), items) if items else None
        decision = {"decision": "accept", "offer_ids": ids} if ids else {"decision": "refuse"}
        self._log({"event": "trade_response", "cycle": self.cycle,
                   "trade_id": trade.get("trade_id"), "decision": decision["decision"]})
        return decision

    # ---------- helpers ----------

    @staticmethod
    def _in_range(value, size):
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < size

    @staticmethod
    def _item_id(item):
        return item.get("id") if isinstance(item, dict) else item

    @staticmethod
    def _readable(item):
        if not isinstance(item, dict):
            return {"id": item}
        keep = ("id", "kind", "type", "category", "candidate", "text", "excludes",   # [FIX-B4] keep type
                "character", "weapon", "room", "other")
        return {k: item[k] for k in keep if k in item}

    @staticmethod
    def _parse(text):
        if not isinstance(text, str):
            return None
        body = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(body[start:end + 1])
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _log(self, record):
        try:
            from research_trace import emit
            emit("agent_journal", record=record)
        except ImportError:
            pass
        record.update({"agent_id": self.agent_id,
                       "schema_version": SCHEMA_VERSION,
                       "ts": round(time.time(), 3)})
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
