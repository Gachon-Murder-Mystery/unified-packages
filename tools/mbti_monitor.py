"""Per-game MBTI axis behaviour gaps (convergence monitor). Usage: mbti_monitor.py <root> <campaign>"""
import collections
import json
import sqlite3
import statistics as st
import sys
from pathlib import Path

root, camp = Path(sys.argv[1]), sys.argv[2]
cdir = root / 'runs' / camp
b = json.loads((cdir / 'batch.json').read_text(encoding='utf-8'))
db = sqlite3.connect(f'file:{cdir / "pool.sqlite3"}?mode=ro', uri=True)
METRICS = ('question', 'explore', 'trade', 'prepare', 'accept', 'counter', 'hostile', 'persuade', 'share_def', 'deceive', 'talk_len', 'accuse')
AXES = ('EI', 'SN', 'TF', 'JP')


def metrics(c):
    act = max(1, c['act']); tr = max(1, c['tr_accept'] + c['tr_refuse'] + c['tr_counter']); qn = max(1, c['a_question'])
    return dict(question=c['a_question'] / act, explore=c['a_explore'] / act, trade=c['a_trade'] / act,
                prepare=c['a_prepare'] / act, accept=c['tr_accept'] / tr, counter=c['tr_counter'] / tr,
                hostile=(c['ap_interrogate'] + c['ap_threaten']) / qn, persuade=c['ap_persuade'] / qn,
                share_def=c['ans2_def'] / max(1, c['ans2']), deceive=c['deceive'] / max(1, c['settled']),
                talk_len=c['talk_chars'] / max(1, c['talks']), accuse=c['accuse'])


for a in b['attempts']:
    if a['status'] != 'completed':
        continue
    gid = a['game_id']
    g = json.loads(db.execute('select state from games where id=?', (gid,)).fetchone()[0])
    c2a = {str(p['character_id']): aid for aid, p in g['players'].items()}
    mbti = {aid: g['characters'][str(p['character_id'])]['mbti'] for aid, p in g['players'].items()}
    per = collections.defaultdict(collections.Counter)
    for line in open(root / 'runs' / gid / 'codex-calls.jsonl', encoding='utf-8'):
        x = json.loads(line)
        try:
            u = json.loads(x['user']); r = json.loads(x['response'])
        except Exception:
            continue
        if not isinstance(u, dict) or not isinstance(r, dict):
            continue
        aid = c2a.get(str((u.get('me') or {}).get('character_id')))
        if 'incoming_trade' in u:
            if aid: per[aid]['tr_' + str(r.get('decision'))] += 1
            continue
        if not aid or 'response_options' in u:
            continue
        if u.get('phase') == 'interaction':
            act = r.get('action') or {}; t = act.get('type'); per[aid]['act'] += 1; per[aid]['a_' + str(t)] += 1
            pl = act.get('payload') or {}
            if t == 'question': per[aid]['ap_' + str(pl.get('approach'))] += 1
            if pl.get('message'): per[aid]['talks'] += 1; per[aid]['talk_chars'] += len(pl['message'])
        if u.get('phase') == 'deduction' and (r.get('action') or {}).get('type') == 'accuse':
            per[aid]['accuse'] += 1
    kind = {c['id']: c['kind'] for c in g['clues']}
    for (s,) in db.execute("select state from competitive_records where game_id=? and kind='question'", (gid,)):
        q = json.loads(s)
        if q.get('successes', 0) >= 2 and q['target_agent'] in g['players']:
            per[q['target_agent']]['ans2'] += 1
            per[q['target_agent']]['ans2_def'] += any(kind.get(i) == 'definite' for i in q.get('transferred_ids', []))
    for t in g['trades'].values():
        if t['status'] == 'settled':
            for x, w in (t.get('withheld') or {}).items():
                if x in g['players']: per[x]['settled'] += 1; per[x]['deceive'] += bool(w)
    rows = {aid: metrics(c) for aid, c in per.items()}
    out = {}
    for axis in AXES:
        gaps = {}
        for k in METRICS:
            a1 = [r[k] for aid, r in rows.items() if axis[0] in mbti[aid]]
            a2 = [r[k] for aid, r in rows.items() if axis[1] in mbti[aid]]
            if a1 and a2: gaps[k] = round(st.mean(a1) - st.mean(a2), 2)
        out[axis] = gaps
    spread = {k: round(st.pstdev([r[k] for r in rows.values()]), 3) for k in METRICS if rows}
    print(json.dumps({'seed': a['seed'], 'axis_gap(first-second)': out, 'spread': spread}, ensure_ascii=False))
