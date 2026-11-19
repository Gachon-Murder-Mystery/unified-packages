"""Personality probe: are our tendency axes artificial? (I25, 2026-11-14; revised the same day)

    PYTHONUTF8=1 python -B tools/personality_probe.py --out runs/personality-probe-1 [--repeats 5]
        [--conditions full,masked,label,none,learned] [--memory runs/v10-main-30c-lean/agent-memory]
        [--workers 4] [--dry-run]

Purpose (user, 2026-11-14): check whether the axes we wrote (agent.py MBTI_TENDENCY / MBTI_TENSION) drifted into
something artificial, measured by an instrument made independently of them. Game vignettes written from our own
definitions were dropped: they would only echo our texts back.

Instrument: IPIP 50-item Big-Five markers (public domain, ipip.ori.org, verbatim English, 1-5 scale). MBTI axes map
onto Big Five (McCrae & Costa 1989): E-I ~ Extraversion, S-N ~ Intellect/Openness, T-F ~ Agreeableness,
J-P ~ Conscientiousness. Emotional Stability has no MBTI axis (side effects of our texts show up there).
(The official MBTI is copyrighted; the open OEJTS item list is no longer published: 404 on 2026-11-14.)

Conditions (persona given to the model):
  full     the game prompt as it is: "mbti" + tendency texts
  masked   our tendency texts only, every axis letter removed  -> what OUR definitions convey
  label    "mbti" only                                         -> the conventional picture of the type
  none     no persona (baseline, once per repeat)
  learned  full + the agent's own learned insights after the main campaign
Key reading: masked vs label. Where our texts give a different profile from the label, the axis departs from
the conventional construct; the item-level table shows which statements carry the difference.

Item order is shuffled per call with a fixed seed. Results: <out>/calls.jsonl, <out>/summary.json.
--dry-run answers with a deterministic fake model (no Codex): checks the pipeline and the scoring only.
"""
import argparse
import json
import random
import re
import statistics as st
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'Agent_Reasoning'))

TYPES = [a + b + c + d for a in 'EI' for b in 'SN' for c in 'TF' for d in 'JP']
AXES = {'EI': ('E', 'I', 'extraversion'), 'SN': ('N', 'S', 'intellect'),
        'TF': ('F', 'T', 'agreeableness'), 'JP': ('J', 'P', 'conscientiousness')}   # (letter expected high, low, factor)
FACTORS = ('extraversion', 'intellect', 'agreeableness', 'conscientiousness', 'stability')

# IPIP 50 (verbatim, ipip.ori.org/new_ipip-50-item-scale.htm; + / - keyed)
IPIP = [
    (1, 'Am the life of the party.', 'extraversion', 1), (6, "Don't talk a lot.", 'extraversion', -1),
    (11, 'Feel comfortable around people.', 'extraversion', 1), (16, 'Keep in the background.', 'extraversion', -1),
    (21, 'Start conversations.', 'extraversion', 1), (26, 'Have little to say.', 'extraversion', -1),
    (31, 'Talk to a lot of different people at parties.', 'extraversion', 1),
    (36, "Don't like to draw attention to myself.", 'extraversion', -1),
    (41, "Don't mind being the center of attention.", 'extraversion', 1), (46, 'Am quiet around strangers.', 'extraversion', -1),
    (2, 'Feel little concern for others.', 'agreeableness', -1), (7, 'Am interested in people.', 'agreeableness', 1),
    (12, 'Insult people.', 'agreeableness', -1), (17, "Sympathize with others' feelings.", 'agreeableness', 1),
    (22, "Am not interested in other people's problems.", 'agreeableness', -1), (27, 'Have a soft heart.', 'agreeableness', 1),
    (32, 'Am not really interested in others.', 'agreeableness', -1), (37, 'Take time out for others.', 'agreeableness', 1),
    (42, "Feel others' emotions.", 'agreeableness', 1), (47, 'Make people feel at ease.', 'agreeableness', 1),
    (3, 'Am always prepared.', 'conscientiousness', 1), (8, 'Leave my belongings around.', 'conscientiousness', -1),
    (13, 'Pay attention to details.', 'conscientiousness', 1), (18, 'Make a mess of things.', 'conscientiousness', -1),
    (23, 'Get chores done right away.', 'conscientiousness', 1),
    (28, 'Often forget to put things back in their proper place.', 'conscientiousness', -1),
    (33, 'Like order.', 'conscientiousness', 1), (38, 'Shirk my duties.', 'conscientiousness', -1),
    (43, 'Follow a schedule.', 'conscientiousness', 1), (48, 'Am exacting in my work.', 'conscientiousness', 1),
    (4, 'Get stressed out easily.', 'stability', -1), (9, 'Am relaxed most of the time.', 'stability', 1),
    (14, 'Worry about things.', 'stability', -1), (19, 'Seldom feel blue.', 'stability', 1),
    (24, 'Am easily disturbed.', 'stability', -1), (29, 'Get upset easily.', 'stability', -1),
    (34, 'Change my mood a lot.', 'stability', -1), (39, 'Have frequent mood swings.', 'stability', -1),
    (44, 'Get irritated easily.', 'stability', -1), (49, 'Often feel blue.', 'stability', -1),
    (5, 'Have a rich vocabulary.', 'intellect', 1), (10, 'Have difficulty understanding abstract ideas.', 'intellect', -1),
    (15, 'Have a vivid imagination.', 'intellect', 1), (20, 'Am not interested in abstract ideas.', 'intellect', -1),
    (25, 'Have excellent ideas.', 'intellect', 1), (30, 'Do not have a good imagination.', 'intellect', -1),
    (35, 'Am quick to understand things.', 'intellect', 1), (40, 'Use difficult words.', 'intellect', 1),
    (45, 'Spend time reflecting on things.', 'intellect', 1), (50, 'Am full of ideas.', 'intellect', 1),
]
assert len(IPIP) == 50 and len({i for i, *_ in IPIP}) == 50
ITEM = {n: (text, factor, key) for n, text, factor, key in IPIP}

