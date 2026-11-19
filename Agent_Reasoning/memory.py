"""[FIX-B1] Cross-game experience of one persistent pool agent.

Why this file exists: in experiment-30 every scenario started with a fresh LLMAgent
and nothing in the prompt said what this agent had done before, so 30 games were 30
first games. The engine already keeps the official record per persistent agent_id
(participation history, competencies, strategy_state); B never read it.

Two stores, both keyed by the persistent pool agent_id. That id is only stable when
all scenarios run against the same pool DB (run_batch.py shares it by default now).

  engine (official)  pool/learning/get|update: participation_history, competencies,
                     strategy_state. Only the generalized TACTICS enum and numeric
                     weights are accepted there, by design (no case secrets).
  local JSON (B)     per-game statistics and the agent's own lessons in natural
                     language, which the engine deliberately does not store.

Timing: the engine refuses LLM calls once a game has ended, so
  on_game_end  -> record(): statistics + official feedback + rule-based lessons (no LLM)
  next game    -> pending_reflection() / complete_reflection(): the agent, active again,
                  refines those lessons with one LLM call through the normal gateway.

[FIX-B37] Textbook II ch.7 "Nonparametric Learning", applied to that one reflection call:
  Reflexion  the reflection reads the game's trajectory (cycle-by-cycle position, action,
             outcome; stats["trajectory"]) and the last PREVIOUS_REFLECTIONS plans with their
             outcome metrics, says whether the last plan was followed and helped, and writes a
             concrete plan for the next game ("plan_for_this_game" in the prompt).
  ExpeL      the agent keeps its own insight list. The same call returns AGREE/EDIT/REMOVE/ADD
             operations, judged by contrasting this game's successful and failed sub-tasks
             (explore, question, trade). Scores as in ExpeL: ADD starts at 2, AGREE and EDIT +1,
             REMOVE -1, an insight at 0 is dropped; the prompt shows the top INSIGHTS_SHOWN.
  "lessons"  stay the rule-based facts of recent games (B32/B35), never overwritten.
Insights are per agent and never shared (user rule: no convergence of all agents to one style).
"""
import json
import os
from pathlib import Path

TACTICS = ("verify_before_accuse", "ask_before_duplicate_search", "share_when_stalled",
           "parallelize_independent_tasks", "prefer_unexplored_rooms", "request_before_escalation")
TACTIC_TEXT = {
    "verify_before_accuse": "고발 전에 근거를 다시 확인한다",   # [FIX-B52]
    # [FIX-B16] the old texts pushed toward questions; explore yields ~3x more definite clues
    "ask_before_duplicate_search": "room_remaining이 0인 칸을 다시 뒤지지 않고, 그 칸 단서를 가져간 사람에게 질문·교섭한다",
    "share_when_stalled": "단서가 몇 사이클째 늘지 않으면 교섭으로 단서를 맞바꾼다",
    "parallelize_independent_tasks": "서로 독립적인 조사를 한 사이클에 나눠 진행한다",
    "prefer_unexplored_rooms": "room_remaining이 남았거나 아직 탐색하지 않은 칸을 우선 탐색한다",
    "request_before_escalation": "심문·협박 전에 일반 질문을 먼저 시도한다",
}
MAX_LESSONS = 4              # [FIX-B37] facts of the most recent games (newest first)
RECENT_GAMES = 5
INSIGHT_CAP = 10             # [FIX-B37] ExpeL insight list
INSIGHTS_SHOWN = 6
INSIGHT_ADDS_PER_GAME = 2
PREVIOUS_REFLECTIONS = 3     # Reflexion reads the last 3 reflections (textbook update_memory)
TRAJECTORY_GAMES = 3         # keep full trajectories only for the newest games (file size)


