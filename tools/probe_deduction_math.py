"""Probe: can a model do the B44 deduction reasoning (its own win chance, expected scores)?

    PYTHONUTF8=1 python -B tools/probe_deduction_math.py <v8 campaign dir> <out dir> <model> [<model> ...]

Takes real deduction-phase prompts from a finished v8 campaign (codex-calls.jsonl), rebuilds them
with the v9 B44 instructions (no precomputed success_if_random; deduction_math required), sends each
once to every model through the same Codex CLI client the games use (reasoning medium), retries once
with the rejection reason like the agent does, and scores the answers with the agent's own checks.
Sample (fixed seed): 8 last-cycle prompts without mission points, 4 last-cycle with mission points,
8 mid-game prompts. Results: <out dir>/<model>.jsonl and a summary on stdout.
"""
import json
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from offline_llm import CodexCLIClient, find_codex  # noqa: E402

OLD_SCORING = ("- 서브 미션 보상은 하 10, 중 20, 상 30. scoring과 accuse_math(남은 조합 수, 무작위 성공 확률, 선택지별 점수)가 "
               "선택지마다 내 점수를 보여 준다. 기다리는 동안 다른 사람이 먼저 맞힐 수도 있다. 언제 고발할지는 네 성격대로 정한다.\n")
NEW_SCORING = ("- 서브 미션 보상은 하 10, 중 20, 상 30. scoring과 accuse_math(남은 조합 수, 선택지별 점수)가 선택지마다 내 점수를 보여 준다. "
               "기다리는 동안 다른 사람이 먼저 맞힐 수도 있다. 언제 고발할지는 네 성격대로 정한다.\n"
               "- 추리 단계에서는 결정 전에 deduction_math로 기대 점수를 스스로 따진다. my_win_chance는 내가 추정한 정답 확률이다"
               "(남은 조합, 가설에 대한 확신, 간접 단서로 내가 정한다). expected_if_accuse = my_win_chance × score_if_correct_first + "
               "(1 − my_win_chance) × score_if_wrong. expected_if_pass는 이번에 넘길 때의 기대 점수와 그 가정(pass_assumption)이다. "
               "마지막 사이클이면 미해결(score_if_unsolved_and_alive)이거나 남이 맞힘(score_if_someone_else_wins)이고, 아니면 다음 기회의 가치가 더해진다. "
               "기대 점수가 낮은 쪽을 고를 수도 있지만 그때는 choice_reason에 이유를 쓴다.\n")
OLD_DECISION = '  "decision": "accuse" 또는 "pass" (deduction 단계에서만. consideration의 결론),\n'
NEW_DECISION = (OLD_DECISION + '  "deduction_math": {"my_win_chance": 0~1, "expected_if_accuse": 숫자, "expected_if_pass": 숫자, '
                '"pass_assumption": "...", "choice_reason": "기대 점수가 낮은 쪽을 고를 때만"} (deduction 단계에서만),\n')


def sample(camp, seed=7):
    b = json.loads((camp / 'batch.json').read_text(encoding='utf-8'))
    pools = {'last_no_mission': [], 'last_mission': [], 'mid': []}
    for a in b['attempts']:
        if a['status'] != 'completed':
            continue
        calls = Path(a['full_record_file']).parent / 'codex-calls.jsonl'
        for line in open(calls, encoding='utf-8'):
            x = json.loads(line)
            try:
                u = json.loads(x['user'])
            except Exception:
                continue
            if not isinstance(u, dict) or u.get('phase') != 'deduction' or OLD_SCORING not in x['system'] or OLD_DECISION not in x['system']:
                continue
            am = u.get('accuse_math') or {}
            if u.get('cycle') == 30:
                pools['last_mission' if (am.get('my_mission_points') or 0) > 0 else 'last_no_mission'].append((a['seed'], x['system'], u))
            elif 10 <= (u.get('cycle') or 0) <= 25:
                pools['mid'].append((a['seed'], x['system'], u))
    rng = random.Random(seed)
    picked = []
    for key, n in (('last_no_mission', 8), ('last_mission', 4), ('mid', 8)):
        for s, system, u in rng.sample(pools[key], min(n, len(pools[key]))):
            u = json.loads(json.dumps(u))
            (u.get('accuse_math') or {}).pop('success_if_random', None)
            system = system.replace(OLD_SCORING, NEW_SCORING).replace(OLD_DECISION, NEW_DECISION)
            picked.append({'kind': key, 'seed': s, 'cycle': u.get('cycle'), 'system': system, 'user': u})
    return picked