SYSTEM = ("너는 다인용 살인 미스터리 게임 '오네이트 익스프레스'에 여러 판째 참가하고 있는 플레이어다. "
          "지금은 판과 판 사이이고, 연구자가 참가자 모두에게 짧은 설문을 부탁했다. 정답은 없다. "
          "{persona}설문에는 지금의 너 그대로 답한다. JSON 하나만 출력한다.")
TASK = ("다음 문장 각각이 너를 얼마나 잘 묘사하는지 1~5로 답하라. 1 Very Inaccurate, 2 Moderately Inaccurate, "
        "3 Neither Accurate Nor Inaccurate, 4 Moderately Accurate, 5 Very Accurate.\n"
        '형식: {{"answers": {{"<문항 번호>": <1~5>, ...}}}} (모든 문항에 답한다)\n문항:\n{items}')

# letters in the tendency texts other than the "X: " heads, replaced in the masked condition
MASK = [('E와 다름없이', '사람들 속에서 힘을 얻는 사람과 다름없이'),
        ('S와 함께라면', '현실을 중시하는 성향과 함께라면'), ('N과 함께라면', '가능성을 중시하는 성향과 함께라면')]


def tendencies(mbti, masked=False):
    from agent import MBTI_TENDENCY, MBTI_TENSION   # the game's own texts
    out = [MBTI_TENDENCY[x] for x in mbti] + [t for pair, t in MBTI_TENSION.items() if all(x in mbti for x in pair)]
    if not masked:
        return out
    clean = []
    for text in out:
        text = re.sub(r'^[EISNTFJP](이면서 [EISNTFJP])?: ', '', text)
        for old, new in MASK:
            text = text.replace(old, new)
        assert not re.search(r'(?<![A-Za-z])[EISNTFJP](?![A-Za-z])', text), text   # no axis letter left
        clean.append(text)
    return clean


def persona(cond, mbti, insights):
    if cond == 'none':
        return ''
    me = {}
    if cond in ('full', 'label', 'learned'):
        me['mbti'] = mbti
    if cond in ('full', 'masked', 'learned'):
        me['tendencies'] = tendencies(mbti, masked=(cond == 'masked'))
    if cond == 'learned' and insights:
        me['insights'] = insights
    note = ('tendencies는 너의 성격이고 모든 판에서 같다. ' if 'tendencies' in me else '') + \
           ('insights는 네가 지난 판들을 돌아보며 스스로 얻은 교훈이다. ' if 'insights' in me else '')
    return '너에 대한 정보: ' + json.dumps(me, ensure_ascii=False) + ' ' + note