def apply_operations(insights, operations, game_no):
    """[FIX-B37] ExpeL rule-list update. insights: [{"no", "text", "score", "born", "updated"}].
    Returns (new list, applied operations). Invalid operations are skipped, not fatal."""
    items = [dict(x) for x in insights or []]
    by_no = {x["no"]: x for x in items}
    next_no = max([x["no"] for x in items] + [0]) + 1
    applied, adds, touched = [], 0, set()
    for op in operations or []:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op", "")).upper()
        text = str(op.get("text") or "").strip()[:300]
        no = op.get("no")
        if kind == "ADD":
            if not text or adds >= INSIGHT_ADDS_PER_GAME or any(text == x["text"] for x in items):
                continue
            item = {"no": next_no, "text": text, "score": 2, "born": game_no, "updated": game_no}
            items.append(item)
            by_no[next_no] = item
            next_no, adds = next_no + 1, adds + 1
            applied.append({"op": "ADD", "no": item["no"], "text": text})
            continue
        if no not in by_no or no in touched:
            continue
        item = by_no[no]
        if kind == "AGREE":
            item["score"] += 1
        elif kind == "EDIT" and text:
            item.update(text=text, score=item["score"] + 1)
        elif kind == "REMOVE":
            item["score"] -= 1
        else:
            continue
        item["updated"] = game_no
        touched.add(no)
        applied.append({"op": kind, "no": no, **({"text": text} if kind == "EDIT" else {})})
    items = [x for x in items if x["score"] > 0]
    if len(items) > INSIGHT_CAP:   # drop the weakest, oldest first
        keep = sorted(items, key=lambda x: (-x["score"], -x["updated"]))[:INSIGHT_CAP]
        items = [x for x in items if x in keep]
    return items, applied


def top_insights(insights, n=INSIGHTS_SHOWN):
    ranked = sorted(insights or [], key=lambda x: (-x["score"], -x["updated"], x["no"]))[:n]
    return [{"no": x["no"], "text": x["text"], "score": x["score"]} for x in ranked]


def default_dir():
    return Path(os.environ.get("GAME_MEMORY_DIR") or Path.cwd() / "runs" / "agent-memory")


def _scores(block):
    block = block or {}
    wrong, unsolved = block.get("score_if_wrong"), block.get("score_if_unsolved_and_alive")
    return (wrong, unsolved if unsolved is not None else 0) if wrong is not None else (None, None)


OUTCOME_KO = {"correct_accusation": "정답", "wrong_accusation_dead": "오답 사망", "unsolved_survivor": "미해결 생존",
              "survivor_someone_else_won": "남이 맞혀 생존"}


def scoreboard_fact(board):
    """[FIX-B46] One sentence: my rank and score, and the score range of each outcome type."""
    groups = {}
    for r in board["rows"]:
        groups.setdefault(r.get("outcome"), []).append(r)
    parts = []
    for outcome, rows in groups.items():
        scores = sorted(r["score"] for r in rows)
        voided = sum(1 for r in rows if r.get("missions_voided"))
        span = f"{scores[0]}" if scores[0] == scores[-1] else f"{scores[0]}~{scores[-1]}"
        parts.append(f"{OUTCOME_KO.get(outcome, outcome)} {len(rows)}명 {span}점" + (f"(미션 무효 {voided}명)" if voided else ""))
    tied = board.get("my_tied") or 1
    place = f"공동 {board['my_rank']}위({tied}명)" if tied > 1 else f"{board['my_rank']}위"
    return (f"GM이 알려준 이번 판 결과: 나는 {board['players']}명 중 {place}, {board['my_score']}점. "
            + ", ".join(parts) + ".")


