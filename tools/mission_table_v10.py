"""Write docs/미션_개편_v10.md: every mission's v9 and v10 condition with the recorded basis (read-only).

Basis: runs/mission_audit.json (67 recorded 30-cycle games, achievement when assigned) and
runs/mission_calibration.json (336 agent-games of gpt-6.1-terra: how often the counted behaviour reached the v10
quantity by the deadline, and the median cycle; most of those agents did not hold the mission, so a holder that
works for it is usually faster).
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V9 = ROOT.parent / 'unified-packages-v9' / 'Game-engine' / 'competitive_catalog.json'
V10 = ROOT / 'Game-engine' / 'competitive_catalog.json'
AUDIT = json.loads((ROOT / 'runs' / 'mission_audit.json').read_text(encoding='utf-8'))['missions']
CAL = json.loads((ROOT / 'runs' / 'mission_calibration.json').read_text(encoding='utf-8'))['metrics']
# v10 mission -> (calibration metric, quantity)
METRIC = {'M02': ('interactions_in_room_2', 14), 'M03': ('time_in_room_3', 12), 'M04': ('time_in_room_4', 12),
          'M06': ('time_in_room_1', 12), 'M10': ('interactions_in_crowd4', 9), 'M11': ('interactions_alone', 7),
          'M28': ('questions_same_target_max', 6), 'M29': ('question_success_distinct_targets', 8),
          'M30': ('success_interrogate', 5), 'M31': ('success_threaten', 3), 'M32': ('explore_successes', 10),
          'M33': ('explore_success_distinct_rooms', 6), 'M34': ('question_2plus', 5), 'M36': ('trades_both_kept', 7),
          'M38': ('trade_distinct_partners', 4), 'M39': ('trades_partner_sent_first', 4), 'M41': ('indirect_acquired', 8),
          'M43': ('definite_given_in_answers', 4), 'M45': ('time_in_room_5', 15), 'M48': ('questions_received', 10),
          'M50': ('time_in_room_7', 12), 'M51': ('definite_acquired', 12), 'M53': ('clues_received_by_trade', 9),
          'M56': ('trades_i_kept', 8), 'O1': ('explore_successes_room_5', 3), 'M05': ('explore_successes_room_6', 4)}


def cal(mid):
    if mid not in METRIC:
        return ''
    metric, n = METRIC[mid]
    row = next((r for r in CAL.get(metric, []) if r['N'] == n), None)
    return f"{row['rate']:.0%} · {row['median_cycle']:g}" if row else '0% (기록 없음)'


def main():
    old = {m['id']: m for m in json.loads(V9.read_text(encoding='utf-8'))['missions']}
    new = json.loads(V10.read_text(encoding='utf-8'))['missions']
    lines = ['# 개인 미션 개편 (v10)', '',
             '작성: 2026-11-12. 생성: `tools/mission_table_v10.py`. 근거: `docs/미션_점검_v8_v9.md`, `tools/mission_calibrate.py`. 변경 기록: A53.', '',
             '- 점수: 모든 미션 20점. 하·중·상은 GM이 고르는 세 자리일 뿐이다(예상 달성률 순으로 셋).',
             '- 기준: 보통 25사이클 무렵 달성. 남의 이동에 달린 "연속·혼자" 조건은 "총 N"으로 바꿨다.',
             '- 쉬워짐·어려워짐: 직업과 능력치만. 성격 글자는 뺐다(A51).',
             '- 기록 달성: v8~v9 67판에서 배정됐을 때 달성/배정과 달성 사이클 중앙값(옛 조건).',
             '- 새 수량 기준선: gpt-6.1-terra 336 에이전트-판(대부분 미션 비보유)이 새 수량에 닿은 비율 · 중앙 사이클. '
             '미션을 가진 에이전트는 대개 더 빠르다. 비율이 0%인 칸 체류·칸별 탐색은 보유자만 그 칸에 머물렀기 때문이다.', '',
             '| ID | 이름 | 자리 | v9 조건 | v10 조건 | 기록 달성 (옛 조건) | 새 수량 기준선 |', '|---|---|---|---|---|---|---|']
    retired, interference = [], []
    for m in new:
        mid = m['id']
        a = AUDIT.get(mid)
        rec = f"{a['achieved']}/{a['assigned']} · {a['cycle_median'] if a['cycle_median'] is not None else '-'}" if a else '배정 없음'
        if m.get('retired'):
            retired.append(f"| {mid} | {old.get(mid, m)['name']} | {old.get(mid, m)['condition']} | {m.get('retired_reason', '')} | {rec} |")
            continue
        if m.get('category') == 'interference':
            interference.append(f"| {mid} | {m['name']} | {m['condition']} |")
            continue
        o = old.get(mid, {})
        changed = '' if o.get('condition') == m['condition'] else o.get('condition', '')
        lines.append(f"| {mid} | {m['name']} | {m['base']}{' · 판 끝 판정' if m.get('judged') == 'end' else ''} | {changed or '(같음)'} | "
                     f"{m['condition']} | {rec} | {cal(mid)} |")
    lines += ['', '## 방해형 미션 (새로 추가, 판 끝 판정)', '',
              '판마다 참가자의 약 1/3(12명이면 4명)이 하나를 받는다(하 자리 대체). 대상만 알려 주고, I1의 대상 미션은 알려 주지 않는다. '
              '판이 끝나면 GM이 방해형 미션 달성 수만 익명으로 알린다(A54). "들킴"은 속은 상대가 반응으로 정한 판 관계값으로 잰다(A52).', '',
              '| ID | 이름 | 조건 |', '|---|---|---|'] + interference
    lines += ['', '## 은퇴한 미션', '', '| ID | 이름 | v9 조건 | 이유 | 기록 달성 |', '|---|---|---|---|---|'] + retired
    (ROOT / 'docs' / '미션_개편_v10.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(len(lines), 'lines')


if __name__ == '__main__':
    main()
