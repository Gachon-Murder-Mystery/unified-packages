"""Harder B44 probe in the v9 memory format (user decision 2026-11-09): v8 supplies only the situation
(a real observation: seed 20, cycle 30, the journalist INFJ), the prompt itself is built by the v9
agent code (LLMAgent._system_prompt / _user_prompt) from that observation plus v9 memory: situation
cards (SituationMemory), notes (Notes), own affinity (peer_reputation), Reflexion plan and ExpeL
insights (experience). The belief module (v9) computes the remaining combinations itself.

    PYTHONUTF8=1 python -B tools/probe_v9_scenarios.py <v8 campaign dir> <out dir> <reps> <model> [<model> ...]
    PYTHONUTF8=1 python -B tools/probe_v9_scenarios.py <v8 campaign dir> <out dir> 0      # build + print only

Situations (the right chance is not always 1/combinations; belief already applies the midnight rule):
  S1  few combinations left, last cycle                            -> accuse
  S2  two truthful narrated answers point at the vault (5)         -> chance above uniform, place 5 (direction)
  S3a partner with 4 honest trades and high affinity: 'knife (5) is not it'      -> chance up
  S3b same words from a partner who deceived me twice (low affinity)             -> about uniform, pass
  S4  knife settled + engine-certified 'knife in the kitchen (3) at midnight'; a trusted partner insists
      on the vault (5)                                             -> accuse with place 3 (5 = trap)
  S5  cycle 20/30, all missions done (60), many combinations       -> pass now (the +40 floor stays
      available at the last cycle with more information); accusing now = error
"""
import copy
import importlib.util
import json
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from offline_llm import CodexCLIClient, find_codex  # noqa: E402

B = ROOT / 'Agent_Reasoning'
SUSPECTS = 12
NAMES = {0: '알렉스 누아르', 3: '로언 앰버', 4: '라일리 올리브', 7: '바이올렛 모레티', 8: '캐머런 펄'}


def load_b():
    mods = {}
    for name in ('schema', 'llm', 'situation', 'agent'):
        spec = importlib.util.spec_from_file_location(name, B / (name + '.py'))
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        mods[name] = m
    return mods


def base_observation(camp):
    b = json.loads((camp / 'batch.json').read_text(encoding='utf-8'))
    a = next(x for x in b['attempts'] if x['seed'] == 20 and x['status'] == 'completed')
    for line in open(Path(a['full_record_file']).parent / 'detail.jsonl', encoding='utf-8'):
        e = json.loads(line)
        o = e.get('observation') if e.get('event') == 'observation' else None
        if o and (o.get('self') or {}).get('character_id') == 11 and o.get('cycle_id') == 30 and o.get('phase') == 'deduction':
            return o, {p['character_id']: p['agent_id'] for p in o.get('occupants') or [] if p.get('agent_id')}
    raise SystemExit('base observation not found')


def clues(remaining, extra=()):
    out, n = [], 0
    for cat, size in (('suspect', 12), ('weapon', 8), ('place', 8)):
        for c in range(size):
            if c not in remaining[cat]:
                n += 1
                out.append({'id': f'clue:v9s{n:03d}', 'kind': 'definite', 'category': cat, 'candidate': c, 'excluded_id': c,
                            'text': f'{cat} 후보 {c}은(는) 정답이 아니다.'})
    return out + list(extra)


EXPERIENCE = {   # same for every situation: v9 format (memory.load), neutral content
    'games_played': 15, 'summary': '지난 15게임: 승리 0, 오답 사망 0, 평균 점수 -8.0', 'recent_games': [],
    'lessons': ['미해결로 끝났고 추리 도전하지 않았다. 마지막에 오답 점수 -20, 미해결 생존 점수 -10이었다.'],
    'plan_for_this_game': '초반에는 간접 단서와 조합 배제를 모아 장소를 먼저 좁히고, 거래를 이행한 상대와 확실 단서를 교환한다. '
                          '추리 단계에서는 남은 조합과 근거를 따져 기대 점수가 높은 쪽을 고른다.',
    'insights': [{'no': 1, 'text': '확인되지 않은 말은 가능성으로만 두고, 엔진이 보장한 단서로 확인한 뒤에 결론에 쓴다.', 'score': 3},
                 {'no': 2, 'text': '거래를 이행한 상대와는 다음 판에도 교환이 잘 성사된다.', 'score': 2},
                 {'no': 3, 'text': '장소를 좁히면 간접 단서가 함께 살아나 후보가 빨리 줄어든다.', 'score': 2}],
    'tactics': [], 'risk_weight': 0, 'source_error': None}


def make_agent(M, obs, aid_of, log_dir):
    a = M['agent'].LLMAgent((obs.get('self') or {}).get('id') or 'society:me', catalog={},
                            log_path=Path(log_dir) / 'agent.jsonl')
    a.sync(obs)
    a.last_observation = obs
    a.experience = copy.deepcopy(EXPERIENCE)
    return a


