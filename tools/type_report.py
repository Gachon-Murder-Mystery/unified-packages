"""What each personality type actually did in a v9 campaign, against what its tendency text says.

    PYTHONUTF8=1 python -B tools/type_report.py . <campaign>

Per axis (E/I, S/N, T/F, J/P) and per 16 types. Sources: codex-calls.jsonl (the prompt each agent saw
and its answer), detail.jsonl journals (reactions), the pool DB (trades, results), agent memory files
(affinity, inferred attitudes). Axis metrics compare the first letter with the second (first - second).
"""
import collections
import json
import os
import re
import sqlite3
import statistics as st
import sys
from pathlib import Path

CASE_WORDS = ("범인", "흉기", "무기", "장소", "용의자", "후보", "단서", "조합", "추리", "자정", "사건")


def journal_combinations(gdir, g):
    """[FIX-B52] the agents no longer see a combination count; their journal keeps it for research:
    (character_id, cycle, phase) -> combinations_left at that decision."""
    char = {aid: p['character_id'] for aid, p in g['players'].items()}
    out = {}
    path = gdir / 'detail.jsonl'
    if not path.exists():
        return out
    for line in open(path, encoding='utf-8'):
        e = json.loads(line)
        r = e.get('record') or {}
        if e.get('event') == 'agent_journal' and r.get('event') == 'decision' and r.get('combinations_left') is not None:
            out[(char.get(e.get('agent_id')), r.get('cycle'), r.get('phase'))] = r['combinations_left']
    return out


def mean(xs):
    return round(st.mean(xs), 3) if xs else None


def rate(a, b):
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else "n/a"


