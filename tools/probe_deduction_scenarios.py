"""Harder probe for B44: deduction situations where the right win chance is NOT 1/combinations.

    PYTHONUTF8=1 python -B tools/probe_deduction_scenarios.py <v8 campaign dir> <out dir> <reps> <model> [<model> ...]

Built on a real v8 last-cycle deduction prompt (seed 20, cycle 30: the journalist, INFJ), with its
system prompt rewritten for B44 (as in probe_deduction_math.py). The belief module already folds the
midnight rule (culprit and weapon were in the crime room at midnight) into the remaining candidates,
so the only legitimate reasons for a non-uniform chance are information the belief does not use:
narrated (vague but truthful) answers, unverified talk weighted by who said it, and the value of time.

  S1  four combinations left, last cycle                        -> accuse (EV +10 > pass -10)
  S2  18 left; two truthful narrated answers point at the vault -> chance above uniform, place 5 (direction only)
  S3a 16 left; a partner with 4 honest trades says 'knife (5) is not it'      -> chance up
  S3b same claim from a partner who deceived me twice                         -> stay near uniform, pass
  S4  weapon settled (knife) + engine-certified 'knife was in the kitchen at midnight' => place 3;
      a trusted partner insists 'the vault, certainly'          -> accuse with place 3 (place 5 = trap)
  S5  cycle 20 of 30, all missions done (60), 120 left: wrong = +40 now, but the same floor is still
      available at the last cycle with more information         -> pass now (accusing now = error)
"""
import copy
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from offline_llm import CodexCLIClient, find_codex  # noqa: E402
from probe_deduction_math import OLD_SCORING, NEW_SCORING, OLD_DECISION, NEW_DECISION, check, parse  # noqa: E402

SUSPECTS = {0: '알렉스 누아르 (탐정)', 1: '케이시 스칼렛 (배우)', 2: '에이버리 재스퍼 (사업가)', 3: '로언 앰버 (사기꾼)',
            4: '라일리 올리브 (교수)', 5: '다코타 네이비 (대령)', 6: '케이시 더블루 (의사)', 7: '바이올렛 모레티 (상속인)',
            8: '캐머런 펄 (기술자)', 9: '캉디드 블랑슈 (요리사)', 10: '퀸 슬레이트 (차장)', 11: '파커 에보니 (기자)'}
WEAPONS = {0: '골동품 단검', 1: '유리 조각', 2: '드라이버', 3: '만년필', 4: '편지칼', 5: '식칼', 6: '총검', 7: '머리핀'}
PLACES = {0: '라운지 칸', 1: '침대 칸', 2: '식당 칸', 3: '주방 칸', 4: '수하물 칸', 5: '금고 칸', 6: '전망 칸', 7: '기관실'}
CATS = {'suspect': SUSPECTS, 'weapon': WEAPONS, 'place': PLACES}


def template(camp):
    b = json.loads((camp / 'batch.json').read_text(encoding='utf-8'))
    a = next(x for x in b['attempts'] if x['seed'] == 20 and x['status'] == 'completed')
    for line in open(Path(a['full_record_file']).parent / 'codex-calls.jsonl', encoding='utf-8'):
        x = json.loads(line)
        try:
            u = json.loads(x['user'])
        except Exception:
            continue
        if isinstance(u, dict) and u.get('phase') == 'deduction' and u.get('cycle') == 30 and (u.get('me') or {}).get('character_id') == 11:
            return x['system'].replace(OLD_SCORING, NEW_SCORING).replace(OLD_DECISION, NEW_DECISION), u
    raise SystemExit('template prompt not found')


