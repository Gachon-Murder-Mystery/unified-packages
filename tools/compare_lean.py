"""[FIX-B58] Compare a default-mode and a lean-mode campaign: tokens saved against what the agents lose.

    PYTHONUTF8=1 python -B tools/compare_lean.py v9-check7-30c v9-check8-30c-lean

Tokens: per call kind (movement / interaction / deduction / answer / trade / talk / reflect / share), mean input,
cached share, output; totals per game. Quota per game from usage-events.log. Behaviour: model stand-ins and
rejected outputs, missing reactions, movement quality (into a room I had noted as exhausted; next to a person who
holds a type I still need), deduction (accusations, last-cycle share, correct, chance x combinations), trades,
talk lines, game results.
"""
import collections
import json
import re
import sqlite3
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def kind_of(system, user):
    if '"must_speak"' in user:
        return 'talk'
    if '"my_score_this_game"' in user:
        return 'share'
    try:
        d = json.loads(user)
    except ValueError:
        return 'other'
    if not isinstance(d, dict):
        return 'other'
    if 'this_game' in d:
        return 'reflect'
    if 'response_options' in d:
        return 'answer'
    if 'incoming_trade' in d:
        return 'trade'
    return d.get('phase') or 'other'


def campaign(name):
    camp = ROOT / 'runs' / name
    b = json.loads((camp / 'batch.json').read_text(encoding='utf-8'))
    db = sqlite3.connect(f'file:{camp / "pool.sqlite3"}?mode=ro', uri=True)
    games = [a for a in b['attempts'] if a['status'] == 'completed']
    K = collections.defaultdict(lambda: collections.Counter())
    behaviour = collections.Counter()
    chance = []
    for a in games:
        gdir = Path(a['full_record_file']).parent
        g = json.loads(db.execute('select state from games where id=?', (a['game_id'],)).fetchone()[0])
        truth = g['truth']
        s = json.loads(Path(a['summary_file']).read_text(encoding='utf-8'))
        behaviour['games'] += 1
        behaviour['winners'] += len(s.get('winners') or [])
        behaviour['deaths'] += len(s.get('deaths') or [])
        behaviour['model_fallbacks'] += s.get('model_fallbacks') or 0
        behaviour['player_fallbacks'] += s.get('player_fallbacks') or 0
        behaviour['say_do'] += sum((s.get('say_do_mismatches') or {}).values())
        behaviour['settled_trades'] += sum(t['status'] == 'settled' for t in g['trades'].values())
        behaviour['proposals'] += len(g['trades'])
        for line in open(gdir / 'detail.jsonl', encoding='utf-8'):
            e = json.loads(line)
            r = e.get('record') or {}
            if e.get('event') == 'agent_journal':
                ev = r.get('event')
                if ev in ('invalid_output', 'invalid_trade_response', 'invalid_talk', 'invalid_share'):
                    behaviour['rejected_outputs'] += 1
                if ev == 'reaction_missing' or ev == 'reaction_error':
                    behaviour['reaction_missing'] += 1
        for line in open(gdir / 'codex-calls.jsonl', encoding='utf-8'):
            x = json.loads(line)
            if x.get('kind'):
                continue
            k = kind_of(x.get('system') or '', x.get('user') or '')
            u = x.get('usage') or {}
            K[k]['calls'] += 1
            K[k]['input'] += u.get('input') or 0
            K[k]['cached'] += u.get('cached_input') or 0
            K[k]['output'] += u.get('output') or 0
            K[k]['user_chars'] += len(x.get('user') or '')
            try:
                d = json.loads(x.get('user') or '')
                resp = json.loads(x.get('response') or '')
            except ValueError:
                continue
            if not isinstance(d, dict) or not isinstance(resp, dict):
                continue
            if d.get('phase') == 'movement':
                act = resp.get('action') or {}
                room = (act.get('payload') or {}).get('room') if act.get('type') == 'move' else (d.get('me') or {}).get('room')
                notes = d.get('room_notes') or {}
                behaviour['moves'] += 1
                behaviour['move_into_exhausted'] += bool(room is not None and (notes.get(str(room)) or {}).get('exhausted'))
                seek = (d.get('situation') or {}).get('seek') or {}
                holders = {row.get('id') for rows in seek.values() for row in rows or []}
                rooms_of = {p.get('id'): p.get('room') for p in d.get('positions') or []}
                behaviour['move_to_holder'] += bool(room is not None and any(rooms_of.get(h) == room for h in holders))
                behaviour['talk_intent'] += resp.get('talk') is True
            if d.get('phase') == 'deduction':
                behaviour['deductions'] += 1
                dm = resp.get('deduction_math') or {}
                if resp.get('decision') == 'accuse':
                    behaviour['accusations'] += 1
                    behaviour['accuse_last'] += d.get('remaining_cycles') == 1
                    hyp = resp.get('current_hypothesis') or {}
                    behaviour['accuse_correct'] += all(hyp.get(c) == truth[c] for c in truth)
                if isinstance(dm.get('my_win_chance'), (int, float)):
                    chance.append(dm['my_win_chance'])
        for (raw,) in db.execute("select state from competitive_events where game_id=? and state like '%talk_said%'", (a['game_id'],)):
            behaviour['talk_lines'] += bool((json.loads(raw).get('payload') or {}).get('text'))
    quota = []
    log = camp / 'usage-events.log'
    if log.exists():
        for line in log.read_text(encoding='utf-8').splitlines():
            m = re.search(r'(?:watch started: quota|game finished: \d+/\d+ done, quota|-> )(\d+)%', line)
            if m:
                quota.append(int(m.group(1)))
    return K, behaviour, chance, quota


