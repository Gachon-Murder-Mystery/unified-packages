"""v9 check against the textbook-based gaps (docs/v9_점검표.md): what the logs of a v9 campaign show.

    PYTHONUTF8=1 python -B tools/v9_check.py . <campaign>

One line per check item (ID matches the checklist). Reads batch.json, the pool DB, each game's
detail.jsonl (agent journals) and codex-calls.jsonl (the prompts the model saw), the agents' own
memory files and the GM's REPORT. Other tools add detail: audit_memory.py, replay_traces.py,
relationship_monitor.py, mbti_monitor.py, review_campaign.py, game_report.py.
"""
import collections
import json
import re
import sqlite3
import statistics as st
import sys
from pathlib import Path


def journal(gdir):
    for line in open(gdir / 'detail.jsonl', encoding='utf-8'):
        e = json.loads(line)
        if e.get('event') == 'agent_journal':
            yield e.get('agent_id'), e.get('record') or {}


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


def prompts(gdir):
    path = gdir / 'codex-calls.jsonl'
    if not path.exists():
        return
    for line in open(path, encoding='utf-8'):
        x = json.loads(line)
        try:
            u = json.loads(x['user'])
            r = json.loads(x['response']) if x.get('response') else {}
        except Exception:
            continue
        if isinstance(u, dict):
            yield u, (r if isinstance(r, dict) else {}), x