def rule_lessons(stats):
    """Facts from one game's statistics, used at game end and as the fallback when no LLM is
    available. [FIX-B32] Facts, not prescriptions: the old "do X next time" sentences were the
    same for every agent and every type, and all agents converged on them (runs/v4-30c)."""
    lessons, tactics, risk = [], [], 0
    if stats.get("wasted_explores", 0) >= 2:
        lessons.append(f"성공했는데 아무것도 못 얻은 탐색이 {stats['wasted_explores']}번 있었다(room_remaining 0인 칸).")
        tactics.append("prefer_unexplored_rooms")
    # [FIX-B21] the score table, stated as it was
    wrong_at, unsolved_at = _scores(stats.get("scores_at_accuse"))
    wrong_end, unsolved_end = _scores(stats.get("final_scores"))
    if stats.get("died"):
        lessons.append(f"추리 도전에서 틀려 사망했다. 그때 오답 점수 {wrong_at}, "   # [FIX-B52] no computed count
                       f"미해결 생존 점수 {unsolved_at}였다.")
        if wrong_at is None or wrong_at < unsolved_at:
            risk -= 1
    elif stats.get("resolution") in ("time_out", "deadline") and not stats.get("accused"):
        lessons.append(f"미해결로 끝났고 고발하지 않았다. 마지막에 오답 점수 {wrong_end}, 미해결 생존 점수 {unsolved_end}였다.")
        if wrong_end is not None and wrong_end >= unsolved_end:
            risk += 1
    # [FIX-B35] user decision 2026-11-08: what mutual failure cost me, and how far the closest rival got.
    # Facts only: the game stays competitive; what to make of them is the agent's (and its type's) call.
    if stats.get("resolution") in ("time_out", "deadline") and not stats.get("died"):
        actual, alt = stats.get("final_score"), stats.get("score_if_someone_had_won")
        if actual is not None and alt is not None:
            lost = stats.get("missions_voided_points") or 0
            lessons.append(f"이번 판은 아무도 맞히지 못해 너는 {actual}점이었고, 쌓은 미션 점수 {lost}점도 무효가 됐다. "
                           f"누군가 맞혔다면 너는 {alt}점이었다(차이 {alt - actual}점).")
    mine, rival = stats.get("final_combinations"), stats.get("closest_rival_combinations")
    if stats.get("resolution") in ("time_out", "deadline") and mine is not None and rival is not None:
        if rival < mine:
            lessons.append(f"가장 앞선 경쟁자는 남은 조합 {rival}개까지 좁혔다. 너는 {mine}개였다.")
        else:
            lessons.append(f"네가 가장 앞섰다(남은 조합 {mine}개). 다음 경쟁자는 {rival}개였다.")
    if stats.get("stalled_cycles", 0) >= 3:
        lessons.append(f"단서가 {stats['stalled_cycles']}사이클 연속 늘지 않았다.")
        tactics.append("share_when_stalled")
    if not lessons:
        lessons.append("특이 사항 없음.")
    return lessons, tactics, risk