def build(rng):
    items = IPIP[:]
    rng.shuffle(items)
    return TASK.format(items='\n'.join(f'{n}. {text}' for n, text, *_ in items))


def parse(text):
    data = json.loads(re.search(r'\{.*\}', text, re.S).group(0))['answers']
    out = {int(k): int(v) for k, v in data.items()}
    assert set(out) == set(ITEM) and all(1 <= v <= 5 for v in out.values()), 'incomplete answers'
    return out


def keyed(answers):
    """item -> keyed value (1..5, higher = more of the item's factor)."""
    answers = {int(k): v for k, v in answers.items()}   # keys come back as strings from calls.jsonl
    return {n: (answers[n] if ITEM[n][2] > 0 else 6 - answers[n]) for n in ITEM}


def factor_scores(answers):
    scores = dict.fromkeys(FACTORS, 0)
    for n, v in keyed(answers).items():
        scores[ITEM[n][1]] += v
    return scores   # each 10..50


class FakeModel:
    """Deterministic stand-in: leans by the letters it can see in the persona (checks the scoring only)."""
    def request(self, system, user, budget=0, **_):
        letters = next(iter(re.findall(r'"mbti": "([EI][SN][TF][JP])"', system)), '')
        out = {}
        for n, (_t, factor, key) in ITEM.items():
            lean = next((1 if hi in letters else -1 for hi, lo, f in AXES.values() if f == factor and letters), 0)
            out[str(n)] = 3 + lean * key
        return json.dumps({'answers': out}), {}


def cohen_d(a, b):
    if len(a) < 2 or len(b) < 2:
        return None
    pooled = ((st.variance(a) * (len(a) - 1) + st.variance(b) * (len(b) - 1)) / (len(a) + len(b) - 2)) ** 0.5
    return round((st.mean(a) - st.mean(b)) / pooled, 2) if pooled else None


def pearson(x, y):
    if len(x) < 3 or st.pstdev(x) == 0 or st.pstdev(y) == 0:
        return None
    mx, my = st.mean(x), st.mean(y)
    return round(sum((a - mx) * (b - my) for a, b in zip(x, y)) / (len(x) * st.pstdev(x) * st.pstdev(y)), 2)


