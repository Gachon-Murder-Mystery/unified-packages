"""Element impact analysis: v8 -> v9 (11-09 bundle) -> v9 (11-10 bundle, talk rooms) -> lean main run.

Read-only. Recomputes, from the run records of each campaign, the indicators that the change log (CHANGES_EXPERIMENT30.md)
and the prediction table (docs/성격_16유형_예측.md §6) named when each element was introduced, plus a talk-room section.

    PYTHONUTF8=1 python -B tools/element_impact.py            # writes runs/element_impact.json and prints a digest
"""
import collections
import itertools
import json
import sqlite3
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAMPS = {
    'v8': ROOT / 'unified-packages-fixed' / 'runs' / 'v8-main-30c',        # gpt-6-luna, 20 games
    'v9a': ROOT / 'unified-packages-v9' / 'runs' / 'v9-main-30c',          # gpt-6.1-terra, 11-09 bundle, 12 games
    'chk7': ROOT / 'unified-packages-v9' / 'runs' / 'v9-check7-30c',       # 11-10 bundle, full prompts, 2 games
    'chk8': ROOT / 'unified-packages-v9' / 'runs' / 'v9-check8-30c-lean',  # 11-10 bundle, lean, same seeds, 2 games
    'v9b': ROOT / 'unified-packages-v9' / 'runs' / 'v9-main2-30c-lean',    # 11-10 bundle, lean, 16 games
}
DEADLINE = 30
TALK = '담화방'


def load(p):
    return json.loads(Path(p).read_text(encoding='utf-8'))


def completed(camp):
    by_seed = {}
    for a in load(camp / 'batch.json')['attempts']:
        if a['status'] == 'completed':
            by_seed[a['seed']] = a
    return [by_seed[s] for s in sorted(by_seed)]


def detail(gdir):
    for line in open(gdir / 'detail.jsonl', encoding='utf-8'):
        yield json.loads(line)


def calls(gdir):
    p = gdir / 'codex-calls.jsonl'
    if not p.exists():
        return
    for line in open(p, encoding='utf-8'):
        try:
            x = json.loads(line)
        except Exception:
            continue
        try:
            u = json.loads(x['user'])
        except Exception:
            u = None
        try:
            r = json.loads(x['response'])
        except Exception:
            r = None
        yield x, (u if isinstance(u, dict) else {}), (r if isinstance(r, dict) else {})


def call_kind(x, u):
    s = x.get('system') or ''
    if '담화방' in s[:60]:
        return 'talk'
    if u.get('phase') in ('movement', 'interaction', 'deduction'):
        return u['phase']
    if '질문을 받은' in s[:40]:
        return 'answer'
    if '교섭' in s[:30]:
        return 'trade_response'
    if '돌아보는' in s[:40]:
        return 'reflection'
    if '1위' in s[:40]:
        return 'share'
    return 'other'


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(st.median(xs), 3) if xs else None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(st.mean(xs), 3) if xs else None


def pct(a, b):
    return round(100 * a / b, 1) if b else None


