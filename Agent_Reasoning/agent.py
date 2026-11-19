"""LLM reasoning agent for competitive-v1.3. One observation in, one action out.

The engine owns rules, dice and disclosure; this file only chooses. Payload
shapes follow Game-engine/competitive.py `Rules.action` exactly, since a wrong
field is rejected as `invalid_input` and wastes the phase.

A belief_mode switch exists for the H2 comparison.
  True  -> structured belief (remaining candidates + entropy) goes into the prompt
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
# points (where to stand, how to ask, what to disclose, how to trade and talk, whom to believe, when
# to accuse). It is NOT an ability: rolls, scores and relationship numbers come from the job and the
# rules, never from the type. Each cost below is what the leaning itself tends to bring about.
MBTI_TENDENCY = {
    "E": "E: 사람이 있는 칸으로 가서 묻고 교섭하며 관계를 넓힌다. 담화를 적극적으로 쓰고, 주고받는 데 거리낌이 적다. "
         "그래서 질문을 많이 하는 만큼 관심사가 드러나기 쉽다",
    "I": "I: 사람이 적은 칸에서 탐색하고, 질문은 꼭 필요한 상대에게만 한다. 공개와 말을 아끼고 담화는 짧다. "
         "그래서 관계가 느리게 쌓이고 남에게서 오는 정보가 적다",
    "S": "S: 직접 탐색해 얻은 원문 단서를 믿고, 확실 단서로 후보가 좁혀질 때까지 고발을 미룬다. 확인되지 않은 말은 믿지 않는다. "
         "그래서 확실하지만 느리다",
    "N": "N: 간접 단서·조합 배제·담화에서 패턴을 읽어 결론을 끌어내고, 확신이 서면 남보다 먼저 고발한다. 가설은 열린 후보로 넓게 던진다. "
         "그래서 빠르지만 틀릴 위험을 더 진다",
    "T": "T: 손익으로 판단한다. 받을 정보가 줄 정보보다 값질 때 내주고, 조건을 내거는 설득을 쓴다. 관계보다 근거를 본다. "
         "그래서 관계를 쌓는 데 덜 투자한다",
    "F": "F: 관계로 판단한다. 관계가 좋은 상대에게는 손해를 감수하고 더 내주고 정직하게 교섭하며, 심문·협박은 피한다. "
         "나를 속인 상대는 오래 기억한다. 그래서 관계가 나쁜 상대와는 얻는 것이 적다",
    "J": "J: 계획을 세워 준비(prepare)를 활용하고, 선언한 교섭은 지킨다. 제안은 분명하게, 응답은 되도록 수락 아니면 거절로 한다. "
         "그래서 예측 가능하고 흥정의 여지가 적다",
    "P": "P: 상황에 맞춰 계획을 바꾸고 역제안으로 조건을 흥정한다. 필요하면 선언과 다른 단서를 넘기거나 담화로 떠볼 수도 있다. "
         "그래서 유연하지만 신뢰를 잃기 쉽다",
}
MBTI_TENSION = {
    "ES": "E이면서 S: 사람을 찾아가되, 말보다 직접 탐색해 확인한 단서를 주고받으며 관계를 쌓는 편이다",
    "FP": "F이면서 P: 흥정은 유연하게 하되, 관계가 좋은 상대를 속이는 일은 꺼린다",
    "NJ": "N이면서 J: 결론은 빨리 세우되, 고발은 계획한 시점과 근거가 맞을 때 한다",
    "TJ": "T이면서 J: 손익으로 따지되, 한 번 선언한 거래는 손해가 나도 지킨다",
    "IN": "I이면서 N: 드러나지 않게 묻고, 받은 간접 단서와 조합 배제로 혼자 결론을 좁힌다",
    "SF": "S이면서 F: 관계가 좋은 상대에게만 캐묻고, 나머지는 직접 탐색으로 채운다",
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
        combos = self.belief.combinations()  # [FIX-B23] after pair exclusions
        out = {"combinations_left": combos,
               "success_if_random": round(1.0 / combos, 4) if combos else None}
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
            },
            # [FIX-B4] my past actions; [FIX-B36] past targets say whether they are in my room NOW
            "recent_actions": [dict(a, target_here_now=a.get("target") in here) if a.get("target") is not None else a
                               for a in self.action_log[-RECENT_ACTIONS:]],
            "room_notes": {str(k): v for k, v in sorted(self.room_notes.items())},
            "experience": self.experience,                          # [FIX-B1]
            "positions": [{"id": x.get("id"), "room": x.get("room"), "mode": x.get("mode")}
                          for x in observation.get("positions") or []],
            "relationships": self.relationships,
            "sub_missions": self._missions(),                       # [FIX-B6]
            "my_information": self._my_info(),                       # [FIX-B30] with provenance
            "heard_statements": (observation.get("statements") or [])[-8:],
            "heard_claims": self.heard_claims[-8:],                  # [FIX-B31] unverified talk
            "already_disclosed": self.disclosed,
            "announcements": observation.get("announcements") or [],
            "question_pairs": (observation.get("question_pairs") or [])[-10:],
        }
        state["trades"] = self.trades_view  # [FIX-B17]
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
        else:
            state["candidates"] = {k: [c.get("name") for c in (self.candidates.get(k) or [])]
                                   for k in CATEGORIES}
        if self._last_roll:
            state["last_roll"] = self._last_roll
        return json.dumps(state, ensure_ascii=False)

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
            if not ok:
                self._log({"event": "invalid_output", "cycle": self.cycle, "phase": phase,
                           "action": action, "reason": reason, "attempt": attempt})
                feedback = {"rejected_action": {"type": action, "payload": payload}, "reason": reason,  # [FIX-B36]
                            "valid_targets_now": [x.get("id") for x in observation.get("occupants") or []
                                                  if x.get("mode") != "corpse"],
                            "allowed_actions": allowed}
                continue
            payload = self._finalize(action, payload)
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
            self.combinations_at_accuse = math_.get("combinations_left")
            self.accuse_scores = {k: math_.get(k) for k in ("score_if_wrong", "score_if_unsolved_and_alive",
                                                             "my_mission_points")}  # [FIX-B21]
        return payload

    def _record(self, parsed, action, payload, observation, attempt, started, usage):
        held = {self._item_id(i) for i in self.known_info}
        used = [x for x in (parsed.get("evidence_used") or []) if isinstance(x, str)]
        self._log({"event": "decision", "cycle": self.cycle, "phase": observation.get("phase"),
                   "action": action, "payload": payload,
                   "evidence_used": used,
                   "ungrounded_evidence": [x for x in used if x not in held],
                   "hypothesis": parsed.get("current_hypothesis"),
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
            "heard_claims": self.heard_claims[-6:],
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
                "combinations_left": math_.get("combinations_left"),
                "clues_held": len(self.known_info),
                "explores": len(explores),
                "wasted_explores": sum(1 for a in explores if (a["successes"] or 0) > 0 and a["new_clues"] == 0),
                "questions": len(questions),
                "question_clues": sum(a["new_clues"] for a in questions),
                "stalled_cycles": best}

    def reflect(self, entry):
        """One LLM call at the start of the next game (the engine refuses calls after a
        game has ended). Returns (lessons, tactics, risk) or None to keep rule lessons."""
        if self.client is None or not entry:
            return None
        system = ("너는 방금 끝난 살인 미스터리 게임을 돌아보는 플레이어다. JSON 하나만 출력한다.\n"
                  "너의 성격(me.mbti, me.tendencies)은 모든 게임에서 같다. 교훈은 누구에게나 맞는 일반론이 아니라, "
                  "이번 판에서 네 성격의 장점이 살았던 장면과 약점이 드러난 장면을 바탕으로 다음 게임에서 네 성격대로 더 잘 할 방법을 쓴다. "
                  "성격을 버리거나 반대 성격처럼 행동하라는 교훈(예: I가 '더 적극적으로 나서라')은 쓰지 않는다.\n"
                  '{"lessons": [다음 게임에 쓸 교훈 1~3개, 각 한 문장], '
                  '"tactics": [아래 목록에서 0~3개], "risk": -1|0|1}\n'
                  f"tactics 목록: {', '.join(TACTIC_NAMES)}\n"
                  "risk: 다음 게임에서 고발을 더 과감히(+1), 그대로(0), 더 신중히(-1).\n"
                  "사건 정답·인물 이름·방 번호처럼 이번 사건에만 해당하는 사실은 쓰지 마라. "
                  "다음 게임은 다른 사건이고 다른 캐릭터를 맡을 수 있다.")
        user = json.dumps({"me": self._me_type(), "this_game": entry.get("stats"), "official_feedback": entry.get("feedback"),
                           "game_facts": entry.get("lessons")}, ensure_ascii=False)
        try:
            text, usage = self._request_complete(system, user, budget=REFLECT_BUDGET)
            self._count(usage)
        except Exception as exc:
            self._log({"event": "reflection_failed", "reason": str(exc)[:200]})
            return None
        parsed = self._parse(text) or {}
        lessons = [str(x)[:300] for x in parsed.get("lessons") or [] if str(x).strip()][:3]
        tactics = [t for t in parsed.get("tactics") or [] if t in TACTIC_NAMES][:3]
        risk = parsed.get("risk") if parsed.get("risk") in (-1, 0, 1) else 0
        self._log({"event": "reflection", "game_id": entry.get("game_id"), "lessons": lessons,
                   "tactics": tactics, "risk": risk})
        return (lessons, tactics, risk) if lessons else None

    # ---------- stage 6: negotiation (not an action; no phase cost) ----------

    def _occupant(self, x):
        """[FIX-B20] Who is in my room, and for players: which pool agent (short tag), its public
        trade record over all games, and how it treated me in our own past trades."""
        out = {"id": x.get("id"), "name": x.get("name"), "mode": x.get("mode"), "job": x.get("job")}
        aid = x.get("agent_id")
        if aid:   # [FIX-B30] the engine shows no record: only what I remember of that pool agent
            out.update(agent_tag=aid[-6:],
                       # [FIX-B33] the engine's carried-over agent relationship (A28) was dropped here,
                       # so no prompt ever showed it (runs/v6-check-20c game 2: 44 non-50 values, 0 shown)
                       my_agent_relationship=x.get("my_agent_relationship", 50.0),
                       my_history_with_them=self.peer_reputation.get(aid, {"fulfilled": 0, "broken": 0}),
                       they_got_from_me=self.gave_to.get(aid, []), i_got_from_them=self.got_from.get(aid, []))
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
        if claim not in self.heard_claims:
            self.heard_claims.append(claim)
            self.heard_claims = self.heard_claims[-30:]
            self._log({"event": "claim_heard", **claim})

    @staticmethod
    def _talk_ok(message):
        return message is None or (isinstance(message, str) and len(message) <= TALK_LIMIT)

    def record_trade_result(self, view, me):
        """A trade of mine closed this cycle. I judge the partner myself: did what I received fit
        what they declared (they proposed) or what I requested (I proposed)?"""
        partner = view["recipient"] if view.get("proposer") == me else view.get("proposer")
        partner_character = view.get("recipient_character") if view.get("proposer") == me else view.get("proposer_character")
        entry = {"cycle": view.get("cycle"), "partner_character": partner_character, "status": view.get("status"),
                 "offer_types": view.get("offer_types"), "requested": view.get("requested"),
                 "i_was": "proposer" if view.get("proposer") == me else "recipient"}
        if view.get("status") == "settled":
            spec = view.get("requested") if view.get("proposer") == me else view.get("offer_types")
            held = {self._item_id(i): i for i in self.known_info}
            got = [held[c] for c in view.get("received_ids") or [] if c in held]
            honest = bool(spec) and len(got) == len(spec) and self.assign(got, spec) is not None
            rec = self.peer_reputation.setdefault(partner, {"fulfilled": 0, "broken": 0})
            rec["fulfilled" if honest else "broken"] += 1
            self.gave_to.setdefault(partner, []).extend(view.get("gave_ids") or [])
            self.got_from.setdefault(partner, []).extend(view.get("received_ids") or [])
            for cid in view.get("received_ids") or []:
                self.label(cid, "trade", partner_character)
            entry.update(received=view.get("received_ids"), gave=view.get("gave_ids"), partner_honest=honest)
        self.trade_log.append(entry)
        self.trade_log = self.trade_log[-12:]
        self._log({"event": "trade_judged_by_me", **entry})

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

    def decide_trade(self, trade, observation, context=None):
        """[FIX-B29] Answer an incoming declared-type proposal: accept (seal my clues for their request,
        the exchange happens at once), counter (my declaration + request + sealed clues) or refuse."""
        mine = list(self._full_clues().values())
        items = self._items(trade.get("requested")) or []
        partner = trade.get("proposer")
        for m in trade.get("messages") or []:
            if m.get("from_character") == trade.get("proposer_character"):
                self.hear(m.get("from_character"), "trade", m.get("text"))
        if self.client is None:
            return self.respond_to_trade(trade)
        prompt = json.dumps({
            "incoming_trade": {"from_character": trade.get("proposer_character"), "agent_tag": (partner or "")[-6:],
                               "they_offer_types": trade.get("offer_types"), "they_request": items,
                               "messages": trade.get("messages") or []},
            "cycle": self.cycle, "remaining_cycles": observation.get("remaining_cycles"),
            "me": {"character_id": (observation.get("self") or {}).get("character_id"),
                   "mbti": (observation.get("self") or {}).get("mbti"),
                   "tendencies": self._tendencies((observation.get("self") or {}).get("mbti"))},

            "relationship_to_them": self.relationships.get(str(trade.get("proposer_character"))),
            "my_history_with_them": self.peer_reputation.get(partner, {"fulfilled": 0, "broken": 0}),
            "they_got_from_me": self.gave_to.get(partner, []), "i_got_from_them": self.got_from.get(partner, []),
            "my_information": self._my_info(),
            "clues_fitting_their_request": self.assign(mine, items) if items else None,
            "belief": self.belief.summary() if self.belief is not None else None,
            "heard_claims": self.heard_claims[-8:],
            "if_you_accept": context or {},
            "sub_missions": self._missions(), "my_trade_log": self.trade_log[-6:],
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
            offer = self._offer_ids(parsed.get("offer_ids"))
            message = parsed.get("message") if self._talk_ok(parsed.get("message")) else None
            out = None
            if decision == "refuse":
                out = {"decision": "refuse"}
            elif decision == "accept" and offer and len(offer) == len(items) and self.assign(mine, items) is not None:
                out = {"decision": "accept", "offer_ids": offer}
            elif decision == "counter" and offer:
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
            self._log({"event": "trade_response", "cycle": self.cycle, "trade_id": trade.get("trade_id"),
                       "decision": out["decision"], "sealed_honest": honest,
                       "consideration": str(parsed.get("consideration", ""))[:300]})
            self.last_utterance = parsed.get("utterance")
            return out
        counts = self.__dict__.setdefault("model_fallbacks", {})
        counts["trade_response"] = counts.get("trade_response", 0) + 1
        return self.respond_to_trade(trade)

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
