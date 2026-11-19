"""[FIX-I28] Prepare the next scenario of the learning curriculum (user decisions 2026-11-16).

The same agents keep learning: the previous campaign's pool (learning, memories, relationships) is copied, only the
cumulative scores are reset (each scenario announces its own ranking), and the assignment schedule is written.

  S1: python -B tools/prepare_scenario.py --from runs/v11-fault-30c-lean --out runs/v11-s1-30c-lean --games 16 --players 12 --blocks 4 --start-seed 201
  S2: python -B tools/prepare_scenario.py --from runs/v11-s1-30c-lean --out runs/v11-s2-30c-lean --games 24 --players 8 --blocks 3 --start-seed 301
then: python -B run_batch.py --codex --parallel --lean --deadline 30 --daily-pace ... --count <games> --start-seed <seed> \\
          --assignments <out>/schedule.json --batch-dir <out>
"""
import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='source', type=Path, required=True, help='finished campaign directory')
    ap.add_argument('--out', type=Path, required=True, help='new scenario directory (must not exist)')
    ap.add_argument('--games', type=int, required=True)
    ap.add_argument('--players', type=int, required=True)
    ap.add_argument('--blocks', type=int, required=True)
    ap.add_argument('--start-seed', type=int, required=True)
    args = ap.parse_args()
    if args.out.exists():
        raise SystemExit(f'{args.out} exists: use a new directory')
    batch = json.loads((args.source / 'batch.json').read_text(encoding='utf-8'))
    running = [a['seed'] for a in batch['attempts'] if a['status'] == 'running']
    if running:
        raise SystemExit(f'{args.source} still has running attempts {running}: finish or roll them back first')
    args.out.mkdir(parents=True)
    shutil.copy2(args.source / 'pool.sqlite3', args.out / 'pool.sqlite3')
    if (args.source / 'agent-memory').exists():
        shutil.copytree(args.source / 'agent-memory', args.out / 'agent-memory')
    db = sqlite3.connect(args.out / 'pool.sqlite3')
    reset = []
    for agent_id, raw in db.execute('select agent_id, state from agents').fetchall():
        a = json.loads(raw)
        if a.get('pool_role') == 'player' or 'cumulative_score' in a:
            reset.append((agent_id, a.get('fixed_mbti'), a.get('cumulative_score', 0)))
            # [FIX-A64] the ranking is the average of counted games (A61): the games counted until now are the
            # offset, so this scenario's games-played and average start from zero as its score does
            counted = (sum(1 for h in a.get('history', []) if h.get('resolution') not in ('operator_stopped', 'gm_exhausted'))
                       + len(a.get('gm_stand_in', [])))
            prev_games = counted - a.get('scenario_games_offset', 0)
            reset[-1] = reset[-1] + (prev_games,)
            a['cumulative_score'] = 0          # scores only: history, learning and relationships stay
            a['scenario_games_offset'] = counted
            a['score_reset'] = {'from': str(args.source), 'previous_cumulative_score': reset[-1][2],
                                'previous_games': prev_games}
            db.execute('update agents set state=? where agent_id=?', (json.dumps(a, ensure_ascii=False), agent_id))
    db.commit()
    db.close()
    rows = [{'agent_id': a, 'mbti': m, 'score': s, 'games': n, 'average': round(s / n, 2) if n else 0.0}
            for a, m, s, n in reset]
    (args.out / 'previous_ranking.json').write_text(json.dumps(   # [FIX-A64] ranked as the engine does: average first
        sorted(rows, key=lambda r: (-r['average'], -r['score'], r['agent_id'])), ensure_ascii=False, indent=1), encoding='utf-8')
    subprocess.run([sys.executable, '-B', str(ROOT / 'tools' / 'make_schedule.py'), '--pool', str(args.out / 'pool.sqlite3'),
                    '--games', str(args.games), '--players', str(args.players), '--blocks', str(args.blocks),
                    '--start-seed', str(args.start_seed), '--out', str(args.out / 'schedule.json')], check=True)
    print(f'pool copied from {args.source}, {len(reset)} cumulative scores reset to 0 (previous ranking kept)')


if __name__ == '__main__':
    main()
