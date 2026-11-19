"""[FIX-I3] LLM backends that need no paid API key, for checking the pipeline.

Both implement the B client contract `request(system, user, budget) -> (text, usage)`
and receive the exact prompts B builds, so prompt construction, parsing, validation,
the game-end hook and cross-game experience are all exercised.

  RuleBasedLLM   deterministic, free, instant. Reads the prompt JSON and answers with a
                 legal decision. Tests plumbing, not reasoning quality.
  CodexCLIClient pipes the prompt to the local `codex exec` (ChatGPT subscription login).
                 A real model, but NOT the engine gateway: calls are not archived in the
                 engine DB (they are logged to codex-calls.jsonl instead) and the model is
                 whatever Codex runs. Never report these runs as the API model.
"""
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()


def _prompt_json(user):
    try:
        data = json.loads(user)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


class RuleBasedLLM:
    """A stand-in 'model' that follows the prompt's own numbers."""
    model = "rule-based-offline"

    def __init__(self, log_path=None):
        self.calls = 0
        self.log_path = Path(log_path) if log_path else None

    _process_calls = 0      # [FIX-I11] all agents in the process, for the simulated outage

    def request(self, system, user, budget=700, **_):
        self.calls += 1
        # [FIX-I11] FAKE_LLM_OUTAGE_AT=N: the N-th model call of the process meets a simulated
        # outage (FAKE_LLM_OUTAGE_PROBES failed health checks), to test PAUSE/RESUME end to end.
        with _LOCK:
            RuleBasedLLM._process_calls += 1
            n = RuleBasedLLM._process_calls
        if n == int(os.environ.get("FAKE_LLM_OUTAGE_AT") or 0):
            from model_outage import GATE
            left = [int(os.environ.get("FAKE_LLM_OUTAGE_PROBES") or 2)]

            def probe():
                left[0] -= 1
                return ("", {}, "simulated outage") if left[0] >= 0 else ('{"ok": true}', {}, None)
            return GATE.outage(probe, lambda: (*self._reply(system, user), None), "simulated outage", RuntimeError)
        return self._reply(system, user)

    def _reply(self, system, user):
        data = _prompt_json(user)
        if "response_options" in data:
            reply = self._answer(data)
        elif "incoming_trade" in data:  # [FIX-B17]
            reply = self._trade_reply(data)
        elif "this_game" in data:
            reply = self._reflect(data)
        elif "must_speak" in data:   # [FIX-B54] a talk room
            reply = {"consideration": "[offline] 담화", "say": f"[offline] {data.get('round')}라운드 한마디", "leave": False}
        elif "my_score_this_game" in data:   # [FIX-B55] the first-ranked player's post-game sharing
            people = data.get("players_of_this_game") or []
            reply = {"consideration": "[offline] 분배", "shares": [{"agent": people[0]["agent_tag"], "points": 10, "why": "[offline]"}] if people else []}
        else:
            reply = self._decide(data)
        text = json.dumps(reply, ensure_ascii=False)
        if self.log_path:  # keep the exact prompts so tests can inspect what the model saw
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with _LOCK, self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"system": system, "user": user, "response": text}, ensure_ascii=False) + "\n")
        return text, {"output": 0}

    @staticmethod
    def _answer(data):
        options, n = data.get("response_options") or {}, data.get("successes", 0)
        definite = [{"kind": "definite", "clue_id": c} for c in (options.get("definite") or [])[:1]]
        vague = [{"kind": "ambiguous", "pair": dict(x["pair"])} for x in options.get("ambiguous") or []]
        e = data.get("extra_vague_slots", 0)
        statements = ((vague[:1 + e] or definite) if n == 1 else ((definite + vague[:e]) if definite else vague[:2 + e])
                      if n == 2 else definite + vague[:2 + e])
        return {"statements": statements, "consideration": "규칙 기반 응답", "utterance": "알려드리죠."}

    @staticmethod
    def _reflect(data):
        stats = data.get("this_game") or {}
        # [FIX-B37] Reflexion plan + ExpeL operations: agree with the top insight, add one per game
        insights = data.get("insights") or []
        operations = [{"op": "AGREE", "no": insights[0]["no"]}] if insights else []
        if len(insights) < 3:
            operations.append({"op": "ADD", "text": f"[offline] 통찰 {len(insights) + 1}: 단서가 멈추면 거래한다."})
        if len(insights) >= 3:
            operations.append({"op": "REMOVE", "no": insights[-1]["no"]})
        return {"plan_check": "[offline] 지난 계획 확인" if data.get("previous_reflections") else "",
                "plan": f"[offline] 계획: 궤적 {len(data.get('trajectory') or [])}줄을 보고 탐색 후 거래한다.",
                "operations": operations,
                "tactics": ["prefer_unexplored_rooms"] if stats.get("wasted_explores") else [],
                "risk": 1 if not stats.get("accused") else 0}

    def _decide(self, data):
        allowed = data.get("allowed_actions") or []
        me = data.get("me") or {}
        belief = data.get("belief") or {}
        guess = {c: ((belief.get(c) or {}).get("remaining") or [{"id": 0}])[0]["id"]
                 for c in ("suspect", "weapon", "place")}
        leverage = belief.get("indirect_leverage") or []
        if leverage:  # use the compass the prompt now provides
            guess["place"] = leverage[0]["if_place_excluded"]
        notes = data.get("room_notes") or {}
        thresholds = me.get("thresholds") or {}
        action, payload = "pass", {}
        if "accuse" in allowed:
            math_ = data.get("accuse_math") or {}
            budget = data.get("time_budget") or {}
            scoring = data.get("scoring") or {}
            # [FIX-B52] no count is given any more: the stand-in multiplies the remaining candidates itself
            combos = 1
            for c in ("suspect", "weapon", "place"):
                combos *= max(1, (belief.get(c) or {}).get("remaining_count") or len((belief.get(c) or {}).get("remaining") or []) or 1)
            last = (budget.get("estimated_remaining_cycles") or 99) <= 1
            free_shot = scoring.get("unsolved_survivor", 0) <= scoring.get("wrong_accusation", -20)
            if combos <= 6 or (last and free_shot):
                action, payload = "accuse", guess
        elif "move" in allowed:
            room = me.get("room")
            if notes.get(str(room), {}).get("exhausted"):
                fresh = [r for r in range(8) if not notes.get(str(r), {}).get("exhausted") and r != room]
                action, payload = ("move", {"room": fresh[0]}) if fresh else ("stay", {})
            else:
                action = "stay"
        elif "explore" in allowed:
            here = notes.get(str(me.get("room")), {})
            targets = thresholds.get("by_target") or {}
            living = [o["id"] for o in data.get("occupants") or [] if o.get("mode") != "corpse"]
            best = min(((t[a], int(tid), a) for tid, t in targets.items() if int(tid) in living
                        for a in ("question", "persuade")), default=None)
            explore_th = thresholds.get("explore", 6)
            if best and (here.get("exhausted") or best[0] < explore_th):
                action, payload = "question", {"target_id": best[1], "approach": best[2], "hypothesis": guess}
            else:
                action = "explore"
        reason = f"규칙 기반 오프라인 결정: {action}"
        # [FIX-B29] propose every third cycle so the declared-type trade path is exercised offline
        mine = [i for i in data.get("my_information") or [] if i.get("kind") in ("definite", "indirect")]
        living = [o["id"] for o in data.get("occupants") or [] if o.get("mode") != "corpse"]
        if ("trade" in allowed and mine and living and (data.get("cycle") or 0) % 3 == 0):
            widest = max(("suspect", "weapon", "place"),
                         key=lambda c: len((belief.get(c) or {}).get("remaining") or []))
            first = mine[0]
            declared = first.get("category") if first.get("kind") == "definite" else first.get("type")
            known = [i.get("candidate") for i in mine if i.get("kind") == "definite" and i.get("category") == widest]
            action, payload = "trade", {"target_id": living[0], "offer_types": [declared], "offer_ids": [first["id"]],
                                        "requested": [{"type": widest, "exclude": known}],
                                        "message": "오프라인 제안"}
        out = {"consideration": reason, "evidence_used": [], "current_hypothesis": dict(guess, confidence=0.0)}
        if "accuse" in allowed:
            out["decision"] = action  # [FIX-B13]
            # [FIX-B44] the agent's own expected scores (uniform chance here; a model may judge otherwise)
            am = data.get("accuse_math") or {}
            p = 1.0 / max(1, combos)   # combos is set above whenever accuse is allowed
            win, wrong = am.get("score_if_correct_first", 100), am.get("score_if_wrong", -20)
            out["deduction_math"] = {"my_win_chance": round(p, 4), "expected_if_accuse": round(p * win + (1 - p) * wrong, 2),
                                     "expected_if_pass": am.get("score_if_unsolved_and_alive", -10),
                                     "pass_assumption": "[offline] 미해결 가정",
                                     "choice_reason": "[offline] 규칙대로"}
        # [FIX-B42] exercise the agent's own notes: add one every second cycle, make room when full
        notes = ((data.get("situation") or {}).get("my_notes") or {})
        cycle = data.get("cycle") or 0
        if data.get("phase") == "interaction" and cycle % 2 == 0:
            ops = [{"op": "add", "text": f"[offline] c{cycle} 메모"}]
            if notes.get("full"):
                ops = [{"op": "drop", "no": notes["notes"][-1]["no"]}] + ops
            out = dict(out, memory=ops)
        # [FIX-B47] react to every person in new_dealings (required)
        dealings = (data.get("situation") or {}).get("new_dealings") or []
        if dealings:
            out = dict(out, reaction=[{"who": d["who"], "their_attitude_to_me": 0, "delta": 1 if cycle % 2 else -1,
                                       "why": "[offline] 반응"} for d in dealings])
        # [FIX-B43] exercise the agent's own affinity: one impression of someone here every third cycle
        people = [p for p in ((data.get("now") or {}).get("people_here") or []) if p.get("agent_tag")]
        if data.get("phase") == "interaction" and cycle % 3 == 0 and people:
            out = dict(out, impression=[{"who": people[0]["id"], "delta": 5 if cycle % 2 else -3,
                                         "why": f"[offline] c{cycle} 인상"}])
        if data.get("phase") == "movement":   # [FIX-B54] want to talk every second cycle
            out = dict(out, talk=cycle % 2 == 0)
        return dict(out, action={"type": action, "payload": payload}, utterance=reason)

    @staticmethod
    def _trade_reply(data):
        offers = data.get("clues_fitting_their_request") or []
        if not offers:
            return {"consideration": "맞는 단서 없음", "decision": "refuse", "offer_ids": None, "message": "가진 게 없어요"}
        mine = [i["id"] for i in data.get("my_information") or [] if i.get("kind") in ("definite", "indirect")]
        others = [i for i in mine if i not in offers]
        # [FIX-B29] every fourth cycle hand over something else, so the deception path runs offline too
        if (data.get("cycle") or 0) % 4 == 0 and len(others) >= len(offers):
            return {"consideration": "기만", "decision": "accept", "offer_ids": others[:len(offers)]}
        return {"consideration": "맞는 단서로 수락", "decision": "accept", "offer_ids": offers}