def check(parsed, am):
    """Mirror of LLMAgent._consistent + _deduction_math_ok."""
    if not isinstance(parsed, dict):
        return 'not_json'
    action = (parsed.get('action') or {}).get('type')
    if parsed.get('decision') not in ('accuse', 'pass') or parsed.get('decision') != action:
        return 'decision_vs_action'
    m = parsed.get('deduction_math')
    if not isinstance(m, dict):
        return 'math_missing'
    p, ea, ep = m.get('my_win_chance'), m.get('expected_if_accuse'), m.get('expected_if_pass')
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (p, ea, ep)) or not 0 <= p <= 1:
        return 'math_types'
    if not str(m.get('pass_assumption') or '').strip():
        return 'pass_assumption_missing'
    want = p * am['score_if_correct_first'] + (1 - p) * am['score_if_wrong']
    if abs(ea - want) > max(1.0, 0.02 * abs(want)):
        return 'arithmetic'
    if ((action == 'accuse' and ea < ep) or (action == 'pass' and ep < ea)) and not str(m.get('choice_reason') or '').strip():
        return 'lower_without_reason'
    return 'ok'


def parse(text):
    text = (text or '').strip()
    if text.startswith('```'):
        text = text.strip('`').split('\n', 1)[-1]
    try:
        return json.loads(text[text.index('{'):text.rindex('}') + 1])
    except Exception:
        return None


def run(model, items, out_dir, exe):
    client = CodexCLIClient(exe, out_dir / f'{model}-calls.jsonl', model=model, reasoning='medium')
    lock = threading.Lock()
    rows = []

    def one(i, item):
        am = item['user']['accuse_math']
        text, usage, err = client._attempt(item['system'], json.dumps(item['user'], ensure_ascii=False))
        first = check(parse(text), am) if not err else 'call_error'
        final, parsed, retry = first, parse(text), False
        if first not in ('ok', 'call_error'):
            retry = True
            u = dict(item['user'])
            u['now'] = dict(u.get('now') or {}, previous_output_rejected={'reason': first})
            text2, usage2, err2 = client._attempt(item['system'], json.dumps(u, ensure_ascii=False))
            parsed = parse(text2)
            final = check(parsed, am) if not err2 else 'call_error'
        m = (parsed or {}).get('deduction_math') or {}
        row = {'i': i, 'kind': item['kind'], 'seed': item['seed'], 'cycle': item['cycle'],
               'combos': am.get('combinations_left'), 'mission': am.get('my_mission_points'),
               'first': first, 'final': final, 'retried': retry,
               'decision': (parsed or {}).get('decision'), 'math': m,
               'uniform_chance': round(1 / am['combinations_left'], 4) if am.get('combinations_left') else None,
               'output_tokens': (usage or {}).get('output')}
        with lock:
            rows.append(row)

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(lambda a: one(*a), enumerate(items)))
    rows.sort(key=lambda r: r['i'])
    (out_dir / f'{model}.jsonl').write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n', encoding='utf-8')
    return rows


def summary(model, rows):
    n = len(rows)
    first_ok = sum(r['first'] == 'ok' for r in rows)
    final_ok = sum(r['final'] == 'ok' for r in rows)
    acc = [r for r in rows if r['decision'] == 'accuse']
    ev_follow = sum(1 for r in rows if r['final'] == 'ok' and (
        (r['decision'] == 'accuse') == (r['math'].get('expected_if_accuse', 0) > r['math'].get('expected_if_pass', 0))))
    errors = {}
    for r in rows:
        if r['final'] != 'ok':
            errors[r['final']] = errors.get(r['final'], 0) + 1
    chance_gap = [abs(r['math'].get('my_win_chance', 0) - (r['uniform_chance'] or 0)) for r in rows
                  if r['final'] == 'ok' and r['uniform_chance'] is not None]
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r['kind'], [0, 0, 0])
        k[0] += 1
        k[1] += r['final'] == 'ok'
        k[2] += r['decision'] == 'accuse'
    tok = [r['output_tokens'] for r in rows if r['output_tokens']]
    print(f"{model}: first-try ok {first_ok}/{n}, after one retry ok {final_ok}/{n}, remaining errors {errors}")
    print(f"  accuse {len(acc)}/{n}; decision matches its own higher expected score {ev_follow}/{final_ok}; "
          f"median |own chance - uniform| {sorted(chance_gap)[len(chance_gap) // 2] if chance_gap else None}; "
          f"median output tokens {sorted(tok)[len(tok) // 2] if tok else None}")
    print(f"  by kind (n, ok, accuse): {by_kind}")


def main(argv):
    if len(argv) < 3:
        raise SystemExit(__doc__)
    camp, out_dir, models = Path(argv[0]), Path(argv[1]), argv[2:]
    out_dir.mkdir(parents=True, exist_ok=True)
    items = sample(camp)
    print(f"prompts: {len(items)} ({', '.join(f'{k} {sum(i['kind'] == k for i in items)}' for k in ('last_no_mission', 'last_mission', 'mid'))})")
    exe = find_codex()
    for model in models:
        summary(model, run(model, items, out_dir, exe))


if __name__ == '__main__':
    main(sys.argv[1:])
