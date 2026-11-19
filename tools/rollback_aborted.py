"""Undo what an operator-stopped (aborted) game left in a shared pool, so the rerun of its seed starts
from the same state as if the aborted attempt never ran.

Touches only: experience_events of that game (+ the agent_learning aggregates they fed), engine
negotiation_reputation increments from its settled trades, and the agents' own peer counters in
agent-memory/*.json for those trades. Backs everything up first.
usage: python rollback_aborted.py <batch_dir> <game_id_prefix> [--apply]
"""
import json, shutil, sqlite3, sys, time
from pathlib import Path

batch, prefix, apply = Path(sys.argv[1]), sys.argv[2], '--apply' in sys.argv
db = sqlite3.connect(batch / 'pool.sqlite3')
gid, raw = db.execute("select id,state from games where id like ?", (prefix + '%',)).fetchone()
g = json.loads(raw)
assert g.get('resolution') == 'operator_stopped', g.get('resolution')
print('game', gid, 'cycle', g['cycle_id'])

if apply:
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bdir = batch / f'rollback-backup-{stamp}'
    bdir.mkdir()
    db.execute(f"VACUUM INTO '{(bdir / 'pool.sqlite3').as_posix()}'")
    shutil.copytree(batch / 'agent-memory', bdir / 'agent-memory')
    print('backup', bdir)

# 1. experience events -> learning aggregates
learning = {aid: json.loads(s) for aid, s in db.execute("select agent_id,state from agent_learning")}
events = [json.loads(s) for (s,) in db.execute("select state from experience_events where state like ?", (f'%"game_id": "{gid}"%',))]
for e in events:
    assert e['game_id'] == gid
    l = learning[e['agent_id']]
    for tag in (e['role'], *e['tags']):
        sc = l['competencies'][tag]
        sc['attempts'] -= 1
        sc['successes'] -= int(bool(e['feedback']['success']))
        assert sc['attempts'] >= 0 and sc['successes'] >= 0
        sc['estimate'] = (sc['successes'] + 1) / (sc['attempts'] + 2)
    l['feedback_version'] -= 1
print('experience events', len(events))

# 2./3. settled trades -> engine reputation and agents' own peer counters
agents = {aid: json.loads(s) for aid, s in db.execute("select agent_id,state from agents")}
mem = {p: json.loads(p.read_text(encoding='utf-8')) for p in (batch / 'agent-memory').glob('*.json')}
mem_by_agent = {d['agent_id']: (p, d) for p, d in mem.items()}
settled = [t for t in g['trades'].values() if t['status'] == 'settled']
for t in settled:
    a, b = t['proposer'], t['recipient']
    for x, partner in ((a, b), (b, a)):
        rep = agents[x]['negotiation_reputation']
        rep['fulfilled' if t['fulfilled'][x] else 'broken'] -= 1
        assert min(rep.values()) >= 0, (x, rep)
        # x's own judgement of partner (agent mirrors the engine's honesty check)
        p, d = mem_by_agent[x]
        rec = d['peers'].get(partner)
        key = 'fulfilled' if t['fulfilled'][partner] else 'broken'
        assert rec and rec[key] > 0, (x, partner, rec, key)
        rec[key] -= 1
        if rec['fulfilled'] == 0 and rec['broken'] == 0:
            del d['peers'][partner]
print('settled trades', len(settled), 'deceptions', sum(not v for t in settled for v in t['fulfilled'].values()))

if apply:
    for aid, l in learning.items():
        db.execute("update agent_learning set state=? where agent_id=?", (json.dumps(l), aid))
    db.execute("delete from experience_events where state like ?", (f'%"game_id": "{gid}"%',))
    for aid, a in agents.items():
        db.execute("update agents set state=? where agent_id=?", (json.dumps(a), aid))
    db.commit()
    for p, d in mem.items():
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    print('applied')
else:
    print('dry run ok')
