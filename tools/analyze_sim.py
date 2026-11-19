"""Summarise runs/sim-capacity/results.jsonl per scenario x job x cycle."""
import collections
import json
import statistics as st
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'Game-engine'))
from competitive_rules import SHEETS  # noqa: E402

rows = [json.loads(l) for l in open(root / 'runs' / 'sim-capacity' / 'results.jsonl', encoding='utf-8')]
errors = [r for r in rows if 'error' in r]
rows = [r for r in rows if 'error' not in r]
print('games', collections.Counter(r['scenario'] for r in rows), 'errors', len(errors))
for e in errors[:3]:
    print(' ERR', e['scenario'], e['seed'], e['error'])


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))]


for sc in ('A', 'B'):
    rs = [r for r in rows if r['scenario'] == sc]
    if not rs:
        continue
    dry = [r['dry_cycle'] for r in rs if r['dry_cycle']]
    g = collections.Counter()
    for r in rs:
        for k, v in r['game']['trades'].items(): g['trade_' + k] += v
        g['questions'] += r['game']['questions']
    print(f'\n######## scenario {sc}: {len(rs)} games | rooms empty at cycle median {st.median(dry)} (range {min(dry)}-{max(dry)}) | per game {dict((k, round(v / len(rs), 1)) for k, v in g.items())}')
    for cyc in (10, 15, 20, 25, 30):
        print(f'\n-- cycle {cyc}: job | stats(obs/speech/pressure) | usable p10/p50/p90 | definite p10/p50/p90 | combos p10/p50/p90 | solved% | definite S/W/P mean | indirect CR/WR/CO mean | pairs mean | source mean')
        allu, alld, allc, solved = [], [], [], 0
        for job in range(12):
            u, d, c, dc, ic, pr, src = [], [], [], collections.Counter(), collections.Counter(), [], collections.Counter()
            for r in rs:
                snap = next(s for s in r['snapshots'] if s['cycle'] == cyc)
                p = snap['players'][str(job)]
                u.append(p['usable']); d.append(sum(p['definite'].values())); c.append(p['combos']); pr.append(p['pairs'])
                dc.update(p['definite']); ic.update(p['indirect']); src.update(p['source'])
            n = len(rs)
            s = SHEETS[job]['stats']
            stats = f"{s.get('observe')}/{s.get('speech')}/{s.get('pressure')}"
            sol = sum(x == 1 for x in c)
            allu += u; alld += d; allc += c; solved += sol
            print(f"job {job:2d} {SHEETS[job]['job'][:6]:6s} {stats:7s} | {pct(u,.1):2d}/{pct(u,.5):2d}/{pct(u,.9):2d} | {pct(d,.1):2d}/{pct(d,.5):2d}/{pct(d,.9):2d} | "
                  f"{pct(c,.1):3d}/{pct(c,.5):3d}/{pct(c,.9):3d} | {100*sol/n:5.1f}% | "
                  f"{dc['suspect']/n:.1f}/{dc['weapon']/n:.1f}/{dc['place']/n:.1f} | {ic['character_room']/n:.1f}/{ic['weapon_room']/n:.1f}/{ic['companion']/n:.1f} | "
                  f"{st.mean(pr):.1f} | " + ' '.join(f"{k}:{v/n:.1f}" for k, v in sorted(src.items())))
        print(f"ALL          | {pct(allu,.1)}/{pct(allu,.5)}/{pct(allu,.9)} | {pct(alld,.1)}/{pct(alld,.5)}/{pct(alld,.9)} | {pct(allc,.1)}/{pct(allc,.5)}/{pct(allc,.9)} | {100*solved/len(allc):.1f}%")