def analyse(name, camp):
    db = sqlite3.connect(f'file:{camp / "pool.sqlite3"}?mode=ro', uri=True)
    G = []                      # per game rows
    ACC = []                    # per accusation
    REACT = []                  # per reaction
    AG = []                     # per agent-game
    pairs = collections.Counter()
    contra = []                 # (game, listener, speaker) -> max contradictions, claims heard from speaker
    call_n = collections.Counter()
    call_s = collections.Counter()
    for gi, a in enumerate(completed(camp), 1):
        s = load(a['summary_file'])
        gdir = Path(a['full_record_file']).parent
        g = json.loads(db.execute('select state from games where id=?', (a['game_id'],)).fetchone()[0])
        cid = {aid: p['character_id'] for aid, p in g['players'].items()}
        aid_of = {v: k for k, v in cid.items()}
        winners = {w['agent_id'] if isinstance(w, dict) else w for w in s.get('winners') or []}
        deaths = {d['agent_id'] if isinstance(d, dict) else d for d in s.get('deaths') or []}
        trades = list(g['trades'].values())
        settled = [t for t in trades if t['status'] == 'settled']
        refused = [t for t in trades if t['status'] == 'refused']
        key = lambda t: json.dumps(t.get('requested'), sort_keys=True)
        repeat = sum(any(u['proposer'] == t['proposer'] and u['recipient'] == t['recipient'] and key(u) == key(t)
                         and u.get('cycle', 0) > t.get('cycle', 0) for u in trades) for t in refused)
        decept = sum(1 for t in settled for v in (t.get('fulfilled') or {}).values() if v is False)
        # ---- journals
        acts = collections.Counter()
        heard = collections.defaultdict(collections.Counter)       # agent -> context -> n
        heard_from = collections.Counter()                          # (agent, speaker char) -> n
        talk_heard_by_cycle = collections.defaultdict(lambda: collections.Counter())  # agent -> cycle -> n talk claims
        talk_pairs = set()                                          # (cycle, frozenset(chars))
        talk_lines = collections.Counter()                          # agent -> lines
        talk_intent = collections.Counter()
        reactions = []
        dec_combos = {}
        alive_cycles = collections.Counter()
        for e in detail(gdir):
            ev = e.get('event')
            ag = e.get('agent_id')
            if ev == 'talk_said':
                talk_lines[ag] += 1
            elif ev == 'talk_intent':
                talk_intent[ag] += 1
            if ev != 'agent_journal':
                continue
            r = e.get('record') or {}
            k = r.get('event')
            if k == 'decision':
                acts[(r.get('phase'), r.get('action'))] += 1
                if r.get('phase') == 'movement':
                    alive_cycles[ag] += 1
                if r.get('phase') == 'deduction' and r.get('action') == 'accuse':
                    dec_combos[(ag, r.get('cycle'))] = r.get('combinations_left')
            elif k == 'claim_heard':
                ctx = r.get('context') or '?'
                heard[ag][ctx] += 1
                heard_from[(ag, r.get('from_character'))] += 1
                if ctx == TALK:
                    talk_heard_by_cycle[ag][r.get('cycle')] += 1
                    me = cid.get(ag)
                    if me is not None:
                        talk_pairs.add((r.get('cycle'), frozenset((me, r.get('from_character')))))
            elif k == 'reaction':
                reactions.append((ag, r))
        # ---- model calls
        tokens_in = 0
        mbti = {}
        maxcon = {}
        for x, u, r in calls(gdir):
            kind = call_kind(x, u)
            call_n[kind] += 1
            call_s[kind] += x.get('seconds') or 0
            tokens_in += ((x.get('usage') or {}).get('input') or 0)
            me = u.get('me') or {}
            if isinstance(me, dict) and me.get('mbti') and me.get('character_id') is not None:
                mbti[me['character_id']] = me['mbti']
            sit = u.get('situation') if isinstance(u.get('situation'), dict) else {}
            for c in sit.get('heard_about_case') or []:
                tg = c.get('this_game') or {}
                if 'contradictions' in tg and c.get('from') is not None:
                    lk = (me.get('character_id') if isinstance(me, dict) else None, c['from'])
                    maxcon[lk] = max(maxcon.get(lk, 0), tg['contradictions'] or 0)
            if kind == 'deduction' and (r.get('decision') == 'accuse' or (r.get('action') or {}).get('type') == 'accuse'):
                am = u.get('accuse_math') or {}
                dm = r.get('deduction_math') or {}
                ACC.append({'camp': name, 'game': gi, 'cycle': u.get('cycle'), 'char': me.get('character_id'),
                            'mbti': me.get('mbti'), 'win_chance': dm.get('my_win_chance'),
                            'score_wrong': am.get('score_if_wrong'), 'score_other_wins': am.get('score_if_someone_else_wins'),
                            'score_unsolved': am.get('score_if_unsolved_and_alive'), 'mission': am.get('my_mission_points')})
        for (lc, sc), n in maxcon.items():
            la = aid_of.get(lc)
            contra.append({'camp': name, 'contradictions': n, 'heard': heard_from.get((la, sc), 0)})
        # accusation outcomes: wrong accusation = death, right = win (one accusation per agent at most)
        for A in ACC:
            if A['camp'] == name and A['game'] == gi:
                aid = aid_of.get(A['char'])
                A['correct'] = aid in winners
                A['combos'] = dec_combos.get((aid, A['cycle']))
                A['talk_heard_before'] = sum(n for c, n in talk_heard_by_cycle[aid].items() if c is not None and c <= (A['cycle'] or 0))
                A['heard_total'] = sum(heard[aid].values())
                A['free_roll'] = (A['score_wrong'] is not None and A['score_other_wins'] is not None and
                                  A['score_wrong'] >= max(A['score_other_wins'], A['score_unsolved'] if A['score_unsolved'] is not None else -999))
        # reactions with talk flag
        for ag, r in reactions:
            me = cid.get(ag)
            c = r.get('cycle')
            pair = frozenset((me, r.get('who')))
            talked = (c, pair) in talk_pairs or (c - 1 if isinstance(c, int) else None, pair) in talk_pairs
            d = (r.get('after') or 0) - (r.get('before') or 0)
            REACT.append({'camp': name, 'game': gi, 'delta': d, 'before': r.get('before'), 'talked': talked})
        # co-located pairs: talk vs trade
        players = set(cid.values())
        deals = collections.defaultdict(lambda: [0, 0])
        for t in trades:
            k = (t.get('cycle'), frozenset((cid[t['proposer']], cid[t['recipient']])))
            deals[k][0] += 1
            deals[k][1] += t['status'] == 'settled'
        for p in g.get('placements') or []:
            rooms = collections.defaultdict(list)
            for ch, room in p['rooms'].items():
                if int(ch) in players:
                    rooms[room].append(int(ch))
            for chs in rooms.values():
                for x_, y_ in itertools.combinations(chs, 2):
                    k = (p['cycle'], frozenset((x_, y_)))
                    grp = 'talk' if k in talk_pairs else 'no_talk'
                    pairs[grp] += 1
                    pairs[grp + '_proposed'] += deals[k][0] > 0
                    pairs[grp + '_settled'] += deals[k][1] > 0
        # per agent-game
        for aid, ch in cid.items():
            AG.append({'camp': name, 'game': gi, 'mbti': mbti.get(ch), 'won': aid in winners, 'died': aid in deaths,
                       'talk_lines': talk_lines[aid], 'talk_intents': talk_intent[aid], 'cycles': alive_cycles[aid],
                       'heard': dict(heard[aid]), 'accused': any(A['camp'] == name and A['game'] == gi and A['char'] == ch for A in ACC)})
        inter = {k[1]: v for k, v in acts.items() if k[0] == 'interaction'}
        G.append({'game': gi, 'seed': a['seed'], 'solved': bool(winners), 'winners': len(winners), 'deaths': len(deaths),
                  'minutes': round(s['elapsed_seconds'] / 60), 'model_calls': s.get('model_calls'), 'tokens_in': tokens_in,
                  'questions': inter.get('question', 0), 'explores': inter.get('explore', 0), 'trade_actions': inter.get('trade', 0),
                  'prepares': inter.get('prepare', 0), 'inter_pass': inter.get('pass', 0),
                  'moves': acts.get(('movement', 'move'), 0), 'stays': acts.get(('movement', 'stay'), 0),
                  'proposed': len(trades), 'settled': len(settled), 'refused': len(refused), 'repeat': repeat,
                  'deceptions': decept, 'talk_lines': sum(talk_lines.values()),
                  'refuse_no_action_left': (s.get('trade_activity') or {}).get('refuse_no_action_left', 0)})
    return G, ACC, REACT, AG, dict(pairs), contra, dict(call_n), {k: round(v) for k, v in call_s.items()}


