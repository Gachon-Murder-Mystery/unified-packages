"""[FIX-I28] Assignment schedule for a scenario (user decisions 2026-11-16).

Every agent plays the same number of games, in blocks: after each block all agents have played equally, so a scenario
stopped at a block boundary is still fair. Pairs of agents meet as evenly as possible. Over the scenario every agent
holds each job exactly once (games per agent = 12 = jobs): a proper edge colouring of the agent-game graph with 12
colours (König: a bipartite graph of maximum degree 12 always has one).

  S1: python -B tools/make_schedule.py --pool runs/<s1>/pool.sqlite3 --games 16 --players 12 --blocks 4 --start-seed 201 --out runs/<s1>/schedule.json
  S2: python -B tools/make_schedule.py --pool runs/<s2>/pool.sqlite3 --games 24 --players 8 --blocks 3 --start-seed 301 --out runs/<s2>/schedule.json
"""
import argparse
import itertools
import json
import random
import sqlite3
from collections import Counter
from pathlib import Path

JOBS = 12


def participation(agents, games, players, blocks, rng, rounds=40000):
    n = len(agents)
    per_block = games // blocks
    plays = players * per_block // n
    assert games % blocks == 0 and players * per_block == plays * n, 'blocks must give every agent the same games'
    table = []
    for _ in range(blocks):
        while True:   # each agent exactly `plays` games in this block; each game `players` agents
            need = {a: plays for a in agents}
            block, ok = [], True
            for _ in range(per_block):
                pool = sorted(agents, key=lambda a: (-need[a], rng.random()))[:players]
                if any(need[a] == 0 for a in pool):
                    ok = False
                    break
                for a in pool:
                    need[a] -= 1
                block.append(set(pool))
            if ok and not any(need.values()):
                break
        table.extend(block)

    def pairs():
        c = Counter()
        for g in table:
            c.update(itertools.combinations(sorted(g), 2))
        return c

    def cost(c):
        values = [c.get(p, 0) for p in itertools.combinations(sorted(agents), 2)]
        return max(values) - min(values), sum(v * v for v in values)

    c = pairs()
    best = cost(c)
    for _ in range(rounds):   # swap a player of one game with a sitter of another game in the same block
        b = rng.randrange(blocks)
        g1, g2 = rng.sample(range(b * per_block, (b + 1) * per_block), 2)
        x = rng.choice(sorted(table[g1] - table[g2]))
        y = rng.choice(sorted(table[g2] - table[g1]))
        table[g1] = table[g1] - {x} | {y}
        table[g2] = table[g2] - {y} | {x}
        trial = cost(pairs())
        if trial <= best:
            best = trial
        else:
            table[g1] = table[g1] - {y} | {x}
            table[g2] = table[g2] - {x} | {y}
    return table, pairs()


def colour(table, agents):
    """Kempe-chain edge colouring of the bipartite agent-game graph with JOBS colours."""
    at = {}   # node -> {colour: other node}
    nodes = [('a', a) for a in agents] + [('g', i) for i in range(len(table))]
    for v in nodes:
        at[v] = {}
    for i, g in enumerate(table):
        for a in sorted(g):
            u, v = ('a', a), ('g', i)
            ca = next(c for c in range(JOBS) if c not in at[u])
            cb = next(c for c in range(JOBS) if c not in at[v])
            if ca not in at[v]:
                pick = ca
            else:   # swap ca/cb along the alternating path from v; it never reaches u in a bipartite graph
                path, w, cur = [], v, ca
                while cur in at[w]:
                    nxt = at[w][cur]
                    path.append((w, nxt, cur))
                    w, cur = nxt, (cb if cur == ca else ca)
                for x, y, c in path:
                    del at[x][c], at[y][c]
                for x, y, c in path:
                    other = cb if c == ca else ca
                    at[x][other], at[y][other] = y, x
                pick = ca
            at[u][pick], at[v][pick] = v, u
    return [{a: next(c for c, w in at[('g', i)].items() if w == ('a', a)) for a in table[i]} for i in range(len(table))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pool', type=Path, required=True)
    ap.add_argument('--games', type=int, required=True)
    ap.add_argument('--players', type=int, required=True)
    ap.add_argument('--blocks', type=int, required=True)
    ap.add_argument('--start-seed', type=int, required=True)
    ap.add_argument('--rng', type=int, default=20261116)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    db = sqlite3.connect(f'file:{args.pool}?mode=ro', uri=True)
    rows = [json.loads(s) for (s,) in db.execute('select state from agents')]
    mbti = {r['agent_id']: r.get('fixed_mbti') for r in rows if r.get('pool_role') == 'player'}
    agents = sorted(mbti)
    rng = random.Random(args.rng)
    table, pairs = participation(agents, args.games, args.players, args.blocks, rng)
    jobs = colour(table, agents)
    # checks: equal games per block, every job once per agent, distinct jobs per game
    per_block = args.games // args.blocks
    for b in range(args.blocks):
        counts = Counter(a for g in table[b * per_block:(b + 1) * per_block] for a in g)
        assert len(set(counts.values())) == 1 and len(counts) == len(agents), counts
    held = {a: sorted(g[a] for g in jobs if a in g) for a in agents}
    assert all(len(set(v)) == len(v) for v in held.values())
    assert all(len(set(g.values())) == len(g) for g in jobs)
    values = [pairs.get(p, 0) for p in itertools.combinations(agents, 2)]
    out = {'games': {str(args.start_seed + i): jobs[i] for i in range(args.games)},
           'mbti': mbti,
           'by_mbti': {str(args.start_seed + i): {mbti[a]: j for a, j in sorted(jobs[i].items(), key=lambda x: x[1])}
                       for i in range(args.games)},
           'blocks': [[str(args.start_seed + i) for i in range(b * per_block, (b + 1) * per_block)] for b in range(args.blocks)],
           'stats': {'games_per_agent': len(held[agents[0]]), 'jobs_per_agent': {mbti[a]: len(set(v)) for a, v in held.items()},
                     'pair_meetings': {'min': min(values), 'max': max(values)}},
           'made_by': f'tools/make_schedule.py --games {args.games} --players {args.players} --blocks {args.blocks} --rng {args.rng}'}
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f"{args.games} games x {args.players}: {out['stats']['games_per_agent']} games per agent, "
          f"distinct jobs per agent {sorted(set(out['stats']['jobs_per_agent'].values()))}, pair meetings {min(values)}-{max(values)}")


if __name__ == '__main__':
    main()
