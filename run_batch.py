"""Run games sequentially as one campaign of a persistent agent pool.

[FIX-I1] experiment-30 gave every seed its own game.sqlite3, so the engine bootstrapped a
brand-new pool (new agent ids, empty history) per seed: 30 first games. A campaign now
keeps ONE pool DB and one agent-memory folder, so the same 16 agents play every game and
carry their experience forward.

A campaign is never "finished": run more games into the same directory at any time and
the agents keep learning.

    python run_batch.py --codex --count 10 --batch-dir runs/codex-campaign      # first 10 games
    python run_batch.py --codex --add 5 --batch-dir runs/codex-campaign         # 5 more (11-15)
    python run_batch.py --codex --batch-dir runs/codex-campaign                 # resume after a stop

Each game records its own conditions (backend, model, deadline, time limit, unsolved
score), so later games may use different settings; only the pool mode is fixed.
To stop gracefully after the current game, create the file <batch-dir>/STOP.
In a shared pool a failed game stops the campaign (fix the cause, then resume) so that
broken games do not pile up in the agents' experience.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKENDS = ('scripted', 'api', 'fake', 'codex')
FLAG = {'api': '--llm', 'fake': '--fake-llm', 'codex': '--codex'}


def save(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    # [FIX-I9] On Windows a reader (OneDrive sync, the quota watcher) holding batch.json open makes the
    # atomic replace fail with "access denied"; that stopped runs/v8-main-30c after game 1. Retry briefly.
    for attempt in range(30):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 29:
                raise
            time.sleep(1)


def load_state(manifest, args):
    """Read or create the manifest. Old (experiment-30) manifests had one fixed config and
    a per-seed DB; they are treated as isolated campaigns."""
    if not manifest.exists():
        return {'config': {'start_seed': args.start_seed, 'isolated': args.isolated},
                'target_count': 0, 'attempts': []}
    state = json.loads(manifest.read_text(encoding='utf-8'))
    config = state['config']
    if 'target_count' not in state:  # legacy manifest
        state['target_count'] = config.get('count', 0)
        state['config'] = config = {'start_seed': config.get('start_seed', 1),
                                    'isolated': config.get('isolated', True)}
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--start-seed', type=int, default=1, help='first seed of a NEW campaign')
    parser.add_argument('--count', type=int, help='total number of games the campaign should reach')
    parser.add_argument('--add', type=int, help='extend the campaign by N games')
    backend = parser.add_mutually_exclusive_group()
    backend.add_argument('--llm', action='store_true', help='API model through the engine gateway')
    backend.add_argument('--fake-llm', action='store_true', help='B path with a free rule-based stand-in model')
    backend.add_argument('--codex', action='store_true', help='local Codex CLI as the model (no API credit)')
    parser.add_argument('--codex-model', default='gpt-6.1-terra', help='model Codex runs (v9 default gpt-6.1-terra, user decision 2026-11-09; v8 ran gpt-6-luna)')
    parser.add_argument('--codex-reasoning', default='medium', help='Codex model_reasoning_effort (reasoning cap)')
    parser.add_argument('--deadline', type=int, default=120, help='game cycle limit: games end by cycles')
    # [FIX-I7] Games end by cycles only. A wall-clock limit is still available (seconds), but
    # off by default: per-call error caps (300 s) handle stuck model calls instead.
    parser.add_argument('--time-limit', type=int, default=0, help='per-game wall-clock limit in seconds; 0 = off')
    parser.add_argument('--parallel', action='store_true', help='step agents concurrently (LLM calls overlap)')
    parser.add_argument('--question-timeout', type=int, default=None,
                        help='answer/trade error cap in seconds (default 30; 600 with --parallel, FIX-I8/A17)')
    parser.add_argument('--interaction-order', choices=('immediate', 'simultaneous'), default='simultaneous',
                        help='engine rule interaction_order (FIX-A17; immediate = v1.3)')
    parser.add_argument('--clue-flow', choices=('v13', 'boosted'), default='boosted',
                        help='engine rule clue_flow (FIX-A19; v13 = original)')
    parser.add_argument('--unsolved-score', type=int, default=-10,
                        help='score of surviving non-winners when unsolved (v1.3: 0; FIX-A24 default -10)')
    parser.add_argument('--batch-dir', type=Path, help='campaign directory; reuse it to resume or extend')
    # [FIX-I14] user decision 2026-11-10: run up to the weekly Codex quota, then continue after the reset
    parser.add_argument('--quota-reserve', type=float, default=None,
                        help='codex: before each game, stop if less than this many percent of the weekly quota is left '
                             '(set it a little above one game\'s cost); rerun the same command after the reset')
    parser.add_argument('--wait-for-reset', action='store_true',
                        help='[FIX-I16] with --quota-reserve: instead of stopping, wait until the quota resets and go on')
    parser.add_argument('--lean', action='store_true',
                        help='[FIX-B58/I15] lean mode: short Codex instructions and lean movement/deduction prompts')
    parser.add_argument('--outage-wait-hours', type=float, default=None,
                        help='codex: how long a game hit by a model outage (e.g. the quota running out mid-game) waits '
                             'paused before it is aborted (default 3 h); long enough to reach the reset keeps the game')
    # [FIX-I20] user decision 2026-11-12: pace the campaign by the Codex quota. Each day may use at most
    # (quota left at the day's start) / (days left until the reset); a game starts only if today's use plus the
    # largest per-game use seen so far stays inside that budget. Otherwise the campaign waits between games.
    parser.add_argument('--daily-pace', action='store_true',
                        help='Codex: spend at most (left %% / days to reset) per day; wait between games otherwise')
    # [FIX-I23] the day's budget belongs to the account, not the campaign: continue an earlier campaign's day
    parser.add_argument('--pace-from', type=Path, default=None,
                        help='batch directory whose pacing day (start, use at start, budget) this campaign continues')
    parser.add_argument('--game-cost', type=float, default=4.5,
                        help='quota %% one game is assumed to use until games of this campaign are measured')
    # [FIX-I21] the fault experiment: {"<seed>": [fault items, engine A57]} or {"default": [...]}
    parser.add_argument('--fault-plans', type=Path, default=None, help='JSON file mapping seeds to fault plans')
    parser.add_argument('--profile', choices=('v10', 'v9'), default='v10', help='rule profile passed to run_scenario (FIX-I19)')
    parser.add_argument('--isolated', action='store_true',
                        help='old behaviour: fresh pool per seed (no cross-game experience)')
    args = parser.parse_args()
    if not 1 <= args.deadline <= 1000 or args.time_limit < 0:
        parser.error('time-limit must be >= 0 (0 = off); deadline must be 1..1000')
    directory = (args.batch_dir or ROOT / 'runs' / ('batch-' + time.strftime('%Y%m%d-%H%M%S'))).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / 'batch.json'
    state = load_state(manifest, args)
    if manifest.exists() and args.isolated != state['config']['isolated']:
        parser.error('pool mode (--isolated) cannot change within a campaign; use a new --batch-dir')
    if args.add is not None:
        state['target_count'] += args.add
    elif args.count is not None:
        if args.count < state['target_count']:
            parser.error(f"campaign already targets {state['target_count']} games; use --add to extend")
        state['target_count'] = args.count
    elif not state['target_count']:
        parser.error('a new campaign needs --count (or --add)')
    kind = 'api' if args.llm else 'fake' if args.fake_llm else 'codex' if args.codex else 'scripted'
    if kind == 'api':
        from run_game import load_env
        load_env()
        if not os.environ.get('GAME_API_KEY') or not os.environ.get('GAME_MODEL'):
            parser.error('--llm requires GAME_API_KEY and GAME_MODEL in .env or environment')
    pool_db = directory / 'pool.sqlite3'  # [FIX-I1] one persistent pool for the campaign
    memory = directory / 'agent-memory'   # [FIX-I2] B's per-agent lessons live beside it
    state['pool_db'] = None if state['config']['isolated'] else str(pool_db)
    state['memory_dir'] = str(memory)
    save(manifest, state)
    stop_file = directory / 'STOP'
    stop_file.unlink(missing_ok=True)
    print(f"Campaign: {directory} (target {state['target_count']} games)", flush=True)
    # [FIX-C16] GM incidents, model outages and failed games are collected here as regression traces
    child_env = dict(os.environ, GAME_MEMORY_DIR=str(memory), GAME_TRACE_FILE=str(directory / 'regression_traces.jsonl'))
    model = None
    if kind == 'codex':
        child_env.update(CODEX_MODEL=args.codex_model, CODEX_REASONING=args.codex_reasoning)
        if args.lean:   # [FIX-B58/I15]
            child_env.update(GAME_LEAN_PROMPTS='1', CODEX_INSTRUCTIONS_FILE=str(ROOT / 'codex_lean_instructions.md'))
        if args.outage_wait_hours:   # [FIX-I14] a game paused by the quota can wait for the reset
            child_env['CODEX_OUTAGE_MAX_SECONDS'] = str(int(args.outage_wait_hours * 3600))
        model = f'{args.codex_model} via Codex CLI (reasoning {args.codex_reasoning})'
    elif kind == 'api':
        model = os.environ.get('GAME_MODEL')
    # [FIX-I8] With --parallel an answerer is often inside a 10-60 s decision call when a
    # question arrives; at 30 s, 8% of successful questions in arm-20c were settled by the
    # engine's default instead of the agent (received up to 41 s late, answered up to 60 s).
    if args.question_timeout is None and args.parallel:
        # [FIX-A17] phases now wait for real answers; the cap only catches obvious errors
        args.question_timeout = 600
    conditions = {'backend': kind, 'model': model, 'deadline': args.deadline,
                  'time_limit': args.time_limit or None, 'unsolved_score': args.unsolved_score,
                  'parallel': args.parallel, 'question_timeout': args.question_timeout or 30,
                  'trade_timeout': args.question_timeout or 30, 'interaction_order': args.interaction_order,
                  'clue_flow': args.clue_flow, 'lean': bool(args.lean),   # [FIX-B58]
                  'profile': args.profile, 'daily_pace': bool(args.daily_pace)}   # [FIX-I19/I20]
    fault_plans = json.loads(args.fault_plans.read_text(encoding='utf-8')) if args.fault_plans else {}   # [FIX-I21]
    start = state['config']['start_seed']
    for seed in range(start, start + state['target_count']):
        if any(a['seed'] == seed and a['status'] == 'completed' for a in state['attempts']):
            continue
        if stop_file.exists():
            print('STOP file found: stopping before the next game', flush=True)
            break
        if kind == 'codex' and args.daily_pace:   # [FIX-I20]
            if not pace(state, manifest, args, stop_file, seed):
                break
        if kind == 'codex' and args.quota_reserve is not None:   # [FIX-I14]
            waiting = None
            while True:
                try:
                    from codex_usage import quota
                    q = quota()
                    left = 100 - float(q.get('used_percent'))
                except Exception as exc:   # cannot tell: play on; the outage pause is the safety net
                    print(f'quota check failed ({type(exc).__name__}: {exc}); starting the next game anyway', flush=True)
                    left = None
                    break
                if left >= args.quota_reserve or stop_file.exists():
                    break
                if not args.wait_for_reset:
                    break
                if waiting is None:   # [FIX-I16] wait for the reset, re-checking every 10 minutes
                    waiting = time.time()
                    print(f"quota reserve reached: {left:.0f}% left < {args.quota_reserve:g}% (resets {q.get('resets_at')}); "
                          f"waiting for the reset before seed {seed}", flush=True)
                time.sleep(600)
            if stop_file.exists():
                print('STOP file found: stopping before the next game', flush=True)
                break
            if left is not None and left < args.quota_reserve:
                print(f"quota reserve reached: {left:.0f}% left < {args.quota_reserve:g}% "
                      f"(resets {q.get('resets_at')}); stopping before the next game. "
                      f"Rerun the same command after the reset to continue.", flush=True)
                break
            if waiting is not None:
                print(f'quota reset seen after {round((time.time() - waiting) / 60)} min', flush=True)
            if left is not None:
                print(f'quota: {left:.0f}% left, starting seed {seed}', flush=True)
        attempt_dir = directory / f'seed-{seed}' / f"attempt-{1 + sum(a['seed'] == seed for a in state['attempts'])}"
        attempt_dir.mkdir(parents=True)
        plan = fault_plans.get(str(seed), fault_plans.get('default'))   # [FIX-I21]
        row = {'seed': seed, 'status': 'running', 'directory': str(attempt_dir), 'started_at': time.time(),
               'conditions': dict(conditions, **({'fault_plan': plan} if plan else {}))}
        if kind == 'codex' and args.daily_pace:
            row['quota_used_at_start'] = state.get('pacing', {}).get('last_used')
        state['attempts'].append(row)
        save(manifest, state)
        data = attempt_dir / 'game.sqlite3' if state['config']['isolated'] else pool_db
        command = [sys.executable, '-u', str(ROOT / 'run_game.py'), '--seed', str(seed),
                   '--deadline', str(args.deadline), '--time-limit', str(args.time_limit),
                   '--unsolved-score', str(args.unsolved_score),
                   '--data', str(data), '--out', 'summary.json', '--progress']
        if kind in FLAG:
            command.append(FLAG[kind])
        if args.parallel:
            command.append('--parallel')
        if args.question_timeout:
            command += ['--question-timeout', str(args.question_timeout)]
        command += ['--interaction-order', args.interaction_order, '--clue-flow', args.clue_flow,
                    '--profile', args.profile]   # [FIX-I19]
        if plan:
            command += ['--fault-plan', json.dumps(plan)]   # [FIX-I21]
        print(f'seed {seed}: starting', flush=True)
        interrupted = False
        # POSIX: own process group so Ctrl+C can be forwarded. Windows: the child shares the
        # console and receives Ctrl+C itself (os.killpg does not exist there).
        group = {'start_new_session': True} if os.name != 'nt' else {}
        with (attempt_dir / 'stdout.json').open('w', encoding='utf-8') as stdout, (attempt_dir / 'stderr.log').open('w', encoding='utf-8') as stderr:
            proc = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr, env=child_env, **group)
            try:
                code = proc.wait()
            except KeyboardInterrupt:
                interrupted = True
                if os.name != 'nt':
                    os.killpg(proc.pid, signal.SIGINT)
                try:
                    code = proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGTERM) if os.name != 'nt' else proc.terminate()
                    code = proc.wait(timeout=10)
        row.update(status='interrupted' if interrupted else 'failed', returncode=code, finished_at=time.time())
        if code == 0 and not interrupted:
            try:
                result = json.loads((attempt_dir / 'stdout.json').read_text(encoding='utf-8'))
                row.update(status='completed', game_id=result['game_id'], resolution=result['resolution'],
                           cycles=result['cycles'], winners=result['winners'],
                           model_calls=result.get('model_calls'), model_fallbacks=result.get('model_fallbacks'),
                           full_record_file=result['full_record_file'], summary_file=result['summary_file'])
            except (ValueError, KeyError):
                row['error'] = 'child exited successfully but result JSON was missing or invalid'
        if kind == 'codex' and args.daily_pace and row['status'] == 'completed':   # [FIX-I20] measure this game
            got = used_percent()
            used = got[0] if got else None
            if used is not None and row.get('quota_used_at_start') is not None and used >= row['quota_used_at_start']:
                row['quota_used'] = round(used - row['quota_used_at_start'], 2)
            if used is not None:
                state.setdefault('pacing', {})['last_used'] = used
        save(manifest, state)
        print(f"seed {seed}: {row['status']} ({row.get('resolution', 'see stderr.log')})"
              + (f", quota used {row['quota_used']}%" if row.get('quota_used') is not None else ''), flush=True)
        if interrupted:
            return 130
        if row['status'] != 'completed':   # [FIX-C16] a failed game is a regression trace
            tail = (attempt_dir / 'stderr.log').read_text(encoding='utf-8', errors='replace').splitlines()[-40:]
            with (directory / 'regression_traces.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'source': 'runner', 'type': 'game_failed', 'seed': seed,
                                         'status': row['status'], 'returncode': code,
                                         'attempt': str(attempt_dir), 'stderr_tail': tail,
                                         'at': round(time.time(), 1)}, ensure_ascii=False) + '\n')
        if row['status'] != 'completed' and not state['config']['isolated']:
            print(f'seed {seed} failed in a shared pool: campaign paused. Fix the cause '
                  f'(see {attempt_dir / "stderr.log"}) and rerun the same command to resume.', flush=True)
            return 1
    done = sum(a['status'] == 'completed' for a in state['attempts'])
    print(f"{done}/{state['target_count']} games completed. Manifest: {manifest}", flush=True)
    return 0 if done >= state['target_count'] else 1


def used_percent():
    try:
        from codex_usage import quota
        q = quota()
        return float(q['used_percent']), q
    except Exception as exc:   # pragma: no cover - reported by the caller
        print(f'quota check failed ({type(exc).__name__}: {exc})', flush=True)
        return None


def pace(state, manifest, args, stop_file, seed):
    """[FIX-I20] Wait until today's budget allows one more game. Days run from the campaign's first check
    (24 h each); the budget is fixed at each day's start: quota left / days left until the reset."""
    import datetime as dt
    while True:
        if stop_file.exists():
            print('STOP file found: stopping before the next game', flush=True)
            return False
        got = used_percent()
        if got is None:   # cannot tell: wait and ask again rather than overspend
            time.sleep(600)
            continue
        used, q = got
        now = time.time()
        if 'pacing' not in state and args.pace_from:   # [FIX-I23]
            old = json.loads((args.pace_from / 'batch.json').read_text(encoding='utf-8')).get('pacing') or {}
            state['pacing'] = {k: old[k] for k in ('reset_at', 'day_start', 'day_used_start', 'day_budget') if k in old}
            print(f"daily pace: continuing the day of {args.pace_from} (started "
                  f"{time.strftime('%m-%d %H:%M', time.localtime(old.get('day_start', now)))}, budget {old.get('day_budget')}%)", flush=True)
        p = state.setdefault('pacing', {})
        reset = dt.datetime.strptime(q['resets_at'], '%Y-%m-%d %H:%M').replace(
            tzinfo=dt.timezone(dt.timedelta(hours=9))).timestamp() if q.get('resets_at') else now + 7 * 86400
        if p.get('reset_at') != reset or used < p.get('day_used_start', used):   # first check, or the quota was reset
            p.update(reset_at=reset, day_start=now, day_used_start=used, day_budget=None)
        if now >= p['day_start'] + 86400:   # a new day
            p.update(day_start=p['day_start'] + 86400 * int((now - p['day_start']) // 86400), day_used_start=used, day_budget=None)
        if p.get('day_budget') is None:
            days_left = max((reset - p['day_start']) / 86400, 1.0)
            p['day_budget'] = round((100 - used) / days_left, 2)
            print(f"daily pace: {100 - used:.0f}% left, {days_left:.1f} days to reset -> {p['day_budget']}% for the day "
                  f"starting {time.strftime('%m-%d %H:%M', time.localtime(p['day_start']))}", flush=True)
        costs = [a['quota_used'] for a in state['attempts'] if a.get('quota_used') is not None]
        cost = max(costs) if costs else args.game_cost
        spent = used - p['day_used_start']
        p['last_used'] = used
        save(manifest, state)
        if spent + cost <= p['day_budget']:
            print(f'daily pace: {spent:.1f}% used today of {p["day_budget"]}%, next game ~{cost:.1f}%: starting seed {seed}', flush=True)
            return True
        wake = p['day_start'] + 86400
        print(f"daily pace: {spent:.1f}% used today of {p['day_budget']}%, next game ~{cost:.1f}% would exceed it; "
              f"pausing until {time.strftime('%m-%d %H:%M', time.localtime(wake))}", flush=True)
        while time.time() < wake and not stop_file.exists():
            time.sleep(min(600, max(1, wake - time.time())))


if __name__ == '__main__':
    raise SystemExit(main())