class CodexUnavailable(BaseException):
    """Codex keeps failing (quota, login, network). Deliberately a BaseException: B catches
    Exception and would quietly replace every decision with its rule-based fallback, which
    would write a game the model never played into the agents' experience.
    [FIX-I11] Raised only when an outage outlasts CODEX_OUTAGE_MAX_SECONDS; shorter outages
    pause the game and resume it (model_outage.GATE). Then run_game cleans up and run_batch
    pauses the campaign for a later resume, as before."""


# [FIX-I11] the smallest call that proves Codex answers again (not an agent decision)
HEALTH_SYSTEM = "상태 확인용 호출이다. 다른 말 없이 JSON 객체 {\"ok\": true} 하나만 출력하라."
HEALTH_USER = "{}"


class CodexCLIClient:
    """Bridge to the local Codex CLI (`codex exec`), used when there is no API credit."""
    MAX_CONSECUTIVE_FAILURES = 3   # shared by every agent in the process
    _failures = 0

    # [FIX-I5] Error-level cap only: normal calls take 5-30 s; 300 s means the call is stuck.
    def __init__(self, executable, log_path, model="gpt-6.1-terra", reasoning="medium", timeout=300):
        self.executable, self.model, self.reasoning, self.timeout = executable, model, reasoning, timeout
        self.log_path = Path(log_path)
        self.workdir = Path(tempfile.mkdtemp(prefix="codex-empty-"))  # nothing for the agent to touch

    def request(self, system, user, budget=700, **_):
        """[FIX-I11] During a model outage every call waits at the gate; the call that sees the
        outage pauses the game, probes until Codex answers, then resumes it."""
        from model_outage import GATE, MODEL_POLICY
        GATE.wait_healthy(CodexUnavailable)
        text, usage, error = self._attempt(system, user)
        if not error:
            return text, usage
        if CodexCLIClient._failures >= self.MAX_CONSECUTIVE_FAILURES:
            return GATE.outage(lambda: self._attempt(HEALTH_SYSTEM, HEALTH_USER, kind="health_check"),
                               lambda: self._attempt(system, user), error, CodexUnavailable)
        if "TimeoutExpired" in error:   # [FIX-C20] a stuck call: the GM decides what happens next
            remedy = MODEL_POLICY.remedy()
            self._note(remedy)
            if remedy == "retry":
                started = time.time()
                text, usage, error = self._attempt(system, user, remedy="retry")
                MODEL_POLICY.learn("retry", not error, time.time() - started)
                if not error:
                    return text, usage
                if CodexCLIClient._failures >= self.MAX_CONSECUTIVE_FAILURES:
                    return GATE.outage(lambda: self._attempt(HEALTH_SYSTEM, HEALTH_USER, kind="health_check"),
                                       lambda: self._attempt(system, user), error, CodexUnavailable)
            else:
                MODEL_POLICY.learn("stand_in", False, 0)
        raise RuntimeError(error)  # a single failure: B falls back once and it is counted

    def _note(self, remedy):
        """[FIX-C20] the GM's choice after a stuck call, for the research log."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"ts": round(time.time(), 3), "kind": "gm_model_remedy", "remedy": remedy},
                                        ensure_ascii=False) + "\n")

    def _attempt(self, system, user, kind="agent", remedy=None):
        """One codex exec call, logged. Returns (text, usage, error)."""
        from model_outage import MODEL_POLICY
        deadline = MODEL_POLICY.deadline(self.timeout) if kind == "agent" else self.timeout   # [FIX-C20]
        prompt = (system + "\n\n[입력]\n" + user +
                  "\n\n중요: 파일·셸·도구를 쓰지 말고, 위 형식의 JSON 객체 하나만 최종 답으로 출력하라.")
        # --ignore-user-config/--ignore-rules: the operator's personal Codex memories, skills and
        # rules (~5.7k tokens measured) must not leak into agent prompts. --ephemeral: no Codex
        # session carries over between calls. --json: per-call token usage incl. reasoning.
        command = [self.executable, "exec", "--skip-git-repo-check", "--ephemeral", "--json",
                   "--ignore-user-config", "--ignore-rules",
                   "--sandbox", "read-only", "-C", str(self.workdir),
                   "-c", f'model_reasoning_effort="{self.reasoning}"']
        instructions = os.environ.get("CODEX_INSTRUCTIONS_FILE")   # [FIX-I15] lean mode: a game-player instruction
        if instructions:
            command += ["-c", 'model_instructions_file="' + str(Path(instructions).resolve()).replace(chr(92), "/") + '"']
        if self.model:
            command += ["-m", self.model]
        started, text, returncode, error, usage = time.time(), "", None, None, {"output": 0, "total": None}
        # [FIX-I10] Windows temp cleanup can delete the empty workdir mid-campaign; codex exec then
        # fails instantly with "file not found (os error 2)" (runs/v8-main-30c seed 5). Recreate it.
        self.workdir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "last.txt"
            try:
                proc = subprocess.run(command + ["-o", str(out), "-"], input=prompt, capture_output=True,
                                      text=True, encoding="utf-8", errors="replace", timeout=deadline)
                returncode = proc.returncode
                text = out.read_text(encoding="utf-8") if out.exists() else ""
                usage.update(_codex_usage(proc.stdout))
                if returncode != 0 or not text.strip():
                    error = f"codex exec failed ({returncode}): {(proc.stderr or proc.stdout)[-300:]}"
            except (OSError, subprocess.TimeoutExpired) as exc:
                error = f"codex exec could not run: {type(exc).__name__}: {exc}"[:300]
        record = {"ts": round(started, 3), "seconds": round(time.time() - started, 1),
                  "returncode": returncode, "model": self.model or "codex-default", "reasoning": self.reasoning,
                  "error": error, "system": system, "user": user, "response": text, "usage": usage,
                  "deadline": round(deadline, 1), **({"gm_remedy": remedy} if remedy else {}),   # [FIX-C20]
                  **({"kind": kind} if kind != "agent" else {})}   # [FIX-I11] health checks are marked
        if kind == "agent" and not error:
            MODEL_POLICY.observe(record["seconds"])   # [FIX-C20] what a normal call takes in this pool
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            CodexCLIClient._failures = CodexCLIClient._failures + 1 if error else 0
        return text, usage, error


def _codex_usage(stdout):
    """Token usage from `codex exec --json` (turn.completed event). 'output' feeds B's token
    count, which the engine uses to rank correct accusations (fewest first)."""
    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "turn.completed":
            u = event.get("usage") or {}
            return {"input": u.get("input_tokens"), "cached_input": u.get("cached_input_tokens"),
                    "output": (u.get("output_tokens") or 0) + (u.get("reasoning_output_tokens") or 0),
                    "reasoning": u.get("reasoning_output_tokens")}
    return {}


def find_codex():
    """Codex CLI on PATH, else the binary bundled with the VS Code extension."""
    from shutil import which
    found = os.environ.get("CODEX_EXE") or which("codex")
    if found:
        return found
    base = Path.home() / ".vscode" / "extensions"
    name = "codex.exe" if os.name == "nt" else "codex"  # the extension ships both; only one runs here
    candidates = sorted(base.glob(f"openai.chatgpt-*/bin/*/{name}"))
    return str(candidates[-1]) if candidates else None
