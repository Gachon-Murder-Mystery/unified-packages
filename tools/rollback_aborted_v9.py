"""[v9] Undo what an aborted game left in a shared pool, so the rerun of its seed starts from the same state.

v9 agents save `peers` (deal counts, affinity, why, their_attitude) during a game, not only at its end.
The v8 tool (rollback_aborted.py) undoes deal counts and the engine side; this tool also rebuilds each
agent's affinity / why / their_attitude as they were before the aborted game, by replaying the agents'
own journals ('reaction' records) of every completed game of the campaign in order:
  affinity        = 'after' of my last reaction to that peer (AFFINITY_START if none)
  why             = last AFFINITY_WHY_KEEP reasons whose reaction changed the score
  their_attitude  = their_attitude_to_me of my last reaction to that peer
Check: agents that did not play the aborted game still hold their state from before it, so the rebuild
must reproduce their files exactly (--check prints mismatches; exit 1 if any).

usage: python tools/rollback_aborted_v9.py <batch_dir> <aborted_game_id_prefix> [--apply] [--after-b51]
--after-b51: campaigns run with FIX-B51, where an agent saves every reaction (not only up to its last
successful GM report); then every reaction of a completed game is replayed.
Runs the v8 tool for the engine side and deal counts first (same backup), then rewrites the peers.
"""
import json, sqlite3, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Agent_Reasoning'))
from agent import AFFINITY_START, AFFINITY_WHY_KEEP   # noqa: E402

batch, prefix, apply = Path(sys.argv[1]), sys.argv[2], '--apply' in sys.argv
after_b51 = '--after-b51' in sys.argv
b = json.loads((batch / 'batch.json').read_text(encoding='utf-8'))
done = [a for a in b['attempts'] if a['status'] == 'completed']
db = sqlite3.connect(f'file:{batch / "pool.sqlite3"}?mode=ro', uri=True)
gid = db.execute("select id from games where id like ?", (prefix + '%',)).fetchone()[0]
aborted = json.loads(db.execute("select state from games where id=?", (gid,)).fetchone()[0])
played = set(aborted['players'])
full_id = {aid[-6:]: aid for (aid,) in db.execute("select agent_id from agents")}
db.close()


def replay():
    """agent_id -> peer suffix -> {affinity, why, their_attitude} after all completed games."""
    state = {}
    pool = sqlite3.connect(f'file:{batch / "pool.sqlite3"}?mode=ro', uri=True)
    for a in done:
        gdir = Path(a['full_record_file']).parent
        lines = [json.loads(line) for line in open(gdir / 'detail.jsonl', encoding='utf-8')]
        # the journal's reaction record had no 'delta' until 2026-11-12: take it from the model's own answer
        # (codex-calls), matched by the reason text the agent stored (first 80 characters)
        asked = {}
        calls = gdir / 'codex-calls.jsonl'
        if calls.exists():
            for line in open(calls, encoding='utf-8'):
                try:
                    resp = json.loads(json.loads(line).get('response') or '')
                except ValueError:
                    continue
                for x in (resp.get('reaction') or []) if isinstance(resp, dict) else []:
                    if isinstance(x, dict) and isinstance(x.get('delta'), (int, float)):
                        asked[str(x.get('why') or '').strip()[:80]] = x['delta']
        # Until FIX-B51 peers were saved only right after a successful report to the GM, and a report is
        # sent only when the scores changed. Reactions after an agent's last successful report of the game
        # (final cycle, refused 409 after the end, or no score change) never reached its file. The engine's
        # relations_reported events (with timestamps) give the save points.
        saved_until = {}
        for (raw,) in pool.execute("select state from competitive_events where game_id=? and state like '%relations_reported%'",
                                   (a['game_id'],)):
            ev = json.loads(raw)
            if ev.get('type') == 'relations_reported':
                aid = ev['payload']['agent_id']
                saved_until[aid] = max(saved_until.get(aid, 0), ev['timestamp'])
        for e in lines:
            r = e.get('record') or {}
            if not after_b51 and r.get('event') == 'reaction' and float(e.get('timestamp') or 0) > saved_until.get(e.get('agent_id'), 0):
                continue
            if e.get('event') != 'agent_journal' or r.get('event') not in ('reaction', 'impression'):
                continue
            assert r['event'] == 'reaction', 'impression records present: extend the replay'
            s = state.setdefault(e['agent_id'], {}).setdefault(r['agent'], {'why': []})
            s['affinity'] = r['after']
            s['their_attitude'] = r['their_attitude_to_me']
            # the agent keeps the reason whenever the change it asked for was not 0, even when the score sat at
            # 0 or 100 and did not move (agent._apply_reactions: `if delta:`); 'delta' is in the journal record
            delta = r['delta'] if 'delta' in r else asked.get(r['why'])
            changed = delta != 0 if delta is not None else r['after'] != r['before']
            if changed:
                s['why'] = (s['why'] + [r['why']])[-AFFINITY_WHY_KEEP:]
    return state