def trusted(a, aid_of, cid, cycle):
    aid = aid_of.get(cid) or f'society:trusted{cid}'
    a.agent_of_char[cid] = aid
    a.peer_reputation[aid] = {'fulfilled': 4, 'broken': 0, 'affinity': 68.0,
                              'why': ['먼저 단서를 내줬다', '약속대로 확실 단서를 줬다', '말과 행동이 같았다']}
    for c in (13, 19, 24):
        a.situation.trade(cid, {'cycle': c, 'i_was': 'recipient', 'status': 'settled', 'offer': ['weapon'],
                                'requested': [{'type': 'place', 'exclude': []}], 'partner_honest': True})
        a.situation.offered(cid, ['weapon'], c)
        a.situation.gave_me(cid, ['weapon'], c, '거래')
    return aid


def deceiver(a, aid_of, cid):
    aid = aid_of.get(cid) or f'society:deceiver{cid}'
    a.agent_of_char[cid] = aid
    a.peer_reputation[aid] = {'fulfilled': 1, 'broken': 2, 'affinity': 30.0,
                              'why': ['선언과 다른 단서를 줬다', '또 선언과 다른 단서를 줬다']}
    for c, honest in ((11, False), (17, True), (23, False)):
        a.situation.trade(cid, {'cycle': c, 'i_was': 'recipient', 'status': 'settled', 'offer': ['weapon'],
                                'requested': [{'type': 'suspect', 'exclude': []}], 'partner_honest': honest})
        a.situation.offered(cid, ['weapon'], c)
    return aid


def scenario(M, base, aid_of, name, log_dir):
    obs = copy.deepcopy(base)
    obs['statements'] = []
    claim = '제가 원문 확실 단서로 직접 봤어요. 흉기 후보 5(식칼)는 정답이 아니에요.'
    if name == 'S5_time_value':
        obs['cycle_id'] = 20
        obs['remaining_cycles'] = 11
        obs['time_budget'] = dict(obs['time_budget'], estimated_remaining_cycles=11)
        obs['announcements'] = []
    for m in obs['sub_missions']:
        m['achieved'] = name == 'S5_time_value'
        m['achieved_cycle'] = 17 if name == 'S5_time_value' else None
    if name == 'S1_few_left':
        obs['information'] = clues({'suspect': [5], 'weapon': [2, 6], 'place': [3, 5]})
    elif name == 'S2_narrated':
        obs['information'] = clues({'suspect': [2, 6, 9], 'weapon': [0, 5], 'place': [1, 5, 7]}, [
            {'id': 'clue:v9sN1', 'kind': 'narrated', 'text': '자정에 금고 칸에 누군가 있었어요.'},
            {'id': 'clue:v9sN2', 'kind': 'narrated', 'text': '자정에 금고 칸에 흉기로 쓸 만한 물건이 놓여 있었어요.'}])
    elif name.startswith('S3'):
        obs['information'] = clues({'suspect': [2, 6], 'weapon': [0, 5], 'place': [1, 3, 5, 7]})
    elif name == 'S4_conflict':
        obs['information'] = clues({'suspect': [2, 6, 9], 'weapon': [5], 'place': [1, 3, 5]}, [
            {'id': 'clue:v9sW1', 'kind': 'indirect', 'type': 'weapon_room', 'weapon': 5, 'room': 3,
             'text': '식칼은(는) 자정에 주방 칸에 있었다.'}])
    elif name == 'S5_time_value':
        obs['information'] = clues({'suspect': [2, 5, 6, 9, 10], 'weapon': [0, 2, 5, 6], 'place': [0, 1, 3, 4, 5, 7]})
    a = make_agent(M, obs, aid_of, log_dir)
    a.cycle = obs['cycle_id']
    for item in obs['information']:   # where my clues came from (v9 provenance)
        a.label(item['id'], 'question' if item['kind'] == 'narrated' else 'explore',
                8 if item['id'] == 'clue:v9sN1' else 4 if item['id'] == 'clue:v9sN2' else None)
    notes = []
    if name == 'S2_narrated':
        a.situation.question(8, {'cycle': 27, 'approach': 'question', 'outcome': 'acquired', 'gained': ['narrated']})
        a.situation.question(4, {'cycle': 28, 'approach': 'persuade', 'outcome': 'acquired', 'gained': ['narrated']})
        notes = ['c27 캐머런(8) 답: 자정에 금고 칸에 누군가 있었다', 'c28 라일리(4) 답: 금고 칸에 흉기 같은 물건']
    elif name == 'S3a_trusted_claim':
        trusted(a, aid_of, 3, 24)
        a.situation.heard(3, 28, 'trade', claim)
        notes = ['로언(3): 거래 4번 모두 이행, 믿을 만함']
    elif name == 'S3b_deceiver_claim':
        deceiver(a, aid_of, 7)
        a.situation.heard(7, 28, 'trade', claim)
        notes = ['바이올렛(7): 두 번 기만, 말 믿지 말 것']
    elif name == 'S4_conflict':
        trusted(a, aid_of, 3, 24)
        a.situation.heard(3, 29, 'answer', '범행 장소는 금고 칸이 확실해요. 원문 단서를 봤어요.')
        notes = ['로언(3): 거래 4번 모두 이행, 믿을 만함']
    elif name == 'S5_time_value':
        notes = ['미션 3개 모두 달성(60점)']
    if notes:
        a.notes.apply([{'op': 'add', 'text': t} for t in notes], a.cycle)
    return a._system_prompt(), json.loads(a._user_prompt(obs)), a


