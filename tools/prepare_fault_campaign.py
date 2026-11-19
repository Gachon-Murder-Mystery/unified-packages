"""Prepare the fault experiment after the main campaign (user decisions 2026-11-12, engine A57, runner I21).

Copies the main campaign's pool (learning, memories, relationships) into a new campaign directory, resets only the
cumulative scores, and writes the eight-game fault schedule:

  1-2  no fault (baseline in the reset state)
  3    one player cut for good            4  one player cut, back 6 cycles later
  5    the GM cut for good                6  the GM cut, back 6 cycles later
  7    several players: two for good, two coming back
  8    the GM cut again and again until no waiting agent is left (the game ends and does not count)

python -B tools/prepare_fault_campaign.py --main runs/v10-main-30c-lean --out runs/v10-fault-30c-lean --start-seed 101

[FIX-I27] 2026-11-16 (v11, user decision): the agents' operating form changed (B67, B68), so the v11 fault campaign
starts from NEW agents: `--fresh` writes only the schedule; run_batch then bootstraps a new pool (no learning,
memories or relationships).
python -B tools/prepare_fault_campaign.py --fresh --out runs/v11-fault-30c-lean --start-seed 101
then: python -B run_batch.py --codex --parallel --lean --deadline 30 --count 8 --start-seed 101 --daily-pace \\
          --fault-plans runs/v10-fault-30c-lean/fault_plans.json --batch-dir runs/v10-fault-30c-lean
"""
import argparse
import json
import shutil
import sqlite3
from pathlib import Path

PLANS = [
    [],
    [],
    [{'kind': 'player', 'cycle': 8, 'slot': 3}],
    [{'kind': 'player', 'cycle': 8, 'return_cycle': 14, 'slot': 5}],
    [{'kind': 'gm', 'cycle': 8}],
    [{'kind': 'gm', 'cycle': 8, 'return_cycle': 14}],
    [{'kind': 'player', 'cycle': 6, 'slot': 1}, {'kind': 'player', 'cycle': 10, 'return_cycle': 16, 'slot': 4},
     {'kind': 'player', 'cycle': 12, 'return_cycle': 20, 'slot': 8}, {'kind': 'player', 'cycle': 14, 'slot': 6}],
    [{'kind': 'gm', 'cycle': c} for c in (4, 8, 12, 16, 20)],
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--main', type=Path, help='finished main campaign directory (its pool is copied)')
    ap.add_argument('--fresh', action='store_true', help='[FIX-I27] no copy: the campaign bootstraps new agents')
    ap.add_argument('--gm-memory-from', type=Path, default=None,
                    help='[FIX-I30] agent-memory folder whose GM memory (gm_*.json) carries on: the GM is never reset')
    ap.add_argument('--out', type=Path, required=True, help='new fault campaign directory (must not exist)')
    ap.add_argument('--start-seed', type=int, default=101)
    args = ap.parse_args()
    if args.out.exists():
        raise SystemExit(f'{args.out} exists: use a new directory')
    if args.fresh == bool(args.main):
        raise SystemExit('give exactly one of --main or --fresh')
    args.out.mkdir(parents=True)
    plans = {str(args.start_seed + i): plan for i, plan in enumerate(PLANS)}
    if args.fresh:
        if args.gm_memory_from:   # [FIX-I30] user 2026-11-16: only the players start over; the GM keeps learning
            (args.out / 'agent-memory').mkdir()
            for f in sorted(args.gm_memory_from.glob('gm_*.json')):
                shutil.copy2(f, args.out / 'agent-memory' / f.name)
                print('GM memory carried on:', f.name)
        (args.out / 'fault_plans.json').write_text(json.dumps(plans, ensure_ascii=False, indent=1), encoding='utf-8')
        print(f'fresh campaign: no pool copied; fault plans for seeds {min(plans)}-{max(plans)} written')
        return
    shutil.copy2(args.main / 'pool.sqlite3', args.out / 'pool.sqlite3')
    if (args.main / 'agent-memory').exists():
        shutil.copytree(args.main / 'agent-memory', args.out / 'agent-memory')
    db = sqlite3.connect(args.out / 'pool.sqlite3')
    reset = []
    for agent_id, raw in db.execute('select agent_id, state from agents').fetchall():
        a = json.loads(raw)
        if a.get('pool_role') == 'player' or 'cumulative_score' in a:
            reset.append((agent_id, a.get('cumulative_score', 0)))
            a['cumulative_score'] = 0          # scores only: history, learning and relationships stay
            a['score_reset'] = {'from': str(args.main), 'previous_cumulative_score': reset[-1][1]}
            db.execute('update agents set state=? where agent_id=?', (json.dumps(a, ensure_ascii=False), agent_id))
    db.commit()
    db.close()
    (args.out / 'fault_plans.json').write_text(json.dumps(plans, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'pool copied, {len(reset)} cumulative scores reset to 0; fault plans for seeds {min(plans)}-{max(plans)} written')


if __name__ == '__main__':
    main()
