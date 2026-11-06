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
import uuid
try:
    from research_trace import begin, emit
except ImportError:
    def begin(*args, **kwargs): return {}
    def emit(*args, **kwargs): pass

from agent import LLMAgent

ALLOWED = {
    "movement": ("move", "stay"),
    "interaction": ("explore", "question", "prepare", "pass"),
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
        elif kind == "ambiguous" and any(x["category"] == s.get("category")
                                         and x["candidate"] == s.get("candidate")
                                         for x in options["ambiguous"]):
            result.append({"kind": "ambiguous", "category": s["category"],
                           "candidate": s["candidate"]})
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


def required_statements(question):
    """Smallest answer meeting the success-tier disclosure (spec A6)."""
    options, n = question["response_options"], question["successes"]
    definite = [{"kind": "definite", "clue_id": c} for c in options["definite"][:1]]
    vague = [{"kind": "ambiguous", "category": x["category"], "candidate": x["candidate"]}
             for x in options["ambiguous"]]
    vague += [{"kind": "indirect", "clue_id": x["clue_id"], "text": x["narrative_options"][0]}
              for x in options["indirect"]]
    if n <= 0:
        return []
    if n == 1:
        return vague[:1] or definite
    if n == 2:
        return definite or vague[:2]
    return definite + vague[:2]


class LLMPlayerRuntime:
    """One player slot: official observation in, one legal action out."""

    live_llm = True

    def __init__(self, role_client, session, catalog, agent=None, belief_mode=True):
        self.role = role_client
        self.catalog = catalog
        self.agent = agent or LLMAgent(session["agent_id"], belief_mode=belief_mode,
                                       catalog=catalog)
        self.fallbacks = 0
        self.idle = 0
        self._acted = set()  # (cycle_id, phase_epoch) already submitted

    # -- questions ---------------------------------------------------------
    def _answer(self, observation=None):
        for question in self.role.call("questions/inbox")["questions"]:
            begin(observation or {}, "answer", question_id=question["question_id"])
            emit("question_received", question=question)
            try:
                proposed = self.agent.answer_question(question)
            except Exception as exc:
                emit("fallback", reason=str(exc), error_type=type(exc).__name__)
                self.fallbacks += 1
                proposed = []
            statements = legal_statements(proposed, question)
            emit("answer_validation", proposed=proposed, accepted=statements)
            if len(statements) < len(required_statements(question)):
                self.fallbacks += 1
                statements = required_statements(question)
            try:
                self.role.call("questions/respond", question_id=question["question_id"],
                               response_id=uuid.uuid4().hex, statements=statements)
                emit("delivered_dialogue", speaker="player", visibility="private", text="응답: "+str(statements), question_id=question["question_id"], source="validated-statements")
            except Exception as exc:
                if _code(exc) == "expired":
                    # The engine owns timeout settlement; never retry a closed question.
                    continue
                if _code(exc) not in REJECTED:
                    raise
                self.fallbacks += 1
                try:
                    self.role.call("questions/respond", question_id=question["question_id"],
                                   response_id=uuid.uuid4().hex,
                                   statements=required_statements(question))
                    emit("delivered_dialogue", speaker="player / fallback", visibility="private", text="응답: "+str(required_statements(question)), question_id=question["question_id"], source="fallback-validated-statements")
                except Exception as retry_exc:
                    if _code(retry_exc) != "expired":
                        raise


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
                    ("question", "interrogate", "threaten", "negotiate")
                    and isinstance(hypothesis, dict)
                    and all(k in hypothesis for k in ("suspect", "weapon", "place")))
        if action == "accuse":
            return all(k in payload for k in ("suspect", "weapon", "place"))
        return True  # stay / pass / prepare / explore (optional target_id)

    # -- loop --------------------------------------------------------------
    def step(self):
        obs = self.role.call("agent/observe")
        if obs.get("ended") or obs["self"].get("mode") != "active":
            return "inactive"
        self.agent.sync(obs)
        self._answer(obs)
        slot = (obs["cycle_id"], obs["phase"], obs["phase_epoch"])
        if not obs.get("remaining_actions") or slot in self._acted:
            self.idle += 1
            if self.idle % HEARTBEAT_EVERY_IDLE_STEPS == 0:
                self.role.call("agent/heartbeat")
            return "waiting"
        self.idle = 0
        begin(obs, "action")
        before_fallbacks = self.fallbacks
        action, payload = self._choose(obs)
        request = {k: obs[k] for k in ("game_id", "cycle_id", "phase", "phase_epoch")}
        request.update(agent_id=obs["self"]["id"], action_id=uuid.uuid4().hex,
                       action=action, payload=payload)
        emit("selected_action", request=request, fallbacks=self.fallbacks-before_fallbacks)
        declaration = getattr(self.agent, "last_utterance", None)
        if declaration and self.fallbacks == before_fallbacks:
            emit("delivered_dialogue", speaker="player", visibility="private", text=declaration, source="model-action-declaration", action_id=request["action_id"])
        result = self.role.call("agent/action", request=request)
        emit("delivered_dialogue", speaker="GM / 규칙 엔진", visibility="private", text="행동: "+action+" · 결과: "+str(result.get("outcome"))+"\n"+"\n".join(x.get("text",str(x)) for x in result.get("acquired_information",[])), source="engine-result", action_id=request["action_id"])
        emit("execution", request=request, result=result)
        self._acted.add(slot)
        if result.get("execution_status") == "completed":
            self.agent.ingest_result(result)  # roll + acquired_information
        return action


def factory(role_client, session, catalog):
    """`--player adapter:factory`"""
    return LLMPlayerRuntime(role_client, session, catalog)