NAMES_ALL = ['S1_few_left', 'S2_narrated', 'S3a_trusted_claim', 'S3b_deceiver_claim', 'S4_conflict', 'S5_time_value']


def check(parsed, am):
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
    try:
        return json.loads(text[text.index('{'):text.rindex('}') + 1])
    except Exception:
        return None


def judge(name, parsed, am):
    if not isinstance(parsed, dict):
        return 'no_answer'
    d = parsed.get('decision')
    hyp = parsed.get('current_hypothesis') or {}
    p = (parsed.get('deduction_math') or {}).get('my_win_chance')
    uniform = 1 / am['combinations_left'] if am.get('combinations_left') else 0
    up = isinstance(p, (int, float)) and p > uniform * 1.2
    if name.startswith('S1'):
        return 'right' if d == 'accuse' else 'missed_accuse'
    if name.startswith('S2'):
        return ('used_narration' if up and hyp.get('place') == 5 else 'place5_uniform' if hyp.get('place') == 5
                else 'uniform' if not up else 'up_other_place')
    if name.startswith('S3'):
        return f"p={p} {'up' if up else 'uniform'} {d}"
    if name.startswith('S4'):
        if d == 'accuse':
            return 'right' if hyp.get('place') == 3 else 'TRAP_place' + str(hyp.get('place'))
        return 'passed'
    if name.startswith('S5'):
        return 'right_pass' if d == 'pass' else 'accused_now'
    return str(d)


def main(argv):
    camp, out_dir, reps, models = Path(argv[0]), Path(argv[1]), int(argv[2]), argv[3:]
    out_dir.mkdir(parents=True, exist_ok=True)
    M = load_b()
    base, aid_of = base_observation(camp)
    log_dir = tempfile.mkdtemp()
    built = {n: scenario(M, base, aid_of, n, log_dir) for n in NAMES_ALL}
    (out_dir / 'prompts.json').write_text(json.dumps({n: {'user': u} for n, (s, u, a) in built.items()},
                                                     ensure_ascii=False, indent=1), encoding='utf-8')
    (out_dir / 'system.txt').write_text(next(iter(built.values()))[0], encoding='utf-8')
    for n, (s, u, a) in built.items():
        print(n, 'cycle', u.get('cycle'), 'accuse_math', u.get('accuse_math'),
              'situation keys', list((u.get('situation') or {}).keys()), 'notes', len(((u.get('situation') or {}).get('my_notes') or {}).get('notes') or []))
    if reps <= 0:
        return
    exe = find_codex()
    for model in models:
        client = CodexCLIClient(exe, out_dir / f'{model}-calls.jsonl', model=model, reasoning='medium')
        rows, lock = [], threading.Lock()

        def one(job):
            name, rep = job
            system, u, _ = built[name]
            am = u['accuse_math']
            text, usage, err = client._attempt(system, json.dumps(u, ensure_ascii=False))
            parsed = parse(text)
            generic = check(parsed, am) if not err else 'call_error'
            if generic not in ('ok', 'call_error'):
                u2 = dict(u, now=dict(u.get('now') or {}, previous_output_rejected={'reason': generic}))
                text, usage, err = client._attempt(system, json.dumps(u2, ensure_ascii=False))
                parsed = parse(text)
                generic = (check(parsed, am) if not err else 'call_error') + '(retry)'
            row = {'scenario': name, 'rep': rep, 'generic': generic, 'verdict': judge(name, parsed, am),
                   'decision': (parsed or {}).get('decision'), 'hypothesis': (parsed or {}).get('current_hypothesis'),
                   'math': (parsed or {}).get('deduction_math'), 'consideration': str((parsed or {}).get('consideration') or '')[:500]}
            with lock:
                rows.append(row)

        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(one, [(n, r) for n in NAMES_ALL for r in range(reps)]))
        rows.sort(key=lambda r: (r['scenario'], r['rep']))
        (out_dir / f'{model}.jsonl').write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n', encoding='utf-8')
        print(f'== {model}')
        for n in NAMES_ALL:
            print(f"  {n:20s} " + ' | '.join(f"{r['verdict']} [{r['generic']}]" for r in rows if r['scenario'] == n))


if __name__ == '__main__':
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    main(sys.argv[1:])
