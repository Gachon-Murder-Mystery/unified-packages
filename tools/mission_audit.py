"""Per-mission achievement audit across the recorded 30-cycle campaigns (read-only).

For every personal mission: how often it was handed out, how often it was achieved, when, and at
which tier, split by model era. Also lists missions never handed out and whether the 30-cycle
feasibility check (A10) would block them. Output: runs/mission_audit.json + a printed table.
"""
import collections
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
V9 = ROOT / 'unified-packages-v9'
sys.path.insert(0, str(V9 / 'Game-engine'))
import competitive_rules as CR  # noqa: E402

# (name, era, path). Era = model; rules also changed between campaigns (see CHANGES_EXPERIMENT30.md).
CAMPS = [
    ('v4-30c', 'gpt-6-luna', ROOT / 'unified-packages-fixed' / 'runs' / 'v4-30c'),
    ('v8', 'gpt-6-luna', ROOT / 'unified-packages-fixed' / 'runs' / 'v8-main-30c'),
    ('chk2', 'gpt-6.1-terra', V9 / 'runs' / 'v9-check2-30c'),
    ('chk6', 'gpt-6.1-terra', V9 / 'runs' / 'v9-check6-30c'),
    ('v9a', 'gpt-6.1-terra', V9 / 'runs' / 'v9-main-30c'),
    ('chk7', 'gpt-6.1-terra', V9 / 'runs' / 'v9-check7-30c'),
    ('chk8', 'gpt-6.1-terra', V9 / 'runs' / 'v9-check8-30c-lean'),
    ('v9b', 'gpt-6.1-terra', V9 / 'runs' / 'v9-main2-30c-lean'),
]
FULL = 28  # a game that ran at least this many cycles counts as "full length"


def completed(camp):
    by_seed = {}
    for a in json.loads((camp / 'batch.json').read_text(encoding='utf-8'))['attempts']:
        if a['status'] == 'completed':
            by_seed[a['seed']] = a
    return [by_seed[s] for s in sorted(by_seed)]


def rows():
    out = []
    for name, era, camp in CAMPS:
        db = sqlite3.connect(f'file:{camp / "pool.sqlite3"}?mode=ro', uri=True)
        for a in completed(camp):
            r = db.execute('select state from games where id=?', (a['game_id'],)).fetchone()
            if not r:
                continue
            g = json.loads(r[0])
            cycles = a.get('cycles') or len(g.get('cycle_history', []))
            for aid, p in g['players'].items():
                for m in p.get('missions', []):
                    out.append({'camp': name, 'era': era, 'game': a['game_id'], 'cycles': cycles,
                                'agent': aid, 'char': p.get('character_id'), 'mbti': p.get('mbti'),
                                'mission': m['mission_id'], 'tier': m.get('tier'),
                                'achieved': bool(m.get('achieved')), 'cycle': m.get('achieved_cycle')})
    return out


def table(rs):
    by = collections.defaultdict(list)
    for r in rs:
        by[r['mission']].append(r)
    res = {}
    for mid, xs in by.items():
        full = [x for x in xs if x['cycles'] >= FULL]
        done = [x for x in xs if x['achieved']]
        cyc = [x['cycle'] for x in done if x['cycle'] is not None]
        res[mid] = {
            'assigned': len(xs), 'achieved': len(done), 'rate': round(len(done) / len(xs), 3),
            'assigned_full': len(full), 'achieved_full': sum(x['achieved'] for x in full),
            'cycle_median': statistics.median(cyc) if cyc else None,
            'cycle_min': min(cyc) if cyc else None, 'cycle_max': max(cyc) if cyc else None,
            'tiers': dict(collections.Counter(x['tier'] for x in xs)),
            'tier_rate': {t: round(sum(x['achieved'] for x in xs if x['tier'] == t)
                                   / sum(1 for x in xs if x['tier'] == t), 3)
                          for t in {x['tier'] for x in xs}},
            'by_era': {e: [sum(1 for x in xs if x['era'] == e), sum(x['achieved'] for x in xs if x['era'] == e)]
                       for e in sorted({x['era'] for x in xs})},
        }
    return res


def main():
    rs = rows()
    res = table(rs)
    catalog = CR.MISSIONS
    never = {}
    for mid in catalog:
        if mid not in res:
            ok = CR.mission_feasible(mid, 30, 12)
            never[mid] = {'feasible_30c': bool(ok[0]), 'why': ok[1] if len(ok) > 1 else None,
                          'condition': catalog[mid]['condition']}
    tiers = collections.defaultdict(lambda: [0, 0])
    for r in rs:
        tiers[(r['era'], r['tier'])][0] += 1
        tiers[(r['era'], r['tier'])][1] += r['achieved']
    games = {(r['camp'], r['game']) for r in rs}
    out = {'games': len(games), 'agent_missions': len(rs),
           'games_by_camp': dict(collections.Counter(c for c, _ in games)),
           'tier_by_era': {f'{e} {t}': v for (e, t), v in sorted(tiers.items())},
           'missions': {m: {**res[m], 'condition': catalog[m]['condition'] if m in catalog else None}
                        for m in sorted(res, key=lambda m: (res[m]['rate'], -res[m]['assigned']))},
           'never_assigned': never}
    (V9 / 'runs' / 'mission_audit.json').write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    print('games', out['games'], 'agent-missions', out['agent_missions'], out['games_by_camp'])
    print('tier by era', out['tier_by_era'])
    for m, v in out['missions'].items():
        print(f"{m:4} {v['achieved']:3}/{v['assigned']:3} {v['rate']:.0%}  med {v['cycle_median']}  "
              f"tiers {v['tiers']}  era {v['by_era']}  | {(v['condition'] or '')[:60]}")
    print('never assigned:')
    for m, v in never.items():
        print(' ', m, v['feasible_30c'], v['why'], '|', v['condition'][:60])


if __name__ == '__main__':
    main()