def build(u, remaining, cycle=30, missions_done=False, extra_info=(), claims=(), trade_log=None, midnight=None):
    u = copy.deepcopy(u)
    deadline = 30
    u['cycle'] = cycle
    u['remaining_cycles'] = deadline - cycle + 1
    u['now'] = dict(u['now'], cycle=cycle)
    u['time_budget'] = dict(u['time_budget'], estimated_remaining_cycles=deadline - cycle + 1)
    for m in u['sub_missions']:
        m['achieved'] = bool(missions_done)
        m['achieved_cycle'] = (cycle - 3) if missions_done else None
    banked = 60 if missions_done else 0
    combos = len(remaining['suspect']) * len(remaining['weapon']) * len(remaining['place'])
    u['accuse_math'] = {'combinations_left': combos, 'my_mission_points': banked,
                        'score_if_correct_first': 100 + banked, 'score_if_wrong': -20 + banked,
                        'score_if_unsolved_and_alive': -10, 'score_if_someone_else_wins': banked}
    info, n = [], 0
    for cat, names in CATS.items():
        for cid in names:
            if cid not in remaining[cat]:
                n += 1
                info.append({'id': f'clue:scn{n:03d}', 'kind': 'definite', 'category': cat, 'candidate': cid,
                             'text': f'{cat} 후보 {cid}은(는) 정답이 아니다.', 'via': 'explore' if n % 3 else 'trade'})
    info.extend(extra_info)
    u['my_information'] = info
    belief = {}
    for cat, names in CATS.items():
        rem = remaining[cat]
        belief[cat] = {'remaining': [{'id': c, 'name': names[c], 'p': round(1 / len(rem), 4)} for c in rem],
                       'remaining_count': len(rem), 'excluded_count': len(names) - len(rem), 'settled': rem[0] if len(rem) == 1 else None}
    belief.update(midnight_facts=midnight or {'character_rooms': 0, 'weapon_rooms': 0, 'companions': 0},
                  definite_count=n, unusable_count=sum(1 for i in extra_info if i.get('kind') == 'narrated'),
                  combinations_left=combos, indirect_leverage=[], pair_exclusions=[])
    u['belief'] = belief
    u['heard_statements'] = []
    u['heard_claims'] = list(claims)
    u['trades'] = []
    if trade_log is not None:
        u['my_trade_log'] = trade_log
    u['announcements'] = [] if cycle < 20 else [{'ref': 'deadline:10', 'kind': 'deadline_warning', 'remaining_cycles': 10}]
    return u


def scenarios(u):
    honest = [{'cycle': c, 'partner_character': 3, 'status': 'settled', 'i_judged': 'fulfilled'} for c in (8, 13, 19, 24)]
    deceived = [{'cycle': 11, 'partner_character': 7, 'status': 'settled', 'i_judged': 'broken (선언과 다른 단서를 받음)'},
                {'cycle': 23, 'partner_character': 7, 'status': 'settled', 'i_judged': 'broken (선언과 다른 단서를 받음)'}]
    claim_text = '제가 원문 확실 단서로 직접 봤어요. 흉기 후보 5(식칼)는 정답이 아니에요.'
    return {
        'S1_few_left': build(u, {'suspect': [5], 'weapon': [2, 6], 'place': [3, 5]}),
        'S2_narrated': build(u, {'suspect': [2, 6, 9], 'weapon': [0, 5], 'place': [1, 5, 7]}, extra_info=[
            {'id': 'clue:scnN1', 'kind': 'narrated', 'text': '자정에 금고 칸에 누군가 있었어요.', 'via': 'question', 'from_character': 8},
            {'id': 'clue:scnN2', 'kind': 'narrated', 'text': '자정에 금고 칸에 흉기로 쓸 만한 물건이 놓여 있었어요.', 'via': 'question', 'from_character': 4}]),
        'S3a_trusted_claim': build(u, {'suspect': [2, 6], 'weapon': [0, 5], 'place': [1, 3, 5, 7]},
                                   claims=[{'cycle': 28, 'from_character': 3, 'context': 'trade', 'text': claim_text}],
                                   trade_log=honest),
        'S3b_deceiver_claim': build(u, {'suspect': [2, 6], 'weapon': [0, 5], 'place': [1, 3, 5, 7]},
                                    claims=[{'cycle': 28, 'from_character': 7, 'context': 'trade', 'text': claim_text}],
                                    trade_log=deceived),
        'S4_conflict': build(u, {'suspect': [2, 6, 9], 'weapon': [5], 'place': [3]}, extra_info=[
            {'id': 'clue:scnW1', 'kind': 'indirect', 'type': 'weapon_room', 'text': '식칼은(는) 자정에 주방 칸에 있었다.',
             'weapon': 5, 'room': 3, 'via': 'explore'}],
            claims=[{'cycle': 29, 'from_character': 3, 'context': 'answer', 'text': '범행 장소는 금고 칸이 확실해요. 원문 단서를 봤어요.'}],
            trade_log=honest, midnight={'character_rooms': 0, 'weapon_rooms': 1, 'companions': 0}),
        'S5_time_value': build(u, {'suspect': [2, 5, 6, 9, 10], 'weapon': [0, 2, 5, 6], 'place': [0, 1, 3, 4, 5, 7]},
                               cycle=20, missions_done=True),
    }


