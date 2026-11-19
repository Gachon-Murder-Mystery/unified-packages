"""Review a campaign: code-level health + game outcomes. Usage: review_campaign.py <root> <campaign>"""
import collections
import json
import sqlite3
import statistics as st
import sys
from pathlib import Path

root, camp = Path(sys.argv[1]), sys.argv[2]
cdir = root / 'runs' / camp
b = json.loads((cdir / 'batch.json').read_text(encoding='utf-8'))
db = sqlite3.connect(cdir / 'pool.sqlite3')
NEW = {'M52', 'M53', 'M54', 'M55', 'M56'}
rows = []
for a in b['attempts']:
    if a['status'] != 'completed':
        print('attempt', a['seed'], a['status']); continue
    gid = a['game_id']
    s = json.loads(Path(a['summary_file']).read_text(encoding='utf-8'))
    g = json.loads(db.execute('select state from games where id=?', (gid,)).fetchone()[0])
    qs = [json.loads(r[0]) for r in db.execute("select state from competitive_records where game_id=? and kind='question'", (gid,))]
    detail = collections.Counter()
    invalid = collections.Counter()
    for line in open(root / 'runs' / gid / 'detail.jsonl', encoding='utf-8'):
        e = json.loads(line)
        if e['event'] == 'agent_journal':
            r = e.get('record') or {}
            if r.get('event') in ('invalid_output', 'invalid_trade', 'invalid_trade_response'):
                invalid[(r.get('event'), str(r.get('reason') or r.get('raw') or '')[:60])] += 1
        else:
            detail[e['event']] += 1
    fin, acts, acc, conf = [], collections.Counter(), [], []
    for line in open(root / 'runs' / gid / 'codex-calls.jsonl', encoding='utf-8'):
        x = json.loads(line)
        try:
            u = json.loads(x['user']); r = json.loads(x['response'])
        except Exception:
            continue
        if not isinstance(u, dict) or 'phase' not in u:
            continue
        if u['phase'] == 'deduction' and u.get('cycle') == g['rules']['deadline_cycles']:
            fin.append((u.get('accuse_math') or {}).get('combinations_left'))
        if u['phase'] == 'interaction':
            acts[(r.get('action') or {}).get('type')] += 1
        if u['phase'] == 'deduction' and (r.get('action') or {}).get('type') == 'accuse':
            am = u.get('accuse_math') or {}
            acc.append((u['cycle'], am.get('combinations_left'), am.get('score_if_wrong')))
    fin = [f for f in fin if f is not None]
    alive_def = [sum(x.get('kind') == 'definite' for x in g['characters'][str(p['character_id'])]['information'].values())
                 for p in g['players'].values() if p['mode'] == 'active'] or [0]
    trades = g['trades'].values()
    missions = [m for p in g['players'].values() for m in p['missions']]
    rows.append(dict(
        seed=a['seed'], res=s['resolution'], cyc=s['cycles'], min=round(s['elapsed_seconds'] / 60),
        winners=len(s['winners']), deaths=len(s['deaths']), calls=s['model_calls'], standins=s['model_fallbacks'],
        saydo=s.get('say_do_mismatches'), repairs=s.get('answer_repairs'),
        q=len(qs), q_timeout=sum(q.get('mandatory_reason') == 'timeout' for q in qs),
        extra=sum(q.get('extra_vague', 0) for q in qs),
        trades=dict(collections.Counter(t['status'] for t in trades)),
        bundles=dict(collections.Counter(f"{len(t.get('declared',{}).get(t['proposer'], t['offers'].get(t['proposer'], [])))}v{len(t.get('requested') or [])}" for t in trades)),
        deceptions=sum(t['status'] == 'settled' and any((t.get('withheld') or {}).values()) for t in trades),
        talk=dict(question=sum(bool(q.get('asker_message')) for q in qs), answer=sum(bool(q.get('answer_message')) for q in qs),
                  trade=sum(len(t.get('messages') or []) for t in trades)),
        excludes=sum(any(i.get('exclude') for i in (t.get('requested') or []) if isinstance(i, dict)) for t in trades),
        replacements=detail.get('replacement_granted', 0), errors=detail.get('tool_error', 0),
        combos=(st.median(fin) if fin else None, min(fin) if fin else None),
        definite=(st.median(alive_def), max(alive_def)), accusations=acc, acts=dict(acts),
        room_taken=len(g.get('room_taken', [])),
        new_missions=dict(collections.Counter(m['mission_id'] for m in missions if m['mission_id'] in NEW)),
        new_achieved=[m['mission_id'] for m in missions if m['mission_id'] in NEW and m['achieved']],
        invalid=invalid.most_common(6)))
for r in rows:
    print(json.dumps(r, ensure_ascii=False))
won = sum(r['winners'] > 0 for r in rows)
print(f'games with a winner: {won}/{len(rows)}')
