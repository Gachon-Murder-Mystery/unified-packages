"""[FIX-C16] Memory boundary audit (textbook II ch13, shared memory & context boundaries).

    python tools/audit_memory.py runs/<campaign>

User decision 2026-11-09: every agent and the GM hold their own memory; nothing is shared.
Checks (a violation makes the exit code 1):
  owner        each file belongs to the agent in its name (agent_id matches the file name)
  played       every game in a player's memory is a game that player actually played (pool DB)
  roles        player files hold no GM memory (playbook, norms, escalations) and the GM file
               holds no player memory (lessons, insights, peers)
  secrets      no engine secret of any game (role tokens, roll secrets, observer tokens) appears
               in any memory file
  reports      the GM's own memory holds none of the agents' relationship reports (FIX-A35: they go
               to the GM's REPORT as a record, never into its memory)
  live state   in-game working memory (situation cards, live notes) is not persisted between
               games; only the final notes inside a game record (input to the reflection) are
Reported, not a violation:
  convergence  how similar the agents' insights are (mean pairwise Jaccard of insight texts,
               insights shared word-for-word by several agents). The project wants agents to
               stay different, so a rising value is a warning sign.
"""
import itertools
import json
import re
import sqlite3
import sys
from pathlib import Path

GM_KEYS = {"playbook", "norms", "escalations"}
PLAYER_KEYS = {"lessons", "insights", "peers"}
LIVE_KEYS = {"situation", "cards", "notes", "seek"}


def _items(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _items(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _items(v)


def _counts(value):
    return isinstance(value, dict) and all(isinstance(v, (int, float)) for v in value.values())


def _norm(text):
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def _secrets(db):
    out, players = set(), {}
    if not db.exists():
        return out, players
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for gid, raw in con.execute("select id, state from games"):
            g = json.loads(raw)
            players[gid] = set((g.get("players") or {}).keys())
            for key in ("roll_secret", "observer_token"):
                if isinstance(g.get(key), str) and len(g[key]) >= 16:
                    out.add(g[key])
            ctl = g.get("controller") or {}
            if isinstance(ctl.get("token"), str):
                out.add(ctl["token"])
            for p in (g.get("players") or {}).values():
                if isinstance(p, dict) and isinstance(p.get("token"), str):
                    out.add(p["token"])
    finally:
        con.close()
    return out, players


def audit(campaign):
    campaign = Path(campaign)
    mem = campaign / "agent-memory"
    secrets, players = _secrets(campaign / "pool.sqlite3")
    violations, notes = [], []
    insights = {}
    files = sorted(mem.glob("*.json"))
    for f in files:
        text = f.read_text(encoding="utf-8")
        data = json.loads(text)
        is_gm = f.name.startswith("gm_")
        aid = data.get("agent_id", "")
        expected = f.stem[3:] if is_gm else f.stem
        if aid.replace(":", "_") != expected:
            violations.append(f"owner: {f.name} holds agent_id {aid!r}")
        top = set(data)
        if is_gm and any(k in text for k in ('"relations"', '"reported_relationships"', '"affinity"')):
            violations.append(f"roles: GM file {f.name} holds the agents' relationship reports (record-only, FIX-A35)")
        if is_gm and top & PLAYER_KEYS:
            violations.append(f"roles: GM file {f.name} holds player memory {sorted(top & PLAYER_KEYS)}")
        if not is_gm and top & GM_KEYS:
            violations.append(f"roles: player file {f.name} holds GM memory {sorted(top & GM_KEYS)}")
        if not is_gm and top & LIVE_KEYS:
            violations.append(f"live state: {f.name} persists in-game memory {sorted(top & LIVE_KEYS)}")
        leaked = [s for s in secrets if s in text]
        if leaked:
            violations.append(f"secrets: {f.name} contains {len(leaked)} engine secret(s)")
        if not is_gm:
            for g in data.get("games") or []:
                gid = g.get("game_id")
                if players and gid in players and aid not in players[gid]:
                    violations.append(f"played: {f.name} remembers game {gid} it did not play")
                # counts such as stats.situation {"characters_with_evidence": 3} are a summary, not state
                deep = {k for k, v in _items(g) if k in LIVE_KEYS and k != "notes" and not _counts(v)}
                if deep:
                    violations.append(f"live state: {f.name} game {gid} persists {sorted(deep)}")
            insights[aid] = {_norm(i.get("text")) for i in data.get("insights") or [] if i.get("text")}
    pairs = [(a, b) for a, b in itertools.combinations(sorted(insights), 2) if insights[a] or insights[b]]
    jac = [len(insights[a] & insights[b]) / len(insights[a] | insights[b]) for a, b in pairs]
    counts = {}
    for s in insights.values():
        for t in s:
            counts[t] = counts.get(t, 0) + 1
    shared = sorted(((n, t) for t, n in counts.items() if n > 1), reverse=True)
    notes.append(f"files: {len(files)} ({sum(f.name.startswith('gm_') for f in files)} GM)")
    notes.append(f"convergence: mean pairwise Jaccard of insights {round(sum(jac) / len(jac), 3) if jac else 'n/a'} "
                 f"over {len(pairs)} pairs; insights held word-for-word by 2+ agents: {len(shared)}")
    for n, t in shared[:5]:
        notes.append(f"  shared by {n}: {t[:100]}")
    return violations, notes


def main(argv):
    if len(argv) != 1:
        raise SystemExit(__doc__)
    violations, notes = audit(argv[0])
    for line in notes:
        print(line)
    for line in violations:
        print("VIOLATION", line)
    print("audit:", "OK" if not violations else f"{len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