def main(root, name):
    root = Path(root); camp = root / 'runs' / name
    b = json.loads((camp / 'batch.json').read_text(encoding='utf-8'))
    db = sqlite3.connect(f'file:{camp / "pool.sqlite3"}?mode=ro', uri=True)
    games = [a for a in b['attempts'] if a['status'] == 'completed']
    c = collections.Counter()
    claims_seen, seek_seen, decisions = [], [], 0
    refl, t_paths, plan_checks = [], collections.Counter(), []
    ded = collections.Counter(); ev_follow = [0, 0]; chance_vs_uniform = []
    refused_again = [0, 0]
    gm_incidents, rel_reports = collections.Counter(), 0
    coop = collections.Counter()   # N1
    for a in games:
        gdir = Path(a['full_record_file']).parent
        g = json.loads(db.execute('select state from games where id=?', (a['game_id'],)).fetchone()[0])
        mbti = {p['agent_id']: g['characters'][str(p['character_id'])]['mbti'] for p in g['players'].values()}
        for aid, r in journal(gdir):
            ev = r.get('event')
            if ev == 'memory_ops':
                c['note_ops_applied'] += len(r.get('applied') or [])
                c['note_ops_refused'] += len(r.get('refused') or [])
            elif ev == 'impression':
                c['impressions'] += 1
            elif ev == 'reflection':
                refl.append(r)
                if r.get('plan_check'):
                    plan_checks.append(r['plan_check'])
                ops = r.get('operations') or []
                c['insight_ops'] += len(ops)
                m = mbti.get(aid, '')
                if len(m) == 4 and m[2] == 'T':
                    plan = r.get('plan') or ''
                    t_paths['교류' if re.search('교류|먼저 내주|오래 주고받', plan) else '단기 실리' if re.search('단기|실리|손익', plan) else '언급 없음'] += 1
            elif ev == 'deduction_math':
                ded['ok' if r.get('ok') else 'error'] += 1
        done_by_char = {}
        jc = journal_combinations(gdir, g)   # [FIX-B52]
        for u, resp, x in prompts(gdir):
            sit = u.get('situation') or {}
            me = (u.get('me') or {}).get('character_id')
            subs = u.get('sub_missions')
            if me is not None and subs:
                done_by_char[me] = done_by_char.get(me) or all(m.get('achieved') for m in subs)
            tag = 'done' if done_by_char.get(me) else 'open'
            if 'response_options' in u and isinstance(resp.get('statements'), list) and resp['statements']:
                coop[tag + '_ans'] += 1
                coop[tag + '_def'] += any(s.get('kind') == 'definite' for s in resp['statements'] if isinstance(s, dict))
            if 'incoming_trade' in u and u.get('clues_fitting_their_request'):
                coop[tag + '_tr'] += 1
                coop[tag + '_acc'] += resp.get('decision') == 'accept'
            if u.get('phase') == 'deduction' and u.get('remaining_cycles') == 1 and resp.get('decision') == 'pass':
                coop['pass_n'] += 1
                assumption = str((resp.get('deduction_math') or {}).get('pass_assumption') or '')
                coop['pass_other'] += any(w in assumption for w in ('다른 사람', '다른 플레이어', '누군가', '남이', '다른 생존자', '타인'))
            if u.get('phase') in ('interaction', 'deduction', 'movement'):
                decisions += 1
                claims_seen.append(bool(sit.get('heard_about_case')))
                seek_seen.append(bool(sit.get('seek')))
            if u.get('phase') == 'deduction':
                m = resp.get('deduction_math') or {}
                am = u.get('accuse_math') or {}
                if isinstance(m.get('expected_if_accuse'), (int, float)) and isinstance(m.get('expected_if_pass'), (int, float)):
                    ev_follow[1] += 1
                    ev_follow[0] += (resp.get('decision') == 'accuse') == (m['expected_if_accuse'] > m['expected_if_pass'])
                combos = am.get('combinations_left') or jc.get(((u.get('me') or {}).get('character_id'), u.get('cycle'), 'deduction'))
                if combos and isinstance(m.get('my_win_chance'), (int, float)):
                    chance_vs_uniform.append(m['my_win_chance'] * combos)
        # repeated request to the same partner for the same type after a refusal (v8: 45/312)
        refusals = set()
        for t in sorted(g['trades'].values(), key=lambda t: t.get('cycle') or 0):
            key = (t['proposer'], t['recipient'], tuple(sorted(i.get('type') for i in t.get('requested') or [] if isinstance(i, dict))))
            if t['status'] == 'refused':
                refused_again[1] += 1
                if key in refusals:
                    refused_again[0] += 1
                refusals.add(key)
        gm = ((json.loads(Path(a['summary_file']).read_text(encoding='utf-8')).get('gm_memory') or {}).get('watch') or {})
        for kind, v in (gm.get('incidents') or {}).items():
            gm_incidents[kind] += v.get('count', 0)
        rel = (g.get('gm_report') or {}).get('reported_relationships') or {}
        rel_reports += rel.get('reports', 0)
    insights, affinity = {}, []
    for f in (camp / 'agent-memory').glob('society_*.json'):
        d = json.loads(f.read_text(encoding='utf-8'))
        insights[d['agent_id']] = [i.get('text') for i in d.get('insights') or []]
        affinity += [v['affinity'] for v in (d.get('peers') or {}).values() if 'affinity' in v]
    gm_files = list((camp / 'agent-memory').glob('gm_*.json'))
    playbook = json.loads(gm_files[0].read_text(encoding='utf-8')).get('playbook') if gm_files else None
    traces = camp / 'regression_traces.jsonl'

    def pct(x):
        return f"{sum(x)}/{len(x)} ({100 * sum(x) / len(x):.0f}%)" if x else 'n/a'
    print(f"games {len(games)} · decisions {decisions}")
    print(f"M1 cards/seek in decision prompts: seek {pct(seek_seen)}")
    print(f"M2 claims about the case in decision prompts (B45): {pct(claims_seen)}")
    print(f"M3 notes: ops applied {c['note_ops_applied']}, refused {c['note_ops_refused']}")
    print(f"R1 reflections {len(refl)}, with plan_check {len(plan_checks)} (games 2+ should check the last plan)")
    print(f"R2 ExpeL insight ops {c['insight_ops']}; insights per agent {sorted(len(v) for v in insights.values())}")
    print(f"R3 T agents' chosen path in their plans (TB1 11.5): {dict(t_paths)}")
    print(f"U1 deduction_math checks {dict(ded)}; decision follows own higher EV {ev_follow[0]}/{ev_follow[1]}; "
          f"own chance x combinations (1 = uniform) median {st.median(chance_vs_uniform) if chance_vs_uniform else 'n/a'}")
    print(f"A1 affinity: impressions {c['impressions']}, stored scores n={len(affinity)}"
          + (f" mean {st.mean(affinity):.1f} sd {st.pstdev(affinity):.1f}" if affinity else ''))
    print(f"A2 relationship reports recorded by the GM: {rel_reports}")
    print(f"X1 same request to the same partner after a refusal: {refused_again[0]}/{refused_again[1]} refusals (v8: 45/312 requests)")
    print(f"N1 after finishing all missions (score incentive to let others solve): "
          f"answers giving a definite clue {coop['done_def']}/{coop['done_ans']} vs others {coop['open_def']}/{coop['open_ans']}; "
          f"trade accepts {coop['done_acc']}/{coop['done_tr']} vs others {coop['open_acc']}/{coop['open_tr']}; "
          f"last-cycle pass assumptions naming another solver {coop['pass_other']}/{coop['pass_n']}")
    print(f"G1 GM incidents {dict(gm_incidents)}; playbook {json.dumps(playbook, ensure_ascii=False)[:300] if playbook else None}")
    print(f"G2 regression traces: {sum(1 for _ in open(traces, encoding='utf-8')) if traces.exists() else 0} records")


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
