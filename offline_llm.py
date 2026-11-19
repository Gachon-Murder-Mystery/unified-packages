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

    def request(self, system, user, budget=700, **_):
        self.calls += 1
        data = _prompt_json(user)
        if "response_options" in data:
            reply = self._answer(data)
        elif "incoming_trade" in data:  # [FIX-B17]
            reply = self._trade_reply(data)
        elif "this_game" in data:
            reply = self._reflect(data)
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
        lessons = list(data.get("rule_based_lessons") or [])[:2] or ["단서 수집 속도를 높인다."]
        return {"lessons": [f"[offline] {x}" for x in lessons],
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
            combos = math_.get("combinations_left") or 10**9
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
    would write a game the model never played into the agents' experience. This aborts the
    game instead; run_game cleans up and run_batch pauses the campaign for a later resume."""


class CodexCLIClient:
    """Bridge to the local Codex CLI (`codex exec`), used when there is no API credit."""
    MAX_CONSECUTIVE_FAILURES = 3   # shared by every agent in the process
    _failures = 0

    # [FIX-I5] Error-level cap only: normal calls take 5-30 s; 300 s means the call is stuck.
    def __init__(self, executable, log_path, model="gpt-6-luna", reasoning="medium", timeout=300):
        self.executable, self.model, self.reasoning, self.timeout = executable, model, reasoning, timeout
        self.log_path = Path(log_path)
        self.workdir = Path(tempfile.mkdtemp(prefix="codex-empty-"))  # nothing for the agent to touch

    def request(self, system, user, budget=700, **_):
        prompt = (system + "\n\n[입력]\n" + user +
                  "\n\n중요: 파일·셸·도구를 쓰지 말고, 위 형식의 JSON 객체 하나만 최종 답으로 출력하라.")
        # --ignore-user-config/--ignore-rules: the operator's personal Codex memories, skills and
        # rules (~5.7k tokens measured) must not leak into agent prompts. --ephemeral: no Codex
        # session carries over between calls. --json: per-call token usage incl. reasoning.
        command = [self.executable, "exec", "--skip-git-repo-check", "--ephemeral", "--json",
                   "--ignore-user-config", "--ignore-rules",
                   "--sandbox", "read-only", "-C", str(self.workdir),
                   "-c", f'model_reasoning_effort="{self.reasoning}"']
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
                                      text=True, encoding="utf-8", errors="replace", timeout=self.timeout)
                returncode = proc.returncode
                text = out.read_text(encoding="utf-8") if out.exists() else ""
                usage.update(_codex_usage(proc.stdout))
                if returncode != 0 or not text.strip():
                    error = f"codex exec failed ({returncode}): {(proc.stderr or proc.stdout)[-300:]}"
            except (OSError, subprocess.TimeoutExpired) as exc:
                error = f"codex exec could not run: {type(exc).__name__}: {exc}"[:300]
        record = {"ts": round(started, 3), "seconds": round(time.time() - started, 1),
                  "returncode": returncode, "model": self.model or "codex-default", "reasoning": self.reasoning,
                  "error": error, "system": system, "user": user, "response": text, "usage": usage}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            CodexCLIClient._failures = CodexCLIClient._failures + 1 if error else 0
            failures = CodexCLIClient._failures
        if error:
            if failures >= self.MAX_CONSECUTIVE_FAILURES:
                raise CodexUnavailable(f"{failures} consecutive Codex failures; last: {error}")
            raise RuntimeError(error)  # a single failure: B falls back once and it is counted
        return text, usage


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