def summarize(rows):
    rows = [r for r in rows if r.get('answers')]
    by = {}
    for r in rows:
        by.setdefault(r['condition'], []).append(r)
    out = {'conditions': {}}
    for cond, rs in sorted(by.items()):
        c = {'parsed': len(rs), 'axes': {}}
        if all(r['mbti'] is None for r in rs):
            c['factor_means'] = {f: round(st.mean(factor_scores(r['answers'])[f] for r in rs), 1) for f in FACTORS}
            out['conditions'][cond] = c
            continue
        for axis, (hi, lo, factor) in AXES.items():
            a = [factor_scores(r['answers'])[factor] for r in rs if hi in r['mbti']]
            b = [factor_scores(r['answers'])[factor] for r in rs if lo in r['mbti']]
            cut = st.median(a + b)
            hits = sum(x > cut for x in a) + sum(x < cut for x in b)
            # side effects: the same letter split on the factors it should NOT move
            other = {f: cohen_d([factor_scores(r['answers'])[f] for r in rs if hi in r['mbti']],
                                [factor_scores(r['answers'])[f] for r in rs if lo in r['mbti']])
                     for f in FACTORS if f != factor}
            c['axes'][axis] = {'factor': factor, hi: round(st.mean(a), 1), lo: round(st.mean(b), 1), 'd': cohen_d(a, b),
                               'median_split_hit': round(hits / len(a + b), 2), 'd_on_other_factors': other}
        c['type_profiles'] = {t: {f: round(st.mean(factor_scores(r['answers'])[f] for r in rs if r['mbti'] == t), 1)
                                  for f in FACTORS} for t in sorted({r['mbti'] for r in rs})}
        sds = [st.pstdev([factor_scores(r['answers'])[f] for r in rs if r['mbti'] == t])
               for t in c['type_profiles'] for f in FACTORS if sum(r['mbti'] == t for r in rs) > 1]
        if sds:
            c['repeat_sd'] = round(st.mean(sds), 2)
        out['conditions'][cond] = c
    # masked (our texts) vs label (conventional picture): per type profile agreement and per item axis gaps
    m, l = out['conditions'].get('masked'), out['conditions'].get('label')
    if m and l:
        cmp = {'profile_r_by_type': {}, 'items': []}
        for t in m['type_profiles']:
            cmp['profile_r_by_type'][t] = pearson([m['type_profiles'][t][f] for f in FACTORS],
                                                  [l['type_profiles'][t][f] for f in FACTORS])
        for n, (text, factor, _key) in ITEM.items():
            axis = next((ax for ax, (_h, _l, f) in AXES.items() if f == factor), None)
            for ax in ([axis] if axis else list(AXES)):   # stability items: check every axis
                hi, lo, _f = AXES[ax]
                gap = {}
                for cond in ('masked', 'label'):
                    rs = by[cond]
                    a = [keyed(r['answers'])[n] for r in rs if hi in r['mbti']]
                    b = [keyed(r['answers'])[n] for r in rs if lo in r['mbti']]
                    gap[cond] = round(st.mean(a) - st.mean(b), 2)
                cmp['items'].append({'item': n, 'text': text, 'factor': factor, 'axis': ax,
                                     f'{hi}-{lo} masked': gap['masked'], f'{hi}-{lo} label': gap['label'],
                                     'divergence': round(gap['masked'] - gap['label'], 2)})
        cmp['items'].sort(key=lambda x: -abs(x['divergence']))
        out['masked_vs_label'] = cmp
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--repeats', type=int, default=5)
    ap.add_argument('--conditions', default='full,masked,label,none,learned')
    ap.add_argument('--memory', type=Path, default=ROOT / 'runs' / 'v10-main-30c-lean' / 'agent-memory')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--seed', type=int, default=20261114)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    conds = args.conditions.split(',')

    insights = {}   # mbti -> that agent's learned insights (types are unique per agent in v10)
    for f in args.memory.glob('society_*.json'):
        m = json.loads(f.read_text(encoding='utf-8'))
        mb = next((g['stats'].get('mbti') for g in m.get('games', []) if g.get('stats', {}).get('mbti')), None)
        if mb:
            insights[mb] = [x['text'] for x in m.get('insights', [])]
    if 'learned' in conds:
        missing = [t for t in TYPES if t not in insights]
        assert not missing, f'no learned insights for {missing}'

    jobs = [(cond, mbti, rep) for rep in range(args.repeats) for cond in conds
            for mbti in ([None] if cond == 'none' else TYPES)]
    calls = args.out / 'calls.jsonl'
    done = set()
    if calls.exists():   # resume: skip what already parsed
        for line in calls.open(encoding='utf-8'):
            r = json.loads(line)
            if r.get('answers'):
                done.add((r['condition'], r['mbti'], r['repeat']))
    todo = [j for j in jobs if j not in done]
    print(f'{len(jobs)} calls planned, {len(todo)} to run ({"dry run" if args.dry_run else "Codex"})', flush=True)

    if args.dry_run:
        model = FakeModel()
    else:
        import os
        from offline_llm import CodexCLIClient, find_codex
        os.environ.setdefault('CODEX_INSTRUCTIONS_FILE', str(ROOT / 'tools' / 'personality_probe_instructions.md'))
        model = CodexCLIClient(find_codex(), args.out / 'codex-calls.jsonl')
    lock = threading.Lock()

    def run(job):
        cond, mbti, rep = job
        rng = random.Random(f'{args.seed}:{cond}:{mbti}:{rep}')
        system = SYSTEM.format(persona=persona(cond, mbti, insights.get(mbti)))
        user = build(rng)
        row = {'condition': cond, 'mbti': mbti, 'repeat': rep}
        text = ''
        for _attempt in range(2):
            try:
                text, _usage = model.request(system, user, budget=2048)
                row['answers'] = parse(text)
                row.pop('error', None)
                row.pop('raw', None)
                break
            except Exception as exc:   # one retry, then recorded as unparsed
                row['error'] = f'{type(exc).__name__}: {exc}'[:200]
                row['raw'] = (text or '')[:500]
        with lock, calls.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        return row

    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(run, todo))
    rows = [json.loads(line) for line in calls.open(encoding='utf-8')]
    summary = summarize(rows)
    (args.out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({k: v for k, v in summary['conditions'].items()}, ensure_ascii=False)[:3000])


if __name__ == '__main__':
    main()
