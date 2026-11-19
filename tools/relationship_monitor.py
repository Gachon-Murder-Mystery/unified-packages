"""Do T and F separate where their prompts say they should? (relationship-conditional behaviour)

    PYTHONUTF8=1 python -B tools/relationship_monitor.py . <campaign>

Background (v8, 2026-11-09): tools/mbti_monitor.py measures unconditional rates, which hide the
T/F difference because the T/F tendencies are conditional on the relationship. This tool measures:
  accept      acceptance of incoming trades I could fulfil, by in-game relationship bucket, T vs F
  approach    question approaches (persuade, interrogate+threaten) by relationship bucket, T vs F
  deception   deceptions as giver in settled trades, T vs F
  affinity    [v9, FIX-B43] each agent's own impressions (journal 'impression'): count, mean |delta|,
              ups vs downs, by T vs F; spread of the final reported scores (a uniform rise would mean
              the scores do not discriminate); the GM's recorded summary (FIX-A35) if present
"""
import collections
import json
import sqlite3
import statistics as st
import sys
from pathlib import Path


def bucket(r):
    return 'high(>=60)' if r >= 60 else 'low(<=40)' if r <= 40 else 'mid'


def main(root, camp_name):
    root = Path(root); camp = root / 'runs' / camp_name
    b = json.loads((camp / 'batch.json').read_text(encoding='utf-8'))
    db = sqlite3.connect(f'file:{camp / "pool.sqlite3"}?mode=ro', uri=True)
    acc = collections.defaultdict(lambda: [0, 0])
    approach = collections.defaultdict(collections.Counter)
    decept = collections.defaultdict(lambda: [0, 0])
    imp = collections.defaultdict(list)
    finals = []
    for a in b['attempts']:
        if a['status'] != 'completed':
            continue
        gid = a['game_id']
        g = json.loads(db.execute('select state from games where id=?', (gid,)).fetchone()[0])
        mbti_of_char = {int(k): c.get('mbti') for k, c in g['characters'].items()}
        mbti_of_agent = {p['agent_id']: mbti_of_char[p['character_id']] for p in g['players'].values()}
        gdir = root / 'runs' / gid
        calls = gdir / 'codex-calls.jsonl'
        for line in open(calls, encoding='utf-8') if calls.exists() else []:
            x = json.loads(line)
            try:
                u = json.loads(x['user']); r = json.loads(x['response'])
            except Exception:
                continue
            if not isinstance(u, dict) or not isinstance(r, dict):
                continue
            m = (u.get('me') or {}).get('mbti') or ''
            if len(m) != 4:
                continue
            tf = m[2]
            if 'incoming_trade' in u and u.get('clues_fitting_their_request') and u.get('relationship_to_them') is not None:
                k = (tf, bucket(u['relationship_to_them']))
                acc[k][1] += 1
                acc[k][0] += r.get('decision') == 'accept'
            if u.get('phase') == 'interaction' and (r.get('action') or {}).get('type') == 'question':
                pay = r['action'].get('payload') or {}
                rel = (u.get('relationships') or {}).get(str(pay.get('target_id')))
                approach[(tf, bucket(rel) if isinstance(rel, (int, float)) else 'unknown')][pay.get('approach') or '?'] += 1
        for t in g['trades'].values():
            if t['status'] == 'settled':
                for giver, withheld in (t.get('withheld') or {}).items():
                    m = mbti_of_agent.get(giver) or ''
                    if len(m) == 4:
                        decept[m[2]][1] += 1
                        decept[m[2]][0] += bool(withheld)
        detail = gdir / 'detail.jsonl'
        for line in open(detail, encoding='utf-8') if detail.exists() else []:
            e = json.loads(line)
            rec = e.get('record') or {}
            if e.get('event') == 'agent_journal' and rec.get('event') == 'impression':
                m = mbti_of_agent.get(e.get('agent_id')) or ''
                if len(m) == 4:
                    imp[m[2]].append(rec.get('after', 0) - rec.get('before', 0))
        rel = ((g.get('gm_report') or {}).get('reported_relationships') or {})
        for scores in (rel.get('latest') or {}).values():
            finals.extend(scores.values())

    print('accept rate when I hold a fitting clue:')
    for bk in ('high(>=60)', 'mid', 'low(<=40)'):
        print(f"  {bk:11s} " + ' | '.join(f"{tf} {acc[(tf, bk)][0]}/{acc[(tf, bk)][1]}" +
                                         (f" = {acc[(tf, bk)][0] / acc[(tf, bk)][1]:.0%}" if acc[(tf, bk)][1] else '')
                                         for tf in ('T', 'F')))
    print('question approaches:')
    for k in sorted(approach):
        c = approach[k]; n = sum(c.values())
        print(f"  {k}: n={n} persuade {c.get('persuade', 0) / n:.0%} interrogate+threaten "
              f"{(c.get('interrogate', 0) + c.get('threaten', 0)) / n:.0%}")
    print('deception as giver:', {k: f"{v[0]}/{v[1]}" for k, v in decept.items()})
    if imp:
        print('impressions (own affinity changes):')
        for tf in ('T', 'F'):
            d = imp.get(tf) or []
            if d:
                print(f"  {tf}: n={len(d)} up {sum(x > 0 for x in d)} down {sum(x < 0 for x in d)} "
                      f"mean |delta| {st.mean(abs(x) for x in d):.1f}")
    if finals:
        print(f"reported affinity at game end (GM record): n={len(finals)} mean {st.mean(finals):.1f} "
              f"sd {st.pstdev(finals):.1f} min {min(finals)} max {max(finals)}")


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
