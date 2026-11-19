"""B player runtime: engine I/O around the LLM reasoning agent.

Slot contract (C, V13_ALIGNMENT_20261026.md section 4):

    factory(role_client, session, catalog) -> object with step() -> str

step() is non-blocking: observe once, answer pending questions, submit at most
one action for the current phase, heartbeat while idle, return. The engine rolls
the dice; the result carries roll + acquired_information back for belief update.

Requires from agent.py:
    sync(observation)                 -> None
    decide(observation)               -> (action, payload)
    answer_question(question)         -> list[statement]
    ingest_result(result)             -> None
    output_tokens                     -> int (cumulative gateway output tokens)
"""
import importlib.util
import json
import inspect
import uuid
from collections import Counter
from pathlib import Path
try:
    from research_trace import begin, emit
except ImportError:
    def begin(*args, **kwargs): return {}
    def emit(*args, **kwargs): pass

from agent import LLMAgent


def _load_memory():
    """Load memory.py by path: run_game.py loads B modules by path, so 'memory' is not
    importable by name, and a bare name could pick up an unrelated module."""
    spec = importlib.util.spec_from_file_location("_team_b_memory", Path(__file__).with_name("memory.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


memory = _load_memory()

ALLOWED = {
    "movement": ("move", "stay"),
    "interaction": ("explore", "question", "prepare", "pass", "trade"),  # [FIX-B22] trade = proposal
    "deduction": ("accuse", "pass"),
}
FALLBACK = {"movement": "stay", "interaction": "pass", "deduction": "pass"}
REJECTED = ("disclosure_required", "untruthful", "invalid_input")
HEARTBEAT_EVERY_IDLE_STEPS = 5


def _code(exc):
    return getattr(exc, "code", None)


def legal_statements(statements, question):
    """Keep only statements inside the engine's current response domain."""
    options, result = question["response_options"], []
    for s in statements or []:
        kind = s.get("kind")
        if kind == "definite" and s.get("clue_id") in options["definite"]:
            result.append({"kind": "definite", "clue_id": s["clue_id"]})
        elif kind == "ambiguous" and isinstance(s.get("pair"), dict) and any(
                x["pair"] == {k: s["pair"][k] for k in sorted(s["pair"])} for x in options["ambiguous"]):
            result.append({"kind": "ambiguous", "pair": {k: s["pair"][k] for k in sorted(s["pair"])}})  # [FIX-B23]
        elif kind == "indirect":
            option = next((x for x in options["indirect"]
                           if x["clue_id"] == s.get("clue_id")), None)
            if option:
                # Closed grammar: free narration is rejected, so snap to an option.
                text = s.get("text", s.get("narration"))
                result.append({"kind": "indirect", "clue_id": s["clue_id"],
                               "text": text if text in option["narrative_options"]
                               else option["narrative_options"][0]})
    return result


def _key(s):
    """Identity of a statement, as the engine dedupes them (FIX-B23: pairs)."""
    return (s.get("kind"), s.get("clue_id"), json.dumps(s.get("pair"), sort_keys=True))


def disclosure_ok(statements, question):
    """Mirror of the engine's validate_response tier rule (competitive.py), so a response
    that would be refused is repaired here instead of costing a round trip."""
    options, n = question["response_options"], question["successes"]
    keys = [_key(s) for s in statements]
    if len(set(keys)) != len(keys) or len(statements) > 3 + question.get("extra_vague", 0):
        return False
    full = sum(s.get("kind") == "definite" for s in statements)
    vague = len(statements) - full
    extra = question.get("extra_vague", 0)  # [FIX-B24] engine A19
    avail = len(options["ambiguous"]) + len(options["indirect"])
    max_vague = min(2 + extra, avail)
    if n <= 0:
        return full == vague == 0
    if n == 1:
        return (full == 0 and vague == min(1 + extra, avail)) or (avail == 0 and full == 1 and vague == 0)
    if n == 2:
        return ((full == 1 and vague == min(extra, avail) and bool(options["definite"]))
                or (full == 0 and vague == max_vague and (max_vague > 0 or not options["definite"])))
    return full == int(bool(options["definite"])) and vague == max_vague


def conform_statements(statements, question):
    """[FIX-B14] Make the model's (already legal) statements meet the tier exactly, keeping
    its choices: dedupe, and at 2 successes a model that offered a definite clue gives that
    clue alone (it mixed forms in 53 of 61 refusals in arm-20c). Falls back to the minimum."""
    seen, unique = set(), []
    for s in statements:
        key = _key(s)
        if key not in seen:
            seen.add(key)
            unique.append(s)
    if disclosure_ok(unique, question):
        return unique
    n, base = question["successes"], required_statements(question)
    definite = [s for s in unique if s["kind"] == "definite"][:1]
    vague = [s for s in unique if s["kind"] != "definite"]
    filler = [s for s in base if s["kind"] != "definite" and s not in vague]
    extra = question.get("extra_vague", 0)  # [FIX-B24]
    if n == 2 and definite:
        candidate = definite + (vague + filler)[:extra]
    elif n == 2:
        candidate = (vague + filler)[:2 + extra]
    elif n >= 3:
        candidate = (definite or [s for s in base if s["kind"] == "definite"]) + (vague + filler)[:2 + extra]
    else:
        candidate = (vague + filler)[:1 + extra]
    return candidate if disclosure_ok(candidate, question) else base


def required_statements(question):
    """Smallest answer meeting the success-tier disclosure (spec A6)."""
    options, n = question["response_options"], question["successes"]
    definite = [{"kind": "definite", "clue_id": c} for c in options["definite"][:1]]
    vague = [{"kind": "ambiguous", "pair": dict(x["pair"])} for x in options["ambiguous"]]  # [FIX-B23]
    vague += [{"kind": "indirect", "clue_id": x["clue_id"], "text": x["narrative_options"][0]}
              for x in options["indirect"]]
    extra = question.get("extra_vague", 0)  # [FIX-B24]
    if n <= 0:
        return []
    if n == 1:
        return vague[:1 + extra] or definite
    if n == 2:
        return (definite + vague[:extra]) if definite else vague[:2 + extra]
    return definite + vague[:2 + extra]


class LLMPlayerRuntime:
    """One player slot: official observation in, one legal action out."""

    live_llm = True

    def __init__(self, role_client, session, catalog, agent=None, belief_mode=True, memory_dir=None):
        self.role = role_client
        self.catalog = catalog
        self.session = session
        self.agent = agent or LLMAgent(session["agent_id"], belief_mode=belief_mode,
                                       catalog=catalog)
        self.fallbacks = 0
        self.idle = 0
        self._acted = set()  # (cycle_id, phase_epoch) already submitted
        # [FIX-B1] The same persistent pool agent plays every scenario; this is where it
        # remembers. pool_credentials come from C's AgentWorker (FIX-C1).
        credentials = session.get("pool_credentials")
        self.pool = role_client.bind(**credentials) if credentials and hasattr(role_client, "bind") else None
        self.memory = memory.ExperienceStore(self.pool, session["agent_id"], catalog, memory_dir)
        self._experience_ready = False
        # [FIX-B17] trades: the engine had the whole protocol, nothing in B ever called it
        self._trade_done = set()    # (trade_id, step) already handled
        self._heard = set()         # [FIX-B31] (trade_id, message index) already passed to the agent
        self._closed = set()        # [FIX-B30] trades already recorded in the agent's memory
        self._reported_relations = None   # [FIX-B43] last affinity report sent to the GM
        self._my_decision = {}      # [FIX-B22] trade_id -> my sealed decision (the engine hides both)
        self._paid_epoch = None     # [FIX-B22] phase epoch whose action I spent on a trade
        self._submitted = {}        # [FIX-B22] phase epoch -> (action, payload) I submitted
        self.trade_activity = Counter()
        self._pending = {}          # [FIX-B19] action_id -> payload of actions settled later
        self._abandoned = set()     # [FIX-B25] question ids the engine refused twice
        self._ingest_takes_payload = len(inspect.signature(self.agent.ingest_result).parameters) >= 2

    @property
    def model_fallbacks(self):
        """[FIX-B9] Decisions/answers the model never made (gateway error, unparseable output)."""
        return sum(getattr(self.agent, "model_fallbacks", {}).values())

    @property
    def model_calls(self):
        return getattr(self.agent, "model_calls", 0)

    @property
    def say_do_mismatches(self):
        """[FIX-B13] deduction outputs rejected because decision/action/hypothesis disagreed."""
        return dict(getattr(self.agent, "say_do_mismatches", {}))

    # -- cross-game experience ---------------------------------------------
    def _prepare_experience(self):
        """Once per game, before the first decision: finish the reflection on the last
        game I played (needs an LLM call, so it must happen while I am an active player),
        then load my whole experience into the agent's prompt."""
        if self._experience_ready:
            return
        self._experience_ready = True
        try:
            pending = self.memory.pending_reflection()
            if pending:   # [FIX-B37] Reflexion + ExpeL result (dict) or None
                self.memory.complete_reflection(pending["game_id"], self.agent.reflect(pending))
            self.agent.experience = self.memory.load()
            # [FIX-B30] how each pool agent treated me in past trades: MY memory, not the engine's
            self.agent.peer_reputation = self.memory.load_peers()
            self._load_share_notes()   # [FIX-B55]
            emit("experience_loaded", agent_id=self.session["agent_id"],
                 games_played=self.agent.experience.get("games_played"),
                 lessons=len(self.agent.experience.get("lessons") or []),
                 insights=len(self.agent.experience.get("insights") or []),
                 has_plan=bool(self.agent.experience.get("plan_for_this_game")))
        except Exception as exc:  # experience is an aid; the game must go on
            self.agent.experience = {"games_played": None, "source_error": f"{type(exc).__name__}: {exc}"[:200]}
            emit("experience_failed", reason=str(exc), error_type=type(exc).__name__)

    def _load_share_notes(self):
        """[FIX-B55/A40] Points first-ranked players gave me and I gave others (told only to the two of us),
        as totals per pool agent. Recomputed from the engine each game, so a rollback cannot double them."""
        if self.pool is None:
            return
        try:
            notes = self.pool.call("pool/standings")
        except Exception as exc:
            emit("standings_failed", reason=str(exc)[:200])
            return
        for field, key, who in (("gifts_received", "shares_received", "from"), ("gifts_given", "shares_given", "to")):
            totals = {}
            for x in notes.get(key) or []:
                totals[x[who]] = totals.get(x[who], 0) + x["points"]
            for aid, rec in self.agent.peer_reputation.items():
                rec.pop(field, None)
            for aid, pts in totals.items():
                self.agent.peer_reputation.setdefault(aid, {"fulfilled": 0, "broken": 0})[field] = pts
        self.agent.share_notes = {k: notes.get(k) for k in ("shares_received", "shares_given")}

    def share_after_game(self, state):
        """[FIX-B55/A40] Called by the runner after the GM report: if I ranked first, I decide whether to give
        part of my score of this game to other pool agents (my call; giving nothing is fine)."""
        if self.pool is None:
            return None
        game_id = self.session.get("game_id")
        try:
            feedback = self.pool.call("pool/feedback", game_id=game_id)["feedback"].get(self.session["agent_id"])
        except Exception as exc:
            emit("share_failed", reason=str(exc)[:200])
            return None
        if not feedback or feedback.get("rank") != 1 or (feedback.get("final_score") or 0) <= 0:
            return None
        try:
            standings = self.pool.call("pool/standings").get("standings") or []
            decision = self.agent.decide_share(feedback, standings)
            result = self.pool.call("pool/share", game_id=game_id, shares=decision["shares"])
        except Exception as exc:   # sharing is optional; the runner closes it afterwards
            emit("share_failed", reason=str(exc)[:200])
            return None
        for x in result.get("shares") or []:
            rec = self.agent.peer_reputation.setdefault(x["agent_id"], {"fulfilled": 0, "broken": 0})
            rec["gifts_given"] = int(rec.get("gifts_given", 0)) + x["points"]
        self.memory.save_peers(self.agent.peer_reputation)
        emit("score_shared", agent_id=self.session["agent_id"], game_id=game_id, shares=result.get("shares"),
             consideration=decision.get("consideration"))
        return result

    def on_game_end(self, state):
        """[FIX-B1] Called by the runner (FIX-C3) after the game is official. No LLM call
        here: the engine refuses new calls once a game has ended."""
        game_id = self.session.get("game_id")
        self._save_my_peers()   # [FIX-B51] the final cycle's reactions belong to my memory too
        feedback = None
        if self.pool is not None:
            try:
                feedback = self.pool.call("pool/feedback", game_id=game_id)["feedback"].get(self.session["agent_id"])
            except Exception as exc:
                emit("feedback_failed", reason=str(exc), error_type=type(exc).__name__)
        stats = self.agent.game_stats(state.get("resolution"))
        if feedback is not None:
            stats["died"] = not feedback.get("survived", True)
            stats["final_score"] = feedback.get("final_score")
            for key in ("final_combinations", "closest_rival_combinations",          # [FIX-B35]
                        "score_if_someone_had_won", "missions_voided_points"):
                stats[key] = feedback.get(key)
        saved = self.memory.record(game_id, stats, feedback)
        emit("experience_recorded", agent_id=self.session["agent_id"], game_id=game_id, saved=saved)
        return f"{self.session['agent_id'][-8:]}: {len(saved['lessons'])} lessons, engine {saved['engine']}"

    # -- questions ---------------------------------------------------------
    def _answer(self, observation=None):
        for question in self.role.call("questions/inbox")["questions"]:
            if question["question_id"] in self.__dict__.setdefault("_abandoned", set()):
                continue  # [FIX-B25] answered once and refused twice: never re-ask the model every step
            begin(observation or {}, "answer", question_id=question["question_id"])
            emit("question_received", question=question)
            try:
                proposed = self.agent.answer_question(question)
            except Exception as exc:
                emit("fallback", reason=str(exc), error_type=type(exc).__name__)
                self.fallbacks += 1
                proposed = []
            # [FIX-B12] the model took seconds; validate against the options as they are now
            current = next((q for q in self.role.call("questions/inbox")["questions"]
                            if q["question_id"] == question["question_id"]), None)
            if current is None:
                continue  # expired or settled while the model was answering
            legal = legal_statements(proposed, current)
            statements = conform_statements(legal, current)  # [FIX-B14]
            emit("answer_validation", proposed=proposed, accepted=statements, repaired=statements != legal)
            if statements != legal:
                self.answer_repairs = getattr(self, "answer_repairs", 0) + 1
            talk = getattr(self.agent, "last_answer_message", None)   # [FIX-B31]
            try:
                self.role.call("questions/respond", question_id=question["question_id"],
                               response_id=uuid.uuid4().hex, statements=statements,
                               **({"message": talk} if talk else {}))
                emit("delivered_dialogue", speaker="player", visibility="private", text="응답: "+str(statements), question_id=question["question_id"], source="validated-statements")
            except Exception as exc:
                if _code(exc) == "expired":
                    # The engine owns timeout settlement; never retry a closed question.
                    continue
                if _code(exc) not in REJECTED:
                    raise
                self.fallbacks += 1
                # [FIX-B12] Build the fallback from the question's CURRENT options. With parallel
                # stepping another player's answer can give me a definite clue while my model is
                # still thinking; that also creates ambiguous options, so the requirement grows
                # and a fallback built from the stale inbox is rejected too (crashed a game).
                fresh = next((q for q in self.role.call("questions/inbox")["questions"]
                              if q["question_id"] == question["question_id"]), None)
                if fresh is None:
                    continue  # already settled (expired or answered)
                try:
                    self.role.call("questions/respond", question_id=question["question_id"],
                                   response_id=uuid.uuid4().hex,
                                   statements=required_statements(fresh))
                    emit("delivered_dialogue", speaker="player / fallback", visibility="private", text="응답: "+str(required_statements(fresh)), question_id=question["question_id"], source="fallback-validated-statements")
                except Exception as retry_exc:
                    if _code(retry_exc) == "expired":
                        continue
                    if _code(retry_exc) not in REJECTED:
                        raise
                    # Still rejected: leave it to the engine's timeout settlement (its default
                    # response) rather than abort the whole game over one answer.
                    emit("answer_abandoned", question_id=question["question_id"], reason=str(retry_exc))
                    self.__dict__.setdefault("_abandoned", set()).add(question["question_id"])


    # -- trades (FIX-B17) ----------------------------------------------------
    def _trade_call(self, route, **body):
        try:
            return self.role.call(route, **body)
        except Exception as exc:  # expired, refused by the engine: never stop the game for a trade
            self.trade_activity[f"{route.split('/')[-1]}_error"] += 1
            emit("trade_error", route=route, reason=str(exc)[:200], error_type=type(exc).__name__)
            return None

    def _propose(self, obs, body):
        """[FIX-B22] Send the trade action. True when the engine took it (the action is spent)."""
        trade_id = "trade:" + uuid.uuid4().hex
        result = self._trade_call("trades/propose", trade_id=trade_id, message_id=trade_id + ":propose", **body)
        if result is None:
            return False
        self._my_decision[trade_id] = True  # [FIX-B29] marks "I paid for this trade"
        self._paid_epoch = obs["phase_epoch"]
        self.trade_activity["proposed"] += 1
        emit("trade_proposed", trade_id=trade_id, body=body)
        return True

    def _trades(self, obs):
        inbox = self._trade_call("trades/inbox")
        if inbox is None:
            return
        me = obs["self"]["id"]
        view = []
        for t in inbox.get("trades", []):
            tid, partner = t["trade_id"], t["recipient"] if t["proposer"] == me else t["proposer"]
            # someone else's proposal waiting for my answer (a counter makes me the recipient again)
            # [FIX-B27] each counter round resets the engine deadline, so it tells rounds apart;
            # keyed by proposer alone, the second counter back to the original proposer was skipped
            # and the trade sat until the 600 s error timeout (13 trades in runs/v4-20c)
            key = (tid, "respond", t["proposer"], t.get("deadline"))
            if t["recipient"] == me and t["status"] == "proposed" and key not in self._trade_done:
                self._trade_done.add(key)
                begin(obs, "trade", trade_id=tid)
                # [FIX-B22] accepting spends this phase's action: impossible once it went to a trade
                # (my own proposal, another acceptance) or while I hold a restricted replacement
                # a counter to my own proposal is answered with the action I already paid
                paid = (self._paid_epoch == obs["phase_epoch"] and tid not in self._my_decision) or obs.get("replacement")
                if paid:
                    choice = {"decision": "refuse"}
                    self.trade_activity["refuse_no_action_left"] += 1
                else:
                    submitted = self._submitted.get(obs["phase_epoch"])
                    context = {"costs": "이번 단계의 행동 1회",
                               "cancels_my_submitted_action": {"action": submitted[0], "payload": submitted[1]} if submitted else None}
                    choice = self.agent.decide_trade(t, obs, context)
                body = {k: v for k, v in choice.items() if k in ("decision", "offer_ids", "offer_types", "requested", "message")}
                if self._trade_call("trades/respond", trade_id=tid, message_id="respond:" + uuid.uuid4().hex,  # engine IDs are <= 100 chars
                                    **body) is not None:
                    # [FIX-B34] a P's free counter belonged to the A29 corrections; it applies only when the engine rule is on
                    free = choice["decision"] == "counter" and "P" in (obs["self"].get("mbti") or "") and bool(obs.get("mbti_rules"))
                    if choice["decision"] in ("accept", "counter") and not free:
                        self._paid_epoch = obs["phase_epoch"]
                    if choice["decision"] in ("accept", "counter"):
                        self._my_decision[tid] = True
                    self.trade_activity[choice["decision"]] += 1
                    emit("trade_responded", trade_id=tid, decision=choice["decision"])
                continue
            # [FIX-B31] talk from the partner reaches my claims; [FIX-B30] closed trades go to my memory
            partner_character = t["recipient_character"] if t["proposer"] == me else t["proposer_character"]
            for n, m in enumerate(t.get("messages") or []):
                if m.get("from_character") == partner_character and (tid, n) not in self._heard:
                    self._heard.add((tid, n))
                    self.agent.hear(partner_character, "trade", m.get("text"))
            if t["status"] in ("settled", "refused", "cancelled") and tid not in self._closed:
                self._closed.add(tid)
                self.agent.record_trade_result(t, me)
                if t["status"] == "settled":
                    self.memory.save_peers(self.agent.peer_reputation)
            view.append({"trade_id": tid[-8:], "role": "proposer" if t["proposer"] == me else "recipient",
                         "partner_character": partner_character, "status": t["status"],
                         "offer_types": t.get("offer_types"), "requested": t.get("requested"),
                         "received_ids": t.get("received_ids"), "gave_ids": t.get("gave_ids"),
                         "messages": t.get("messages")})
        self.agent.trades_view = view[-8:]

    # -- action ------------------------------------------------------------
    def _choose(self, obs):
        phase = obs["phase"]
        try:
            action, payload = self.agent.decide(obs)
        except Exception as exc:
            emit("fallback", reason=str(exc), selected=FALLBACK[phase])
            self.fallbacks += 1
            emit("fallback_action", selected=FALLBACK[phase])
            return FALLBACK[phase], {}
        payload = dict(payload or {})
        if action not in ALLOWED[phase] or not self._payload_ok(action, payload, obs):
            self.fallbacks += 1
            emit("fallback_action", selected=FALLBACK[phase])
            return FALLBACK[phase], {}
        if action == "accuse":
            payload["output_tokens"] = int(payload.get("output_tokens")
                                           or getattr(self.agent, "output_tokens", 0))
        return action, payload

    @staticmethod
    def _payload_ok(action, payload, obs):
        if action == "move":
            return isinstance(payload.get("room"), int)
        if action == "question":
            hypothesis = payload.get("hypothesis")
            return (payload.get("target_id") is not None
                    and payload.get("approach") in
                    ("question", "interrogate", "threaten", "persuade")
                    and isinstance(hypothesis, dict)
                    and all(k in hypothesis for k in ("suspect", "weapon", "place")))
        if action == "accuse":
            return all(k in payload for k in ("suspect", "weapon", "place"))
        return True  # stay / pass / prepare / explore (optional target_id)

    # -- loop --------------------------------------------------------------
    def _save_my_peers(self):
        """[FIX-B51] My own memory of other agents is saved whenever it changed. Until B51 it was saved only
        after a successful report to the GM, and a report is sent only when a score changed: reactions of the
        final cycle, a report refused after the end (409), and changes of reasons or of 'how they see me'
        without a score change never reached the next game."""
        if self.agent.affinity_dirty:
            self.memory.save_peers(self.agent.peer_reputation)
            self.agent.affinity_dirty = False

    def _report_relations(self, obs):
        """[FIX-B43/A35] Tell the GM how I feel about the players I met (my own scores) whenever that
        changed. The engine only relays it to the GM; the GM records the summary."""
        self._save_my_peers()   # [FIX-B51] first, whatever happens to the report
        scores = self.agent.affinity_report()
        if not scores or scores == self._reported_relations:
            return
        try:
            self.role.call("relations/report", scores=scores)
        except Exception as exc:   # a report must never stop the game
            emit("relations_report_error", reason=str(exc)[:200])
            return
        self._reported_relations = scores

    def step(self):
        obs = self.role.call("agent/observe")
        if obs.get("ended") or obs["self"].get("mode") != "active":
            return "inactive"
        self.agent.sync(obs)
        if not self._experience_ready:
            begin(obs, "experience")  # [FIX-B15] journal context, else these events were dropped
        self._prepare_experience()  # [FIX-B1]
        self._collect()    # [FIX-B19]
        self._answer(obs)
        self._trades(obs)  # [FIX-B17]
        self._report_relations(obs)   # [FIX-B43/A35]
        if obs.get("interaction_stage") == "talk":   # [FIX-B54/A42] the talk rooms come first this cycle
            return self._talk(obs)
        # [FIX-B22] a replacement action (after a mechanical failure) is a new slot in the same phase
        slot = (obs["cycle_id"], obs["phase"], obs["phase_epoch"], json.dumps(obs.get("replacement"), sort_keys=True))
        if obs["phase"] == "interaction" and self._paid_epoch == obs["phase_epoch"] and not obs.get("replacement"):
            self._acted.add(slot)  # my action went to a trade while I was answering it
        if not obs.get("remaining_actions") or slot in self._acted:
            self.idle += 1
            if self.idle % HEARTBEAT_EVERY_IDLE_STEPS == 0:
                self.role.call("agent/heartbeat")
            return "waiting"
        self.idle = 0
        begin(obs, "action")
        before_fallbacks = self.fallbacks
        action, payload = self._choose(obs)
        if action == "trade":  # [FIX-B22] the proposal is the action
            if self._propose(obs, payload):
                self._acted.add(slot)
                return action
            others = [a for a in obs.get("allowed_actions") or [] if a != "trade"]
            action, payload = self.agent._rule_action(obs, others)
            self.fallbacks += 1
        if obs["phase"] == "movement" and getattr(self.agent, "talk_intent", False):   # [FIX-B54] before my move lands
            try:
                self.role.call("talk/intent", join=True)
                emit("talk_intent", cycle=obs["cycle_id"])
            except Exception as exc:
                emit("talk_intent_refused", reason=str(exc)[:200])
        request = {k: obs[k] for k in ("game_id", "cycle_id", "phase", "phase_epoch")}
        request.update(agent_id=obs["self"]["id"], action_id=uuid.uuid4().hex,
                       action=action, payload=payload)
        emit("selected_action", request=request, fallbacks=self.fallbacks-before_fallbacks)
        declaration = getattr(self.agent, "last_utterance", None)
        if declaration and self.fallbacks == before_fallbacks:
            emit("delivered_dialogue", speaker="player", visibility="private", text=declaration, source="model-action-declaration", action_id=request["action_id"])
        try:
            result = self.role.call("agent/action", request=request)
        except Exception as exc:  # [FIX-B22] an accepted trade took this phase's action meanwhile
            if _code(exc) != "conflict":
                raise
            emit("action_superseded", reason=str(exc)[:200])
            self._acted.add(slot)
            return "superseded"
        self._submitted[obs["phase_epoch"]] = (action, payload)
        emit("delivered_dialogue", speaker="GM / 규칙 엔진", visibility="private", text="행동: "+action+" · 결과: "+str(result.get("outcome"))+"\n"+"\n".join(x.get("text",str(x)) for x in result.get("acquired_information",[])), source="engine-result", action_id=request["action_id"])
        emit("execution", request=request, result=result)
        self._acted.add(slot)
        if result.get("execution_status") == "completed":
            self._ingest(result, payload)
        elif result.get("execution_status") in ("pending", "held"):  # [FIX-B22] held until trades close
            self._pending[request["action_id"]] = payload
        return action

    def _talk(self, obs):
        """[FIX-B54/A42] My line in my room's talk, when it is my turn; otherwise wait."""
        view = (obs.get("self") or {}).get("talk")
        if not view or not view.get("my_turn"):
            self.idle += 1
            if self.idle % HEARTBEAT_EVERY_IDLE_STEPS == 0:
                self.role.call("agent/heartbeat")
            return "waiting"
        self.idle = 0
        begin(obs, "talk")
        line = self.agent.chat(obs, view)
        if line is None:   # the model failed: say nothing; the GM lets me go after the timeout
            self.fallbacks += 1
            emit("talk_failed", round=view.get("round"))
            return "waiting"
        try:
            self.role.call("talk/say", **({"text": line["say"]} if line.get("say") else {}), leave=line["leave"])
        except Exception as exc:
            if _code(exc) not in ("conflict", "invalid_input"):
                raise
            emit("talk_refused", reason=str(exc)[:200])
            return "waiting"
        emit("talk_said", round=view.get("round"), text=line.get("say"), leave=line["leave"])
        return "talk"

    def _ingest(self, result, payload):
        if self._ingest_takes_payload:  # [FIX-B4] payload gives question target/approach
            self.agent.ingest_result(result, payload)
        else:
            self.agent.ingest_result(result)  # roll + acquired_information

    def _collect(self):
        """[FIX-B19] Room searches settle at the end of the interaction phase (FIX-A14) and
        questions when the answer arrives; both used to be dropped from the agent's own record
        (room_remaining, recent_actions) because only immediately completed results were read."""
        for action_id, payload in list(self._pending.items()):
            try:
                result = self.role.call("agent/result", action_id=action_id)
            except Exception as exc:
                self._pending.pop(action_id, None)
                emit("result_lookup_failed", action_id=action_id, reason=str(exc)[:200])
                continue
            if result.get("execution_status") == "completed":
                self._pending.pop(action_id, None)
                self._ingest(result, payload)


def factory(role_client, session, catalog):
    """`--player adapter:factory`"""
    return LLMPlayerRuntime(role_client, session, catalog)
