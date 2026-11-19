"""Mission quantity calibration from recorded games (read-only).

For every counted behaviour a mission can ask for, rebuild each agent's per-cycle count from the final game
states of the gpt-6.1-terra 30-cycle campaigns, then for each candidate quantity N report how many agent-games
reached N by the deadline and the median cycle when they did. Used to set v10 mission quantities so a mission
is typically completed around cycle 25 of 30 (user decision 2026-11-12) with a moderate achievement rate.

These are baseline rates: most of the recorded agents did not hold the mission, so a holder that works for it
will usually be faster. Output: runs/mission_calibration.json and a printed table.
"""
import collections
import json
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAMPS = [ROOT / 'unified-packages-v9' / 'runs' / 'v9-main-30c', ROOT / 'unified-packages-v9' / 'runs' / 'v9-main2-30c-lean']
OUT = Path(__file__).resolve().parents[1] / 'runs' / 'mission_calibration.json'


def games():
    for camp in CAMPS:
        db = sqlite3.connect(f'file:{camp / "pool.sqlite3"}?mode=ro', uri=True)
        by_seed = {}
        for a in json.loads((camp / 'batch.json').read_text(encoding='utf-8'))['attempts']:
            if a['status'] == 'completed':
                by_seed[a['seed']] = a
        for a in by_seed.values():
            r = db.execute('select state from games where id=?', (a['game_id'],)).fetchone()
            if r:
                yield json.loads(r[0])