def main(full, lean):
    data = {name: campaign(name) for name in (full, lean)}
    print(f"tokens per call kind ({full} | {lean})")
    kinds = sorted(set(data[full][0]) | set(data[lean][0]), key=lambda k: -data[full][0][k]['input'])
    totals = {}
    for name in (full, lean):
        K, b = data[name][0], data[name][1]
        totals[name] = {f: sum(K[k][f] for k in K) / max(1, b['games']) for f in ('input', 'cached', 'output', 'calls')}
    for k in kinds:
        row = []
        for name in (full, lean):
            c = data[name][0][k]
            n = c['calls'] or 1
            row.append(f"{c['calls']:5d} calls, input {c['input'] / n / 1000:5.1f}k (cached {100 * c['cached'] / max(1, c['input']):3.0f}%), "
                       f"prompt {c['user_chars'] / n / 1000:5.1f}k chars, output {c['output'] / n:5.0f}")
        print(f"  {k:12s} {row[0]} | {row[1]}")
    for name in (full, lean):
        t = totals[name]
        print(f"  per game {name}: calls {t['calls']:.0f}, input {t['input'] / 1e6:.1f}M (uncached {(t['input'] - t['cached']) / 1e6:.1f}M), output {t['output'] / 1e6:.2f}M; "
              f"quota log {data[name][3]}")
    print("\nbehaviour (totals over the games)")
    keys = ('games', 'winners', 'deaths', 'model_fallbacks', 'player_fallbacks', 'rejected_outputs', 'say_do', 'reaction_missing',
            'moves', 'move_into_exhausted', 'move_to_holder', 'talk_intent', 'talk_lines', 'proposals', 'settled_trades',
            'deductions', 'accusations', 'accuse_last', 'accuse_correct')
    for k in keys:
        print(f"  {k:20s} {data[full][1][k]:7d} | {data[lean][1][k]:7d}")
    for name in (full, lean):
        ch = data[name][2]
        print(f"  win chance {name}: n {len(ch)}, median {st.median(ch) if ch else None}, mean {round(st.mean(ch), 4) if ch else None}")


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