def judge(name, parsed, am):
    """Scenario-specific verdict next to the generic B44 check."""
    if not isinstance(parsed, dict):
        return 'no_answer'
    d = parsed.get('decision')
    hyp = parsed.get('current_hypothesis') or {}
    p = (parsed.get('deduction_math') or {}).get('my_win_chance')
    uniform = 1 / am['combinations_left']
    if name.startswith('S1'):
        return 'right' if d == 'accuse' else 'missed_accuse'
    if name.startswith('S2'):
        up = isinstance(p, (int, float)) and p > uniform * 1.2
        return ('used_narration' if up and hyp.get('place') == 5 else 'place_5_only' if hyp.get('place') == 5
                else 'uniform' if not up else 'up_other_place')
    if name.startswith('S3'):
        return f"p={p} ({'up' if isinstance(p, (int, float)) and p > uniform * 1.2 else 'uniform'}) {d}"
    if name.startswith('S4'):
        if d == 'accuse':
            return 'right' if hyp.get('place') == 3 else 'TRAP_place_' + str(hyp.get('place'))
        return 'passed_on_3_combos'
    if name.startswith('S5'):
        return 'right_pass' if d == 'pass' else 'accused_now'
    return d


def main(argv):
    if len(argv) < 4:
        raise SystemExit(__doc__)
    camp, out_dir, reps, models = Path(argv[0]), Path(argv[1]), int(argv[2]), argv[3:]
    out_dir.mkdir(parents=True, exist_ok=True)
    system, base = template(camp)
    items = scenarios(base)
    (out_dir / 'scenarios.json').write_text(json.dumps({k: v for k, v in items.items()}, ensure_ascii=False, indent=1), encoding='utf-8')
    exe = find_codex()
    for model in models:
        client = CodexCLIClient(exe, out_dir / f'{model}-calls.jsonl', model=model, reasoning='medium')
        rows, lock = [], threading.Lock()

        def one(job):
            name, rep = job
            u = items[name]
            text, usage, err = client._attempt(system, json.dumps(u, ensure_ascii=False))
            parsed = parse(text)
            generic = check(parsed, u['accuse_math']) if not err else 'call_error'
            if generic not in ('ok', 'call_error'):   # one retry with the reason, like the agent
                u2 = dict(u, now=dict(u['now'], previous_output_rejected={'reason': generic}))
                text, usage, err = client._attempt(system, json.dumps(u2, ensure_ascii=False))
                parsed = parse(text)
                generic = (check(parsed, u['accuse_math']) if not err else 'call_error') + ' (retry)'
            m = (parsed or {}).get('deduction_math') or {}
            row = {'scenario': name, 'rep': rep, 'generic': generic, 'verdict': judge(name, parsed, u['accuse_math']),
                   'decision': (parsed or {}).get('decision'), 'hypothesis': (parsed or {}).get('current_hypothesis'),
                   'math': m, 'consideration': str((parsed or {}).get('consideration') or '')[:400]}
            with lock:
                rows.append(row)

        jobs = [(n, r) for n in items for r in range(reps)]
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(one, jobs))
        rows.sort(key=lambda r: (r['scenario'], r['rep']))
        (out_dir / f'{model}.jsonl').write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n', encoding='utf-8')
        print(f'== {model}')
        for name in items:
            rs = [r for r in rows if r['scenario'] == name]
            print(f"  {name:20s} " + ' | '.join(f"{r['verdict']} [{r['generic']}]" for r in rs))


if __name__ == '__main__':
    main(sys.argv[1:])
