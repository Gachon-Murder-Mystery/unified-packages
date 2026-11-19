"""[FIX-I4] Check a batch for the three properties experiment-30 lacked.

  P1 same agents   every game draws its players from ONE persistent pool (same ids, MBTI fixed)
  P2 distinct      within a game each worker is a different agent: own id, character, MBTI
  P3 experience    an agent's first prompt in game k reports exactly the games it played
                   before k, with lessons; a newcomer's says it is its first game

    python verify_persistence.py runs/<batch-dir>

Needs prompts on disk: --fake-llm writes runs/<game>/fake-calls.jsonl, --codex writes
codex-calls.jsonl. For --llm runs P3 is read from the engine's llm_records instead.
Exit code 0 only if every check passes.
"""
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def prompts(game_id, db):
    """(user JSON dict) for every decision prompt of one game, in call order."""
    out = []
    for name in ("fake-calls.jsonl", "codex-calls.jsonl"):
        path = ROOT / "runs" / game_id / name
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(json.loads(line)["user"]))
                except (ValueError, KeyError, TypeError):
                    pass
    if not out:  # live API runs: the engine archived every request
        for (state,) in db.execute("SELECT state FROM llm_records WHERE game_id=? AND status='started' ORDER BY sequence", (game_id,)):
            try:
                out.append(json.loads(json.loads(state)["request"]["user"]))
            except (ValueError, KeyError, TypeError):
                pass
    return [p for p in out if isinstance(p, dict) and "me" in p]