def main(root, name):
    root = Path(root); camp = root / 'runs' / name
    b = json.loads((camp / 'batch.json').read_text(encoding='utf-8'))
    db = sqlite3.connect(f'file:{camp / "pool.sqlite3"}?mode=ro', uri=True)
    T = collections.defaultdict(collections.Counter)      # type -> counters
    L = collections.defaultdict(lambda: collections.defaultdict(list))   # type -> lists
    agent_type = {}
    for a in b['attempts']:
        if a['status'] != 'completed':
            continue
        g = json.loads(db.execute('select state from games where id=?', (a['game_id'],)).fetchone()[0])
        mbti = {p['agent_id']: g['characters'][str(p['character_id'])]['mbti'] for p in g['players'].values()}
        agent_type.update(mbti)
        s = json.loads(Path(a['summary_file']).read_text(encoding='utf-8'))
        winners = {w['agent_id'] for w in s.get('winners') or []}
        fb = g.get('final_feedback') or {}
        for aid, m in mbti.items():
            T[m]['agent_games'] += 1
            T[m]['wins'] += aid in winners
            T[m]['deaths'] += aid in (s.get('deaths') or [])
            if aid in fb:
                L[m]['score'].append(fb[aid].get('final_score') or 0)
        for t in g['trades'].values():
            if t['status'] == 'settled':
                for giver, withheld in (t.get('withheld') or {}).items():
                    m = mbti.get(giver)
                    if m:
                        T[m]['settled_as_giver'] += 1
                        T[m]['deceptions'] += bool(withheld)
        gdir = Path(a['full_record_file']).parent
        jc = journal_combinations(gdir, g)   # [FIX-B52]
        # [FIX-B54/B55] talk rooms and post-game shares from the engine's research events
        rooms = {}
        events = [json.loads(raw) for (raw,) in db.execute(
            "select state from competitive_events where game_id=? and (state like '%talk_opened%' or state like '%talk_said%' "
            "or state like '%score_shared%') order by sequence", (a['game_id'],))]
        cycle_now = None
        for ev in events:
            pl = ev.get('payload') or {}
            if ev.get('type') == 'talk_opened':
                cycle_now = pl.get('cycle')
                for room, members in (pl.get('rooms') or {}).items():
                    rooms[(cycle_now, int(room))] = len(members)
            elif ev.get('type') == 'talk_said':
                m = pl.get('mbti')
                if not m:
                    continue
                T[m]['talk_lines'] += bool(pl.get('text'))
                T[m]['talk_leave'] += bool(pl.get('leave'))
                T[m]['talk_case'] += any(w in (pl.get('text') or '') for w in CASE_WORDS)
                if pl.get('text'):
                    L[m]['talk_len'].append(len(pl['text']))
                L[m]['talk_said'].append(dict(pl, room_size=rooms.get((cycle_now, pl.get('room')))))
            elif ev.get('type') == 'score_shared':
                m = mbti.get(pl.get('agent_id'))
                if m:
                    T[m]['shared_games'] += 1
                    T[m]['shared_points'] += pl.get('total') or 0
                    T[m]['shared_to'] += len(pl.get('shares') or [])
        for line in open(gdir / 'codex-calls.jsonl', encoding='utf-8'):
            x = json.loads(line)
            try:
                u = json.loads(x['user']); r = json.loads(x['response'])
            except Exception:
                continue
            if not isinstance(u, dict) or not isinstance(r, dict):
                continue
            m = (u.get('me') or {}).get('mbti')
            if not m:
                continue
            ph = u.get('phase')
            if ph == 'interaction':
                act = r.get('action') or {}
                kind = act.get('type')
                pay = act.get('payload') or {}
                others = len((u.get('now') or {}).get('people_here') or [])
                crowd = 'crowd' if others >= 3 else 'few' if others >= 1 else 'alone'
                T[m]['inter'] += 1
                T[m]['act:' + str(kind)] += 1
                T[m][f'{crowd}:n'] += 1
                if kind in ('question', 'trade'):
                    T[m][f'{crowd}:social'] += 1
                if kind == 'question':
                    T[m]['ap:' + str(pay.get('approach'))] += 1
                msg = pay.get('message') or ''
                if msg:
                    T[m]['msgs'] += 1
                    L[m]['msg_len'].append(len(msg))
                    T[m]['case_talk'] += any(w in msg for w in CASE_WORDS)
            # [check6 fix] new dealings are shown (and reactions given) in whichever phase comes next,
            # in practice the deduction phase; this used to be read in the interaction branch only.
            reacts = {x.get('who'): x for x in r.get('reaction') or [] if isinstance(x, dict)}
            for d in (u.get('situation') or {}).get('new_dealings') or []:
                T[m]['dealing_people'] += 1
                rx = reacts.get(d.get('who'))
                if not rx or not isinstance(rx.get('delta'), (int, float)):
                    continue
                whats = ' '.join(e.get('what', '') for e in d.get('events') or [])
                deed = '거래' in whats or '답함' in whats
                words = any(e.get('words') for e in d.get('events') or [])
                key = 'deed' if deed else 'words' if words else 'other'
                L[m]['react_' + key].append(abs(rx['delta']))
                if '기만' in whats:                     # B49: N is hit harder by a betrayal
                    L[m]['react_deceit'].append(rx['delta'])
                if any(s in whats for s in ('refused', 'rejected', 'declined', 'countered')):
                    L[m]['react_refused'].append(rx['delta'])   # B56: J and P alike (a refusal is the partner's call)
                L[m]['react_all'].append(rx['delta'])
            if ph == 'movement':
                T[m]['move_decisions'] += 1
                T[m]['talk_intent'] += r.get('talk') is True   # [FIX-B54]
            elif ph == 'deduction':
                dm = r.get('deduction_math') or {}
                am = dict(u.get('accuse_math') or {})
                am.setdefault('combinations_left', jc.get(((u.get('me') or {}).get('character_id'), u.get('cycle'), 'deduction')))   # [FIX-B52]
                if r.get('decision') == 'accuse':
                    T[m]['accuse'] += 1
                    L[m]['accuse_combos'].append(am.get('combinations_left'))
                    L[m]['accuse_cycle'].append(u.get('cycle'))
                    T[m]['accuse_last'] += u.get('remaining_cycles') == 1
                if isinstance(dm.get('my_win_chance'), (int, float)) and am.get('combinations_left'):
                    L[m]['chance_x_combos'].append(dm['my_win_chance'] * am['combinations_left'])
            elif 'incoming_trade' in u:
                fit = bool(u.get('clues_fitting_their_request'))
                aff = ((u.get('my_affinity_to_them') or {}).get('score'))
                T[m]['in_trades'] += 1
                T[m]['in_' + str(r.get('decision'))] += 1
                if fit and isinstance(aff, (int, float)):
                    band = 'hi' if aff >= 60 else 'lo' if aff <= 40 else 'mid'
                    T[m][f'fit_{band}'] += 1
                    T[m][f'fit_{band}_acc'] += r.get('decision') == 'accept'
    # affinity and inferred attitudes from memory files
    peers = {}
    for f in (camp / 'agent-memory').glob('society_*.json'):
        d = json.loads(f.read_text(encoding='utf-8'))
        peers[d['agent_id']] = d.get('peers') or {}
    pairs = []
    for a_id, ps in peers.items():
        m = agent_type.get(a_id)
        for b_id, rec in ps.items():
            if m and 'affinity' in rec:
                L[m]['affinity'].append(rec['affinity'])
            back = (peers.get(b_id) or {}).get(a_id) or {}
            if 'their_attitude' in rec and 'affinity' in back:
                pairs.append((rec['their_attitude'], back['affinity']))

    def axis(letter_a, letter_b, pos):
        A = [m for m in T if m[pos] == letter_a]; Bt = [m for m in T if m[pos] == letter_b]
        def agg(types, key):
            return sum(T[m][key] for m in types)
        def lst(types, key):
            return [v for m in types for v in L[m][key]]
        return A, Bt, agg, lst

    print(f"agent-games {sum(T[m]['agent_games'] for m in T)} over {len(T)} types")
    for pos, (x, y) in enumerate((('E', 'I'), ('S', 'N'), ('T', 'F'), ('J', 'P'))):
        A, Bt, agg, lst = axis(x, y, pos)
        print(f"\n== {x} vs {y}")
        for lab, types in ((x, A), (y, Bt)):
            inter = agg(types, 'inter')
            line = (f"  {lab}: questions {rate(agg(types, 'act:question'), inter)}, explore {rate(agg(types, 'act:explore'), inter)}, "
                    f"trade {rate(agg(types, 'act:trade'), inter)}, prepare {rate(agg(types, 'act:prepare'), inter)}")
            print(line)
            if pos == 0:
                print(f"     social when 1-2 others {rate(agg(types, 'few:social'), agg(types, 'few:n'))}, "
                      f"with 3+ others {rate(agg(types, 'crowd:social'), agg(types, 'crowd:n'))}; "
                      f"message length {mean(lst(types, 'msg_len'))}, case talk in messages {rate(agg(types, 'case_talk'), agg(types, 'msgs'))}")
            if pos == 1:
                print(f"     reaction after being deceived: mean delta {mean(lst(types, 'react_deceit'))} (n={len(lst(types, 'react_deceit'))}); "
                      f"all reactions mean delta {mean(lst(types, 'react_all'))} (n={len(lst(types, 'react_all'))})")
                print(f"     accusations {agg(types, 'accuse')} at combos {sorted(c for c in lst(types, 'accuse_combos') if c)}; "
                      f"own chance x combos (1 = uniform) mean {mean(lst(types, 'chance_x_combos'))}")
            if pos == 2:
                q = agg(types, 'act:question')
                print(f"     persuade {rate(agg(types, 'ap:persuade'), q)}, interrogate+threaten {rate(agg(types, 'ap:interrogate') + agg(types, 'ap:threaten'), q)}, "
                      f"deception {rate(agg(types, 'deceptions'), agg(types, 'settled_as_giver'))}")
                print(f"     reaction |delta| after deeds {mean(lst(types, 'react_deed'))} (n={len(lst(types, 'react_deed'))}), "
                      f"after words only {mean(lst(types, 'react_words'))} (n={len(lst(types, 'react_words'))}); "
                      f"accept with fitting clue: affinity>=60 {rate(agg(types, 'fit_hi_acc'), agg(types, 'fit_hi'))}, "
                      f"40-60 {rate(agg(types, 'fit_mid_acc'), agg(types, 'fit_mid'))}, <=40 {rate(agg(types, 'fit_lo_acc'), agg(types, 'fit_lo'))}")
            if pos == 3:
                print(f"     reaction |delta| after deeds {mean(lst(types, 'react_deed'))} (n={len(lst(types, 'react_deed'))}), words {mean(lst(types, 'react_words'))} (n={len(lst(types, 'react_words'))}); "
                      f"delta after a refused trade {mean(lst(types, 'react_refused'))} (n={len(lst(types, 'react_refused'))})")
                print(f"     incoming trades: counter {rate(agg(types, 'in_counter'), agg(types, 'in_trades'))}, "
                      f"accept {rate(agg(types, 'in_accept'), agg(types, 'in_trades'))}, refuse {rate(agg(types, 'in_refuse'), agg(types, 'in_trades'))}")
    print("\n== talk rooms (B54), last-cycle accusations, post-game sharing (B55)")
    for pos, (x, y) in enumerate((('E', 'I'), ('S', 'N'), ('T', 'F'), ('J', 'P'))):
        A, Bt, agg, lst = axis(x, y, pos)
        for lab, types in ((x, A), (y, Bt)):
            said = lst(types, 'talk_said')
            big = [z for z in said if (z.get('room_size') or 0) >= 4]
            print(f"  {lab}: talk intent {rate(agg(types, 'talk_intent'), agg(types, 'move_decisions'))}, lines {agg(types, 'talk_lines')}, "
                  f"mean length {mean(lst(types, 'talk_len'))}, case talk {rate(agg(types, 'talk_case'), agg(types, 'talk_lines'))}, "
                  f"leave {rate(agg(types, 'talk_leave'), len(said))}, leave in rooms of 4+ {rate(sum(1 for z in big if z.get('leave')), len(big))}; "
                  f"accusations at the last cycle {rate(agg(types, 'accuse_last'), agg(types, 'accuse'))}; "
                  f"shared {agg(types, 'shared_games')} times, {agg(types, 'shared_points')} points")
    print("\n== affinity and inferred attitudes")
    allaff = [v for m in L for v in L[m]['affinity']]
    reacts = [v for m in L for v in L[m]['react_all']]
    print(f"  reactions {len(reacts)} (up {sum(v > 0 for v in reacts)}, down {sum(v < 0 for v in reacts)}, zero {sum(v == 0 for v in reacts)}); "
          f"stored affinity n={len(allaff)} mean {mean(allaff)} sd {round(st.pstdev(allaff), 2) if len(allaff) > 1 else None}")
    if len(pairs) >= 3:
        xs, ys = zip(*pairs)
        if st.pstdev(xs) and st.pstdev(ys):
            print(f"  my estimate of how B sees me vs B's affinity to me: n={len(pairs)}, correlation {round(st.correlation(xs, ys), 3)}")
        else:
            print(f"  my estimate vs B's affinity: n={len(pairs)} (no variance)")
    print("\n== 16 types: agent-games | wins/deaths | score mean | question/explore/trade share | reactions | affinity mean")
    for m in sorted(T):
        inter = T[m]['inter'] or 1
        print(f"  {m}: {T[m]['agent_games']} | {T[m]['wins']}/{T[m]['deaths']} | {mean(L[m]['score'])} | "
              f"{T[m]['act:question'] / inter:.2f}/{T[m]['act:explore'] / inter:.2f}/{T[m]['act:trade'] / inter:.2f} | "
              f"{len(L[m]['react_all'])} | {mean(L[m]['affinity'])}")


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