class ExperienceStore:
    def __init__(self, pool_client, agent_id, catalog=None, directory=None):
        self.pool = pool_client
        self.agent_id = agent_id
        self.jobs = {c["id"]: c.get("job") or c.get("name")
                     for c in (catalog or {}).get("characters", []) if isinstance(c, dict)}
        self.path = Path(directory or default_dir()) / (agent_id.replace(":", "_") + ".json")

    # ---------- local file ----------
    def _local(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("games"), list):
                return data
        except (OSError, ValueError):
            pass
        return {"agent_id": self.agent_id, "games": []}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(self.path)

    # ---------- [FIX-B30] my own record of other pool agents (the engine shows none) ----------
    def load_peers(self):
        peers = self._local().get("peers") or {}
        out = {}
        for k, v in peers.items():
            if isinstance(v, dict):
                out[k] = {"fulfilled": int(v.get("fulfilled", 0)), "broken": int(v.get("broken", 0))}
                if "affinity" in v:   # [FIX-B43] my own feeling about that agent, carried across games
                    out[k].update(affinity=float(v["affinity"]), why=list(v.get("why") or [])[-3:])
                if "their_attitude" in v:   # [FIX-B47] how I think that agent sees me
                    out[k]["their_attitude"] = v["their_attitude"]
                for g in ("gifts_received", "gifts_given"):   # [FIX-B55] post-game shares between us (A40)
                    if v.get(g):
                        out[k][g] = int(v[g])
        return out

    def save_peers(self, peers):
        data = self._local()
        data["peers"] = {k: {"fulfilled": v.get("fulfilled", 0), "broken": v.get("broken", 0),
                             **({"affinity": v["affinity"], "why": list(v.get("why") or [])[-3:]} if "affinity" in v else {}),
                             **({"their_attitude": v["their_attitude"]} if "their_attitude" in v else {}),
                             **{g: int(v[g]) for g in ("gifts_received", "gifts_given") if v.get(g)}}   # [FIX-B55]
                         for k, v in (peers or {}).items()}
        self._save(data)

    # ---------- engine ----------
    def _official(self):
        if self.pool is None:
            return {}
        try:
            return self.pool.call("pool/learning/get")
        except Exception as exc:  # experience is an aid; never block a game on it
            return {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # ---------- read: what the prompt shows ----------
    def load(self):
        """Compact experience block for the prompt. Empty games -> first game."""
        official = self._official()
        local = self._local()
        by_game = {g["game_id"]: g for g in local["games"]}
        history = official.get("participation_history") or []
        recent = []
        for h in history[-RECENT_GAMES:]:
            mine = by_game.get(h.get("game_id"), {})
            s = mine.get("stats", {})
            recent.append({
                "game_no": h.get("participation_index"),
                "job": self.jobs.get(h.get("character_id"), h.get("character_id")),
                "resolution": s.get("resolution"),
                "score": h.get("final_score"),
                "won": bool(h.get("rank")),
                "survived": h.get("survived"),
                "cycles_played": s.get("cycles_played"),
                "accused": s.get("accused"),
                "wasted_explores": s.get("wasted_explores"),
                "missions_achieved": sum(1 for m in h.get("sub_missions") or [] if m.get("achieved")),
                # [FIX-B35] kept per game so the cost of mutual failure stays visible after reflection
                "score_if_someone_had_won": s.get("score_if_someone_had_won"),
                "my_combinations_at_end": s.get("final_combinations"),
                "closest_rival_combinations": s.get("closest_rival_combinations"),
                # in the engine's history but never closed by B (game aborted by the runner)
                "incomplete": h.get("game_id") not in by_game,
            })
        lessons = list(dict.fromkeys(x for g in reversed(local["games"])   # newest first, no repeats
                                     for x in g.get("lessons", [])))[:MAX_LESSONS]
        strategy = ((official.get("learning") or {}).get("strategy_state") or {})
        # [FIX-B37] Reflexion plan and ExpeL insights (my memory, not the engine's)
        last = next((g for g in reversed(local["games"]) if g.get("plan")), None)
        tactics = (last or {}).get("tactics")
        if tactics is None:
            tactics = strategy.get("tactics", [])
        scores = [h.get("final_score") or 0 for h in history]
        return {
            "games_played": len(history),
            "summary": (f"지난 {len(history)}게임: 승리 {sum(1 for h in history if h.get('rank'))}, "
                        f"오답 사망 {sum(1 for h in history if not h.get('survived'))}, "
                        f"평균 점수 {round(sum(scores) / len(scores), 1)}") if history else "첫 게임",
            "recent_games": recent,
            "lessons": lessons,
            "plan_for_this_game": (last or {}).get("plan"),
            "insights": top_insights(local.get("insights")),
            "tactics": [f"{t}: {TACTIC_TEXT.get(t, t)}" for t in tactics],
            "risk_weight": (strategy.get("weights") or {}).get("risk", 0),
            "source_error": official.get("error"),
        }

    # ---------- write: game end (no LLM) ----------
    def record(self, game_id, stats, feedback):
        lessons, tactics, risk = rule_lessons(stats)
        board = (feedback or {}).get("scoreboard") if isinstance(feedback, dict) else None
        if isinstance(board, dict) and board.get("rows"):   # [FIX-B46] a fact, not a prescription
            lessons.append(scoreboard_fact(board))
        data = self._local()
        data["games"] = [g for g in data["games"] if g.get("game_id") != game_id]
        data["games"].append({"game_id": game_id, "stats": stats, "feedback": feedback,
                              "lessons": lessons, "lesson_source": "rule", "reflected": False})
        for g in data["games"][:-TRAJECTORY_GAMES]:   # [FIX-B37] bound the file
            (g.get("stats") or {}).pop("trajectory", None)
            (g.get("stats") or {}).pop("final_notes", None)
        self._save(data)
        pushed = self.push_strategy(tactics, risk)
        return {"lessons": lessons, "tactics": tactics, "risk": risk, "engine": pushed}

    # ---------- write: next game, LLM refinement ----------
    def pending_reflection(self):
        """The unreflected game plus what Reflexion and ExpeL need: my last reflections with
        the metrics of the game each was written after, and my current insight list."""
        data = self._local()
        games = data["games"]
        entry = next((g for g in reversed(games) if not g.get("reflected")), None)
        if entry is None:
            return None
        entry = dict(entry)
        previous = []
        for g in games:
            if g.get("game_id") == entry["game_id"]:
                break
            if g.get("plan"):
                s = g.get("stats") or {}
                previous.append({"plan": g["plan"], "plan_check": g.get("plan_check"),
                                 "written_after_game_with": {
                                     "final_combinations": s.get("final_combinations"),
                                     "definite_clues": s.get("definite_clues"),
                                     "settled_trades": s.get("settled_trades"),
                                     "final_score": s.get("final_score")}})
        entry["previous_reflections"] = previous[-PREVIOUS_REFLECTIONS:]
        entry["insights"] = top_insights(data.get("insights"), INSIGHT_CAP)
        return entry

    def complete_reflection(self, game_id, result=None):
        """[FIX-B37] result = {"plan", "plan_check", "operations", "tactics", "risk"} or None
        (no LLM result: rule facts stay, insights unchanged)."""
        data = self._local()
        game_no = sum(1 for g in data["games"] if g.get("reflected")) + 1
        applied = []
        for g in data["games"]:
            if g.get("game_id") == game_id:
                if result:
                    data["insights"], applied = apply_operations(data.get("insights"), result.get("operations"), game_no)
                    g.update(plan=result.get("plan"), plan_check=result.get("plan_check"),
                             insight_operations=applied, lesson_source="llm",
                             tactics=[t for t in result.get("tactics") or [] if t in TACTICS])
                g["reflected"] = True
        self._save(data)
        if not result:
            return "kept rule lessons"
        return self.push_strategy([t for t in result.get("tactics") or [] if t in TACTICS], result.get("risk") or 0)

    def push_strategy(self, tactics, risk_delta):
        """Fold tactics/risk into the engine's official strategy_state."""
        if self.pool is None:
            return "no_pool_credentials"
        try:
            got = self.pool.call("pool/learning/get")
            learning = got["learning"]
            ids = [e["event_id"] for e in got.get("experience_events", []) if not e.get("strategy_consumed")]
            if not ids:
                return "no_unconsumed_feedback"
            state = learning.get("strategy_state") or {"tactics": [], "weights": {}}
            # [FIX-B37] replace, not union: the union only grew, and all 16 agents ended with the
            # same tactics (runs/v8-main-30c). An empty choice keeps the current list.
            merged = [t for t in TACTICS if t in set(tactics)] or list(state.get("tactics", []))
            weights = dict(state.get("weights") or {})
            weights["risk"] = max(-10, min(10, weights.get("risk", 0) + risk_delta))
            version = learning["version"]
            for start in range(0, len(ids), 100):  # the engine accepts at most 100 ids per update
                self.pool.call("pool/learning/update", expected_version=version,
                               experience_event_ids=ids[start:start + 100],
                               strategy_state={"tactics": merged, "weights": weights})
                version += 1
            return f"updated to v{version}"
        except Exception as exc:
            return f"failed: {type(exc).__name__}: {exc}"[:200]
