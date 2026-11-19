"""Rewrite the personal-mission catalog for v10 (A53). Writes the same mission list into the engine catalog
(Game-engine/competitive_catalog.json) and the GM copy (GM_Agent_Society/v12_data.json).

User decisions 2026-11-12:
- every mission is worth 20 points; tiers stay only as the GM's three pick slots (base = expected difficulty);
- a mission is typically completed around cycle 25 of 30 (quantities from tools/mission_calibrate.py and
  the per-mission audit docs/미션_점검_v8_v9.md);
- conditions an agent's own actions build up (totals, not "consecutive"; no "nobody else in the room");
- no personality letter changes a mission's difficulty (job and stat tokens only);
- interference missions (category "interference", deception-capable) go to about a third of the players.

Missions that cannot work in a 30-cycle game or that no agent ever achieved for structural reasons are kept
in the file with "retired": true so old records stay readable; candidates() never offers them.

python -B tools/build_v10_missions.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / 'Game-engine' / 'competitive_catalog.json'
GM = ROOT / 'GM_Agent_Society' / 'v12_data.json'

# id: (name or None to keep, condition, base tier, judged at 'end' or None)
# Baseline / holder estimates are in the docs table (docs/미션_개편_v10.md).
REVISED = {
    'O1': (None, '금고 칸 탐색 성공 3회', '상', None),
    'O4': (None, '서로 다른 캐릭터 4명이 나에게 가진 관계성 65 이상', '중', None),
    'O5': (None, 'GM이 지정한 캐릭터 1명이 나에게 가진 관계성 80 이상', '상', None),
    'O6': (None, 'GM이 지정한 간접 단서 2개를 모두 획득', '상', None),
    'M01': ('기차 일주', '8개 칸 모두에서 탐문·탐색 단계를 1회 이상 보냄', '중', None),
    'M02': (None, '식당 칸에서 탐문·탐색 단계를 총 14회 보냄', '상', None),
    'M03': ('주방 지키기', '주방 칸에 총 12사이클 머무름', '상', None),
    'M04': ('수하물 지키기', '수하물 칸에 총 12사이클 머무름', '상', None),
    'M05': (None, '전망 칸 탐색 성공 4회', '상', None),
    'M06': ('침대 칸 휴식', '침대 칸에 총 12사이클 머무름', '상', None),
    'M07': (None, 'GM이 지정한 캐릭터와 같은 칸에서 사이클 10회를 보냄', '중', None),
    'M08': (None, '마지막 10사이클(21~30사이클) 동안 GM이 지정한 플레이어와 같은 칸에 있지 않음', '중', 'end'),
    'M09': ('출발 칸 귀환', '마지막 8사이클(23~30사이클) 동안 출발 칸을 떠나지 않음', '중', 'end'),
    'M10': (None, '캐릭터가 4명 이상 있는 칸에서 탐문·탐색 단계 9회', '중', None),
    'M11': (None, '다른 캐릭터가 없는 칸에서 탐문·탐색 단계 7회', '상', None),
    'M12': (None, '서로 다른 5명이 나에게 가진 관계성 60 이상', '중', None),
    'M14': (None, 'GM이 지정한 플레이어가 나에게 가진 관계성 70 이상', '중', None),
    'M19': ('인기인', '서로 다른 3명이 나에게 가진 관계성 75 이상', '상', None),
    'M20': (None, '30사이클 동안 협박 0회이고 교섭 성사 3회 이상', '하', 'end'),
    'M21': (None, '30사이클 동안 탐색 0회', '상', 'end'),
    'M23': (None, '30사이클 동안 준비 0회', '하', 'end'),
    'M25': (None, '25사이클 연속, 매 이동 단계마다 다른 칸으로 이동', '중', None),
    'M28': (None, '같은 캐릭터에게 질문 6회', '중', None),
    'M29': (None, '서로 다른 캐릭터 8명에게 질문 계열 성공(1성공 이상)', '상', None),
    'M30': (None, '심문 성공 5회', '상', None),
    'M31': (None, '협박 성공 3회', '상', None),
    'M32': (None, '장소 탐색 성공 10회', '중', None),
    'M33': (None, '서로 다른 6개 칸에서 탐색 성공', '중', None),
    'M34': (None, '2성공 이상인 질문 5회', '중', None),
    'M36': (None, '양측 모두 이행한 교섭 7회', '중', None),
    'M38': (None, '서로 다른 4명과 교섭 성사', '중', None),
    'M39': (None, '상대가 먼저 넘긴 교섭 4회', '중', None),
    'M40': (None, '용의자·흉기·장소 분류의 확실한 단서를 각 3개 이상 보유', '중', None),
    'M41': (None, '간접 단서 8개 획득', '중', None),
    'M43': (None, '질문 응답으로 서로 다른 플레이어 4명에게 확실한 단서를 줌', '상', None),
    'M44': (None, 'GM이 지정한 플레이어에게 질문 계열 행동 6회', '중', None),
    'M45': (None, '금고 칸에 총 15사이클 머무름', '상', None),
    'M46': (None, 'GM이 지정한 플레이어에게 심문·협박 성공 3회', '상', None),
    'M48': (None, '다른 캐릭터에게서 질문 계열 행동을 받은 횟수 10회', '중', None),
    'M50': (None, '기관실에 총 12사이클 머무름', '상', None),
    'M51': (None, '확실한 단서 12개 획득(탐색·질문 계열·교섭 모두 셈)', '중', None),
    'M52': (None, '한 번의 교섭으로 단서 3개 이상 받기 2회', '상', None),
    'M53': (None, '교섭으로 받은 단서 합계 9개 이상', '중', None),
    'M54': (None, '준 단서가 받은 단서보다 많은 교섭 3회 성사', '중', None),
    'M55': (None, '한 번의 탐색으로 단서 2개 이상 얻기 3회', '상', None),
    'M56': (None, '교섭 이행 8회, 불이행 0회', '중', None),
}
RETIRED = {
    'O2': '40사이클이 필요해 30사이클 판에서 불가능',
    'O7': '40사이클이 필요해 30사이클 판에서 불가능',
    'M16': '40사이클이 필요해 30사이클 판에서 불가능',
    'M26': '40사이클이 필요해 30사이클 판에서 불가능',
    'M27': '40사이클이 필요해 30사이클 판에서 불가능',
    'M13': 'NPC가 필요한데 30사이클 판은 전원 플레이어',
    'M15': 'NPC가 필요한데 30사이클 판은 전원 플레이어',
    'M35': 'NPC가 필요한데 30사이클 판은 전원 플레이어',
    'M47': 'NPC가 필요한데 30사이클 판은 전원 플레이어',
    'O3': '남의 이동에 달린 "혼자 6사이클 연속"(47회 중 1회 달성). 기관실 체류는 M50으로',
    'M17': '관계 하락이 필요한데 에이전트가 부정적 반응을 거의 고르지 않음(34회 중 0회)',
    'M18': '관계 하락이 필요한데 에이전트가 부정적 반응을 거의 고르지 않음(29회 중 0회)',
    'M22': '핵심 행동(질문)을 판 내내 포기해야 함(34회 중 2회)',
    'M24': '준비가 판당 5~10회뿐이라 준비 후 판정 8회가 불가능에 가까움(39회 중 0회)',
    'M37': '방해형 I2(들키지 않은 배신)로 대체',
    'M49': '남의 행동에 달린 "질문을 한 번도 받지 않음"(86회 중 3회)',
}
# Interference missions (A53): designated target, deception-capable, judged at the game's end.
INTERFERENCE = [
    {'id': 'I1', 'name': '미션 방해', 'condition': 'GM이 지정한 플레이어가 GM이 정한 그의 횟수형 개인 미션 하나(어느 미션인지는 알려 주지 않음)에서 필요한 수의 절반 이상까지 갔다가 끝내 달성하지 못함',
     'designate': 'player_mission', 'designate_count': 1},
    {'id': 'I2', 'name': '들키지 않은 배신', 'condition': '상대만 이행하고 자신은 불이행한 교섭 1회 이상, 판이 끝날 때 그렇게 속인 상대 모두가 나에게 가진 관계성 50 이상',
     'designate': None, 'designate_count': 0},
    {'id': 'I3', 'name': '정보 봉쇄', 'condition': 'GM이 지정한 장소의 단서를 얻고, 그 뒤 판이 끝날 때까지 다른 플레이어가 그 단서를 얻지 못함(최소 8사이클)',
     'designate': 'place', 'designate_count': 1},
    {'id': 'I4', 'name': '압박과 신뢰', 'condition': 'GM이 지정한 플레이어에게 심문·협박 성공 2회, 판이 끝날 때 그 플레이어가 나에게 가진 관계성 50 이상',
     'designate': 'player', 'designate_count': 1},
]
MBTI_LETTERS = set('EISNTFJP')
# Pick slots by expected achievement (score is flat): roughly equal thirds so every slot has candidates.
TIER = {**{k: '하' for k in ('M20', 'M23', 'M10', 'M48', 'M51', 'M01', 'M07', 'M12', 'M14', 'M28', 'M36', 'M41', 'M56', 'M40', 'M38')},
        **{k: '중' for k in ('M32', 'M33', 'M34', 'M53', 'M54', 'M39', 'M44', 'M25', 'O4', 'M08', 'M09', 'M45', 'M50', 'M03')},
        **{k: '상' for k in ('O1', 'O5', 'O6', 'M02', 'M04', 'M05', 'M06', 'M11', 'M19', 'M21', 'M29', 'M30', 'M31', 'M43',
                            'M46', 'M52', 'M55')}}


def rebuild(missions):
    by_id = {m['id']: m for m in missions}
    out = []
    for m in missions:
        m = dict(m)
        m['easier'] = [t for t in m.get('easier', []) if not (len(t) == 1 and t in MBTI_LETTERS)]
        m['harder'] = [t for t in m.get('harder', []) if not (len(t) == 1 and t in MBTI_LETTERS)]
        m.pop('quantity', None)
        if m['id'] in REVISED:
            name, cond, base, judged = REVISED[m['id']]
            m.update(condition=cond, base=TIER[m['id']], category='personal')
            if name:
                m['name'] = name
            if judged:
                m['judged'] = judged
            else:
                m.pop('judged', None)
            m.pop('retired', None)
        elif m['id'] in RETIRED:
            m.update(retired=True, retired_reason=RETIRED[m['id']], category='personal')
            m['base'] = m.get('base') or '중'
        elif m['id'] == 'M42':
            m.update(retired=True, retired_reason='방해형 I3(정보 봉쇄)로 대체', category='personal')
        else:
            raise SystemExit(f'mission {m["id"]} has no v10 decision')
        out.append(m)
    for i in INTERFERENCE:
        if i['id'] in by_id:
            continue
        out.append({**i, 'base': '하', 'easier': [], 'harder': [], 'interference': '', 'category': 'interference', 'judged': 'end'})
    return out


def main():
    for path in (ENGINE, GM):
        data = json.loads(path.read_text(encoding='utf-8'))
        data['missions'] = rebuild(data['missions'])
        data['mission_reward'] = 20
        data.setdefault('provenance', {})
        if isinstance(data['provenance'], dict):
            data['provenance']['v10_missions'] = 'tools/build_v10_missions.py (A53, user decisions 2026-11-12)'
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    live = [m for m in data['missions'] if not m.get('retired')]
    print('missions', len(data['missions']), 'live', len(live),
          {t: sum(1 for m in live if m['category'] == 'personal' and m['base'] == t) for t in ('하', '중', '상')},
          'interference', sum(1 for m in live if m['category'] == 'interference'))


if __name__ == '__main__':
    main()