def stored_affinity(camp):
    xs = [v['affinity'] for p in (camp / 'agent-memory').glob('society_*.json')
          for v in (load(p).get('peers') or {}).values() if 'affinity' in v]
    return {'n': len(xs), 'mean': mean(xs), 'sd': round(st.pstdev(xs), 1) if xs else None,
            'at_100': sum(x >= 100 for x in xs)}


def summarize(name, camp):
    G, ACC, REACT, AG, pairs, contra, cn, cs = analyse(name, camp)
    n = len(G)
    ag_n = len(AG)
    acc = ACC
    out = {'games': n, 'solved': sum(g['solved'] for g in G), 'winners': sum(g['winners'] for g in G),
           'deaths': sum(g['deaths'] for g in G),
           'minutes_median': med([g['minutes'] for g in G]), 'model_calls_per_game': med([g['model_calls'] for g in G]),
           'tokens_in_per_game_M': round(st.median([g['tokens_in'] for g in G]) / 1e6, 1) if G else None,
           'accusations': len(acc), 'correct': sum(a['correct'] for a in acc),
           'acc_last_cycle': sum(a['cycle'] == DEADLINE for a in acc),
           'acc_per_agent_game': round(len(acc) / ag_n, 3) if ag_n else None,
           'win_chance_median': med([a['win_chance'] for a in acc]),
           'combos_at_accuse_median': med([a['combos'] for a in acc]),
           'free_roll': sum(bool(a['free_roll']) for a in acc),
           'free_roll_correct': sum(a['correct'] for a in acc if a['free_roll']),
           'not_free_roll': sum(not a['free_roll'] for a in acc),
           'not_free_roll_correct': sum(a['correct'] for a in acc if not a['free_roll']),
           'per_game': {k: round(sum(g[k] for g in G) / n, 1) for k in
                        ('questions', 'explores', 'trade_actions', 'prepares', 'inter_pass', 'moves', 'stays', 'proposed',
                         'settled', 'refused', 'repeat', 'deceptions', 'talk_lines', 'refuse_no_action_left')},
           'repeat_rate': pct(sum(g['repeat'] for g in G), sum(g['refused'] for g in G)),
           'settle_rate': pct(sum(g['settled'] for g in G), sum(g['proposed'] for g in G)),
           'calls_by_kind': cn, 'seconds_by_kind': cs}
    heard = collections.Counter()
    for r in AG:
        heard.update(r['heard'])
    out['heard_per_agent_game'] = {k: round(v / ag_n, 1) for k, v in heard.most_common()}
    if REACT:
        out['reactions'] = {'n': len(REACT), 'up': sum(r['delta'] > 0 for r in REACT), 'down': sum(r['delta'] < 0 for r in REACT),
                            'zero': sum(r['delta'] == 0 for r in REACT), 'mean_delta': mean([r['delta'] for r in REACT]),
                            'before_by_game': [mean([r['before'] for r in REACT if r['game'] == gi]) for gi in range(1, n + 1)]}
        for flag in (True, False):
            R = [r for r in REACT if r['talked'] == flag]
            out['reactions']['talked' if flag else 'not_talked'] = {
                'n': len(R), 'up': pct(sum(r['delta'] > 0 for r in R), len(R)), 'down': pct(sum(r['delta'] < 0 for r in R), len(R)),
                'mean_delta': mean([r['delta'] for r in R]),
                'mean_delta_below_90': mean([r['delta'] for r in R if (r['before'] or 0) < 90])}
        out['affinity_stored'] = stored_affinity(camp)
    if contra:
        out['contradictions'] = {'pairs': len(contra), 'pairs_with_any': sum(c['contradictions'] > 0 for c in contra),
                                 'sum': sum(c['contradictions'] for c in contra), 'claims_heard_from_these': sum(c['heard'] for c in contra)}
    if pairs:
        out['colocated_pairs'] = pairs
    # talk participation vs outcome (per agent-game), split by E/I
    if any(r['talk_lines'] for r in AG):
        def grp(rows):
            return {'n': len(rows), 'win': pct(sum(r['won'] for r in rows), len(rows)), 'died': pct(sum(r['died'] for r in rows), len(rows)),
                    'accused': pct(sum(r['accused'] for r in rows), len(rows)),
                    'talk_heard': mean([r['heard'].get(TALK, 0) for r in rows])}
        lines = sorted(r['talk_lines'] for r in AG)
        cut = st.median(lines)
        out['talk_vs_outcome'] = {'median_lines': cut}
        for ei in ('E', 'I'):
            rows = [r for r in AG if (r['mbti'] or '?')[0] == ei]
            out['talk_vs_outcome'][ei] = {'high_talk': grp([r for r in rows if r['talk_lines'] > cut]),
                                         'low_talk': grp([r for r in rows if r['talk_lines'] <= cut])}
        A2 = [a for a in acc if a.get('talk_heard_before') is not None]
        out['accusers_talk_exposure'] = {
            'correct_median_talk_heard': med([a['talk_heard_before'] for a in A2 if a['correct']]),
            'wrong_median_talk_heard': med([a['talk_heard_before'] for a in A2 if not a['correct']])}
    # exposure normalised by cycles lived (dead agents stop hearing and speaking, so raw counts favour survivors)
    if any(r['heard'].get(TALK) for r in AG):
        rows = [dict(r, rate=r['heard'].get(TALK, 0) / r['cycles'], say=r['talk_lines'] / r['cycles']) for r in AG if r['cycles']]
        qs = st.quantiles([r['rate'] for r in rows], n=4)
        def band(x):
            return sum(x > q for q in qs)
        def grp(rs):
            return {'n': len(rs), 'heard_per_cycle': mean([r['rate'] for r in rs]), 'accused': pct(sum(r['accused'] for r in rs), len(rs)),
                    'died': pct(sum(r['died'] for r in rs), len(rs)), 'won': pct(sum(r['won'] for r in rs), len(rs)),
                    'cycles_lived': mean([r['cycles'] for r in rs])}
        out['talk_rate_quartiles'] = {'cuts': [round(q, 2) for q in qs],
                                      'all': [grp([r for r in rows if band(r['rate']) == b]) for b in range(4)]}
        for ei in ('E', 'I'):
            sub = [r for r in rows if (r['mbti'] or '?')[0] == ei]
            out['talk_rate_quartiles'][ei] = [grp([r for r in sub if band(r['rate']) == b]) for b in range(4)]
        A2 = [a for a in acc if a.get('talk_heard_before') is not None and a.get('cycle')]
        for a in A2:
            a['rate_before'] = a['talk_heard_before'] / a['cycle']
        out['accusers_talk_rate'] = {
            'correct': {'n': sum(a['correct'] for a in A2), 'median_rate': med([a['rate_before'] for a in A2 if a['correct']]),
                        'median_cycle': med([a['cycle'] for a in A2 if a['correct']])},
            'wrong': {'n': sum(not a['correct'] for a in A2), 'median_rate': med([a['rate_before'] for a in A2 if not a['correct']]),
                      'median_cycle': med([a['cycle'] for a in A2 if not a['correct']])}}
        # correctness by exposure band among accusations
        cuts = st.quantiles([a['rate_before'] for a in A2], n=2) if len(A2) > 3 else []
        if cuts:
            lo = [a for a in A2 if a['rate_before'] <= cuts[0]]
            hi = [a for a in A2 if a['rate_before'] > cuts[0]]
            out['accusers_talk_rate']['split_at'] = round(cuts[0], 2)
            out['accusers_talk_rate']['low_exposure'] = {'n': len(lo), 'correct': sum(a['correct'] for a in lo), 'median_cycle': med([a['cycle'] for a in lo])}
            out['accusers_talk_rate']['high_exposure'] = {'n': len(hi), 'correct': sum(a['correct'] for a in hi), 'median_cycle': med([a['cycle'] for a in hi])}
    out['_rows'] = G
    out['_acc'] = acc
    return out


def main():
    res = {}
    for name, camp in CAMPS.items():
        if not (camp / 'batch.json').exists():
            print('skip', name, file=sys.stderr)
            continue
        print('analysing', name, file=sys.stderr)
        res[name] = summarize(name, camp)
    out = ROOT / 'unified-packages-v9' / 'runs' / 'element_impact.json'
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding='utf-8')
    for name, r in res.items():
        print(f'\n== {name}')
        for k, v in r.items():
            if not k.startswith('_'):
                print(f'  {k}: {json.dumps(v, ensure_ascii=False)}')


if __name__ == '__main__':
    main()