def main(batch_dir):
    batch = json.loads((Path(batch_dir) / "batch.json").read_text(encoding="utf-8"))
    done = [a for a in batch["attempts"] if a["status"] == "completed"]
    if not batch.get("pool_db"):
        print("FAIL batch ran with --isolated: every seed had its own pool")
        return 1
    db = sqlite3.connect(batch["pool_db"])
    agents = {aid: json.loads(s) for aid, s in db.execute("SELECT agent_id, state FROM agents")}
    players = {a: v for a, v in agents.items() if v.get("pool_role") == "player"}
    failures = []

    def check(ok, text):
        print(("PASS " if ok else "FAIL ") + text)
        if not ok:
            failures.append(text)

    # P1
    mbtis = [v["fixed_mbti"] for v in players.values()]
    check(len(players) == 16 and len(set(mbtis)) == 16, f"P1 one pool of 16 player agents with 16 distinct MBTI ({len(players)} agents)")
    played_before, completed_by = Counter(), {}
    aborted = [a for a in batch["attempts"] if a["status"] != "completed"]
    if aborted:
        print(f"note: {len(aborted)} attempt(s) did not complete (paused/failed); their games stay in the agents' history")
    completed_ids = {a["game_id"] for a in done}
    for attempt in done:
        gid = attempt["game_id"]
        game = json.loads(db.execute("SELECT state FROM games WHERE id=?", (gid,)).fetchone()[0])
        roster = game["players"]  # agent_id -> player record
        check(set(roster) <= set(players), f"P1 seed {attempt['seed']}: all {len(roster)} players come from that pool")
        # P2
        chars = [p["character_id"] for p in roster.values()]
        char_mbti = [game["characters"][str(c)]["mbti"] for c in chars]
        check(len(set(roster)) == len(roster) and len(set(chars)) == len(chars) and len(set(char_mbti)) == len(char_mbti),
              f"P2 seed {attempt['seed']}: {len(roster)} distinct agents / characters / MBTI")
        for aid, p in roster.items():
            if game["characters"][str(p["character_id"])]["mbti"] != players[aid]["fixed_mbti"]:
                check(False, f"P2 seed {attempt['seed']}: {aid[-8:]} plays with a foreign MBTI")
        # P3: first prompt per character in this game
        first = {}
        for prompt in prompts(gid, db):
            if "character_id" in (prompt.get("me") or {}):  # answer prompts carry only my type (FIX-B32)
                first.setdefault(prompt["me"]["character_id"], prompt)
        by_char = {p["character_id"]: aid for aid, p in roster.items()}
        if not first:
            check(False, f"P3 seed {attempt['seed']}: no prompts found")
        repeat, before = 0, len(failures)
        for cid, prompt in first.items():
            aid = by_char.get(cid)
            exp = prompt.get("experience") or {}
            # games this agent finished before this one, in the engine's own order (counts
            # aborted games too: the agent did live through them)
            past = [h["game_id"] for h in players[aid]["history"]]
            expected = past.index(gid) if gid in past else len(past)
            # lessons are written at game end, so an agent whose only past games were aborted
            # attempts (not completed) correctly has none yet
            finished_before = [g for g in past[:expected] if g in completed_ids]
            ok = exp.get("games_played") == expected and (not finished_before or exp.get("lessons"))
            repeat += expected > 0
            if not ok:
                check(False, f"P3 seed {attempt['seed']}: {aid[-8:]} expected {expected} past games, prompt shows {exp.get('games_played')}")
        check(len(failures) == before and len(first) == len(roster),
              f"P3 seed {attempt['seed']}: every first prompt reports the right past "
              f"({repeat} returning agents, {len(first) - repeat} newcomers, {len(first)}/{len(roster)} prompts seen)")
        summary_path = Path(attempt["summary_file"]) if attempt.get("summary_file") else None
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path and summary_path.exists() else {}
        hooks = summary.get("end_hooks", [])
        check(hooks and not any(str(h).startswith("failed") for h in hooks), f"seed {attempt['seed']}: {len(hooks)} game-end hooks ran")
        questions = [json.loads(raw) for (raw,) in db.execute(
            "SELECT state FROM competitive_records WHERE game_id=? AND kind='question'", (gid,))]
        # the engine stores timed-out questions as 'completed' with mandatory_fallback=True,
        # so status alone always read "expired 0"; count answers the engine had to supply
        # questions to someone holding nothing are answered by the engine at once (no reply is
        # possible); only replies that missed the deadline count (mandatory_reason, FIX-A15)
        answerable = [q for q in questions if q.get("successes") and q.get("mandatory_reason") not in ("no_information", "npc")]
        defaulted = sum(bool(q.get("mandatory_fallback")) and q.get("mandatory_reason", "timeout") == "timeout"
                        for q in answerable)
        check(defaulted <= 0.02 * max(1, len(answerable)),
              f"seed {attempt['seed']}: agents answered their questions ({len(questions)} questions, "
              f"{len(answerable)} needed an answer, {defaulted} settled by the engine default; "
              f"raise --question-timeout if this grows); answer repairs {summary.get('answer_repairs', 'n/a')}")
        calls, stand_ins = summary.get("model_calls", 0), summary.get("model_fallbacks", 0)
        check(calls > 0 and stand_ins <= 0.1 * (calls + stand_ins),
              f"seed {attempt['seed']}: the model answered ({calls} calls, {stand_ins} rule-based stand-ins)")
        if "say_do_mismatches" in summary:  # FIX-B13
            print(f"info seed {attempt['seed']}: deduction outputs rejected for say/do mismatch "
                  f"{summary['say_do_mismatches'] or 0}")
        for aid in roster:
            played_before[aid] += 1
            completed_by.setdefault(aid, []).append(gid)
    # engine-side learning and history: every completed game is in each participant's history
    for aid, v in sorted(players.items(), key=lambda x: x[1]["fixed_mbti"]):
        learning = json.loads(db.execute("SELECT state FROM agent_learning WHERE agent_id=?", (aid,)).fetchone()[0])
        missing = [g for g in completed_by.get(aid, []) if g not in {h["game_id"] for h in v["history"]}]
        if missing:
            check(False, f"history of {aid[-8:]} lacks {len(missing)} completed game(s)")
        if played_before[aid] and learning["version"] == 0:
            check(False, f"{v['fixed_mbti']} {aid[-8:]} played but strategy_state was never updated")
    check(not [f for f in failures if f.startswith("history") or "strategy_state" in f],
          "engine history and strategy_state advanced for every agent that played")
    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