def events(g, aid):
    """{metric: sorted list of cycles, one entry per counted event} for one agent."""
    p = g['players'][aid]
    cid = p['character_id']
    me = str(cid)
    acts = [x for x in g['activity'] if x['actor'] == cid]
    hist = [x for x in g['cycle_history'] if me in x['alive']]
    ev = collections.defaultdict(list)

    def room_of(x): return x['rooms'].get(me)
    for x in hist:
        n_here = sum(r == room_of(x) for o, r in x['rooms'].items() if o in x['alive'])
        ev[f'time_in_room_{room_of(x)}'].append(x['cycle'])
        if n_here >= 4:
            ev['cycles_in_crowd4'].append(x['cycle'])
        if n_here == 1:
            ev['cycles_alone'].append(x['cycle'])
    seen = set()
    for x in hist:
        r = room_of(x)
        if r not in seen:
            seen.add(r)
            ev['distinct_rooms_visited'].append(x['cycle'])
    visits = collections.Counter()
    for x in hist:
        visits[room_of(x)] += 1
        if visits[room_of(x)] == 2:
            ev['rooms_visited_twice'].append(x['cycle'])
    crowd = {x['cycle'] for x in hist if sum(r == room_of(x) for o, r in x['rooms'].items() if o in x['alive']) >= 4}
    alone = {x['cycle'] for x in hist if sum(r == room_of(x) for o, r in x['rooms'].items() if o in x['alive']) == 1}
    q_targets = collections.Counter()
    q_success_targets = set()
    ex_rooms = set()
    for x in acts:
        k, c = x['kind'], x['cycle']
        if k in ('question', 'explore'):
            ev['interactions'].append(c)
            ev[f'interactions_in_room_{x["room"]}'].append(c)
            if c in crowd:
                ev['interactions_in_crowd4'].append(c)
            if c in alone:
                ev['interactions_alone'].append(c)
        if k == 'explore':
            ev['explores'].append(c)
            if x['successes']:
                ev['explore_successes'].append(c)
                ev[f'explore_successes_room_{x["room"]}'].append(c)
                if x['room'] not in ex_rooms:
                    ex_rooms.add(x['room'])
                    ev['explore_success_distinct_rooms'].append(c)
        if k == 'question':
            ev['questions'].append(c)
            q_targets[x['target']] += 1
            ev['questions_same_target_max'] = ev.get('questions_same_target_max', [])
            if q_targets[x['target']] > len(ev['questions_same_target_max']):
                ev['questions_same_target_max'].append(c)
            if x['successes']:
                ev[f'success_{x["approach"]}'].append(c)
                if x['target'] not in q_success_targets:
                    q_success_targets.add(x['target'])
                    ev['question_success_distinct_targets'].append(c)
            if x['approach'] == 'question' and x['successes'] >= 2:
                ev['question_2plus'].append(c)
        if k == 'prepare':
            ev['prepares'].append(c)
        if x.get('prepared') and k in ('question', 'explore'):
            ev['prepared_rolls'].append(c)
    for x in g['activity']:
        if x.get('target') == cid and x['kind'] == 'question':
            ev['questions_received'].append(x['cycle'])
    partners, first_from_other = set(), []
    for t in sorted(g['trades'].values(), key=lambda t: t.get('cycle') or 0):
        if aid not in (t['proposer'], t['recipient']) or not t.get('accepted'):
            continue
        other = t['recipient'] if t['proposer'] == aid else t['proposer']
        c = t.get('cycle') or 0
        if other not in partners:
            partners.add(other)
            ev['trade_distinct_partners'].append(c)
        if t['status'] != 'settled':
            continue
        f = t.get('fulfilled') or {}
        ev['trades_settled'].append(c)
        if all(f.values()):
            ev['trades_both_kept'].append(c)
        if f.get(aid):
            ev['trades_i_kept'].append(c)
        if f.get(other):
            for _ in t.get('offers', {}).get(other, []):
                ev['clues_received_by_trade'].append(c)
        if t.get('first_sender') not in (None, aid):
            ev['trades_partner_sent_first'].append(c)
        if f.get(other) and not f.get(aid):
            ev['trades_i_broke_they_kept'].append(c)
    start = set(p.get('start_clues', []))
    info = g['characters'][me]['information']
    got_at = {}
    for x in p.get('acquired', []):
        got_at.setdefault(x['id'], x['cycle'])
    for x in g['disclosures']:
        if x.get('recipient') == aid and x.get('clue_id'):
            got_at.setdefault(x['clue_id'], x['cycle'])
    for t in g['trades'].values():
        if t['status'] == 'settled' and aid in (t['proposer'], t['recipient']):
            other = t['recipient'] if t['proposer'] == aid else t['proposer']
            for cl in t.get('offers', {}).get(other, []):
                got_at.setdefault(cl, t.get('cycle') or 0)
    for cl, item in info.items():
        if cl in start or cl not in got_at:
            continue
        if item.get('kind') == 'definite':
            ev['definite_acquired'].append(got_at[cl])
        elif item.get('kind') in ('indirect', 'narrated'):
            ev['indirect_acquired'].append(got_at[cl])
    for x in g['disclosures']:
        if x.get('sender') == aid and x.get('kind') == 'response_definite':
            ev['definite_given_in_answers'].append(x['cycle'])
    return {k: sorted(v) for k, v in ev.items()}


def main():
    rows = []
    for g in games():
        deadline = len(g['cycle_history'])
        for aid in g['players']:
            rows.append((deadline, events(g, aid)))
    metrics = sorted({k for _, e in rows for k in e})
    out = {'agent_games': len(rows), 'metrics': {}}
    print('agent-games', len(rows))
    for m in metrics:
        table = []
        for n in range(1, 41):
            reached = [e[m][n - 1] for _, e in rows if len(e.get(m, [])) >= n]
            if not reached:
                break
            rate = len(reached) / len(rows)
            table.append({'N': n, 'rate': round(rate, 3), 'median_cycle': statistics.median(reached),
                          'p25_cycle': sorted(reached)[len(reached) // 4]})
        out['metrics'][m] = table
        pick = [t for t in table if 0.2 <= t['rate'] <= 0.55 and t['median_cycle'] >= 22]
        shown = pick[:3] if pick else table[-1:]
        print(f"{m:34}", ' | '.join(f"N={t['N']} {t['rate']:.0%} med{t['median_cycle']}" for t in shown))
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')


if __name__ == '__main__':
    main()