def rebuilt(peers, mine):
    """peers dict (deal counts already correct) with affinity fields replaced by the replay."""
    out = {}
    for pid, v in peers.items():
        rec = {'fulfilled': v.get('fulfilled', 0), 'broken': v.get('broken', 0)}
        # [B55] post-game shares happen only after a completed game: an aborted game never changes them, keep as is
        rec.update({g: int(v[g]) for g in ('gifts_received', 'gifts_given') if v.get(g)})
        s = mine.get(pid[-6:])
        if s:
            rec.update(affinity=float(s['affinity']), why=list(s['why']))
            rec['their_attitude'] = s['their_attitude']
        out[pid] = rec
    for suffix, s in mine.items():      # a peer known only through reactions (or whose deal counts the v8 step zeroed)
        pid = full_id[suffix]
        if pid not in out:
            out[pid] = {'fulfilled': 0, 'broken': 0, 'affinity': float(s['affinity']), 'why': list(s['why']),
                        'their_attitude': s['their_attitude']}
    return out


state = replay()
mem = {json.loads(p.read_text(encoding='utf-8'))['agent_id']: p for p in (batch / 'agent-memory').glob('society_*.json')}
bad = 0
for aid, p in mem.items():
    if aid in played:
        continue
    d = json.loads(p.read_text(encoding='utf-8'))
    want = rebuilt(d.get('peers') or {}, state.get(aid, {}))
    if set(want) != set(d.get('peers') or {}):
        bad += 1
        print('MISMATCH peer set', aid[-6:], sorted(set(want) ^ set(d.get('peers') or {})))
    for pid, v in (d.get('peers') or {}).items():
        w = want.get(pid, {})
        same = (round(v.get('affinity', AFFINITY_START), 3) == round(w.get('affinity', AFFINITY_START), 3)
                and list(v.get('why') or []) == list(w.get('why') or [])
                and v.get('their_attitude') == w.get('their_attitude'))
        if not same:
            bad += 1
            print('MISMATCH (not in aborted game)', aid[-6:], pid[-6:], v, w)
print(f'check on {sum(a not in played for a in mem)} agents outside the aborted game: '
      f'{"OK" if not bad else str(bad) + " mismatch(es)"}')
if bad:
    raise SystemExit(1)

if not apply:
    subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('rollback_aborted.py')), str(batch), prefix], check=True)
    print('dry run ok')
    raise SystemExit(0)

subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('rollback_aborted.py')), str(batch), prefix, '--apply'], check=True)
for aid in played:
    p = mem.get(aid)
    if not p:
        continue
    d = json.loads(p.read_text(encoding='utf-8'))
    d['peers'] = rebuilt(d.get('peers') or {}, state.get(aid, {}))
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
print('peers rebuilt for', len(played & set(mem)), 'agents')
