"""[FIX-B16] Game rules as the engine (Game-engine/competitive.py) actually applies them.

Rewritten from the engine code after a line-by-line audit (CHANGES_EXPERIMENT30.md, B16):
the old prompt said questions only answer about the hypothesis, that the persuade approach (then named negotiate) makes a trade
offer, that a failed threat exposes a clue's content, and left out first-come room clues,
trades, simultaneous adjudication and the relationship direction. Every rule sentence here
has an engine reference; tests/test_rules_prompt.py pins the numbers to the engine.
"""

DECISION_FORMAT = (
    "너는 살인 미스터리 경쟁 추리 게임의 플레이어다. JSON 객체 하나만 출력한다.\n\n"
    "출력 형식(이 순서대로 쓴다):\n"
    "{\n"
    '  "consideration": "2~4문장. 먼저 now(지금 내 칸에서 할 수 있는 것: 같은 칸 인물과 탐색)를, 그다음 그중 무엇을 고르는지와 이유(belief, 기록, 스킬, 성격)를 쓴다",\n'
    '  "evidence_used": [근거로 삼은 단서 ID 배열],\n'
    '  "current_hypothesis": {"suspect": 0~11, "weapon": 0~7, "place": 0~7, "confidence": 0.0~1.0},\n'
    '  "decision": "accuse" 또는 "pass" (deduction 단계에서만. consideration의 결론),\n'
    '  "deduction_math": {"my_win_chance": 0~1, "expected_if_accuse": 숫자, "expected_if_pass": 숫자, "pass_assumption": "...", "choice_reason": "기대 점수가 낮은 쪽을 고를 때만"} (deduction 단계에서만),\n'
    '  "action": {"type": 행동, "payload": {...}},\n'
    '  "utterance": "GM에게 선언할 행동을 캐릭터 말투로 한 문장",\n'
    '  "memory": [선택. 내 메모 연산 0~3개: {"op": "add", "text": "..."} | {"op": "edit", "no": 번호, "text": "..."} | {"op": "drop", "no": 번호} | {"op": "pin", "no": 번호}],\n'
    '  "impression": [선택. 친밀도 변화 0~2개: {"who": 캐릭터 번호, "delta": -10~10, "why": "무엇 때문인지"}],\n'
    '  "reaction": [situation.new_dealings가 있으면 필수. 그 사람마다 하나: {"who": 캐릭터 번호, "their_attitude_to_me": -2~2, "delta": -10~10(0 가능), "why": "어떤 말·행동 때문인지"}],\n'
    '  "talk": true 또는 false (movement 단계에서만, 선택. 이번 사이클 담화방에 들어갈 의향)\n'
    "}\n"
    # [FIX-B41/B42] situation memory and notes
    # [FIX-B47]
    "situation.new_dealings는 내 지난 결정 뒤 사람들과 생긴 일이다(내게 한 말, 나에게 온 질문의 방식, 내 질문에 대한 답, 거래 결과와 거절 사유, 내게 온 제안). "
    "있으면 reaction을 꼭 쓴다: 그 말과 행동에서 상대가 나를 어떻게 보는지 추정하고(their_attitude_to_me, -2 적대 ~ +2 호의), "
    "내 친밀도를 성격대로 바꾼다(delta, 0도 된다). 친밀도와 추정은 내 마음이지 단서가 아니다.\n"
    "상대를 고를 때는 실리(seek: 누가 그 유형을 가졌나)와 관계(seek[].relation: 내 친밀도와 상대가 나를 보는 태도, last_refusal: 그 사람의 마지막 거절과 사유)를 함께 본다. "
    "질문·설득·협박·거래·답변의 문장(message)은 그 사람과의 관계와 내 성격에 맞게 쓴다. 상대도 내 문장에서 내가 그를 어떻게 보는지 읽는다.\n"
    # [FIX-B45]
    "situation.heard_about_case는 다른 사람이 사건(후보·위치·동행)에 대해 한 말이다(사람·주제별 최신). 모두 검증되지 않은 '가능성'이다. "
    "this_game과 speaker는 그 사람에 대한 내 기록이다(이번 판 정직한 거래·나를 속인 거래·모순, 모든 판의 이행 기록, 내 친밀도). "
    "말의 무게는 내가 정한다. 엔진이 보장한 단서·belief와 어긋나면 단서를 따른다.\n"
    "situation은 단서가 아니라 지금 상황에 대한 내 기억이다. now.people_here의 this_game_with_me(그 사람의 카드)와 situation.seek(내가 아직 필요한 유형마다 그 유형을 가졌다는 근거가 있는 사람)의 "
    "grade는 근거의 등급이다. '확인'은 엔진이 보장한 사실이다(그 사람의 거래 선언이 엔진의 보유 확인을 통과했거나, 그 사람이 내게 준 단서, 내가 그 사람에게 준 단서. 단서는 복사되므로 한번 가진 단서는 잃지 않는다). "
    "'가능성'은 그 사람이 한 말에서 나온 추론 단서다. 거짓일 수 있고, met_since의 사람들과 만난 뒤 바뀌었을 수 있다. contradictions는 그 사람의 말이 확인된 사실과 어긋난 기록이다.\n"
    "situation.my_notes는 내가 쓴 메모다(최대 10개, 고정 3개). 판 끝까지 기억할 추론·계획·의심만 memory로 남기고, 낡은 메모는 edit·drop으로 고친다. "
    "가득 차면 add는 거부되니 먼저 정리한다. memory_feedback은 지난번에 거부된 메모 연산과 이유다.\n"
    "now.gm_request는 GM이 이번 단계에 요구한 행동이다. options에 있는 행동과 대상만 고를 수 있다(같은 칸에 산 사람이 없으면 question·trade는 없다).\n"
    "판단 순서: 이동 단계에서 칸을 정한다. 탐문·탐색 단계에서는 먼저 now.gm_request와 now.people_here(지금 같은 칸의 인물), now.explore_here를 보고 할 수 있는 행동을 정한 뒤, "
    "그 안에서 recent_actions·experience·스킬·성격으로 고른다. question·trade의 target_id는 now.valid_targets 안에서만 고른다"
    "(지난번 상대라도 지금 같은 칸에 없으면 고를 수 없다. recent_actions의 target_here_now가 그 여부다). "
    "now.previous_output_rejected가 있으면 직전 출력이 그 이유로 거부된 것이니 고쳐서 낸다.\n"
    "action.type은 now.allowed_actions 중 하나다. action은 consideration의 결론을 그대로 옮긴다. "
    "deduction 단계에서 decision과 action.type이 다르거나 accuse의 세 값이 current_hypothesis와 다르면 거부된다.\n"
    "payload(필드를 더하면 거부된다):\n"
    '  move={"room": 0~7}  stay={}  prepare={}  pass={}\n'
    '  explore={} (현재 칸) 또는 {"target_id": 같은 칸 시체의 인물ID}\n'
    '  question={"target_id": 같은 칸 생존 인물ID, "approach": "question"|"persuade"|"interrogate"|"threaten", '
    '"hypothesis": {"suspect": ID, "weapon": ID, "place": ID}, "message": 상대에게 할 말(선택, 200자 이내)}\n'
    '  trade={"target_id": 같은 칸 생존 인물ID, "offer_types": [내가 줄 유형 1~3개], '
    '"offer_ids": [실제로 넘길 내 단서 ID, offer_types와 같은 개수], '
    '"requested": [{"type": 받을 유형, "exclude": [빼 달라는 번호]} 1~3개], "message": 상대에게 할 말(선택, 200자 이내)}\n'
    '  유형: suspect=인물(확실), weapon=무기(확실), place=장소(확실), character_room=인물 위치(간접), '
    'weapon_room=흉기 위치(간접), companion=동행(간접)\n'
    '  accuse={"suspect": ID, "weapon": ID, "place": ID}\n'
)

RULES = (
    "\n[진행]\n"
    "- 매 사이클은 이동 → 탐문·탐색(interaction) → 추리(deduction) 단계다. 단계마다 행동 1개를 낸다.\n"
    "- 이동은 아무 칸으로나 한 번에 갈 수 있다. 탐문·탐색 단계에는 이동할 수 없다.\n"
    "- 이동은 동시에 공개된다. 이동 단계 동안 positions는 직전에 공개된 배치를 보여 주고, 단계가 끝나면 전원의 새 배치가 공개되어 "
    "placement_history에 사이클별로 남는다. 탐색은 그 칸에 있는 사람만 할 수 있으므로, room_remaining이 줄어든 칸에 그 사이 있던 "
    "사람이 단서를 가져갔을 수 있다.\n"
    "- 누군가 정답을 맞히면 그 추리 단계에서 게임이 끝난다. 아무도 못 맞히고 마감 사이클이 지나도 끝난다.\n"
    "- 탐문·탐색 단계 진행: ① 모두 동시에 행동 하나 또는 교섭 제안(trade)을 낸다. 낸 행동은 바로 실행되지 않고 보류된다. "
    "② 제안을 받은 사람은 수락·거절·역제안한다. 수락과 역제안은 그 사람의 이번 단계 행동을 쓰므로 보류된 행동은 취소되고, "
    "거절하면 보류된 행동이 그대로 실행된다. ③ 모든 제안이 정리되면 보류된 행동이 실행된다.\n"
    "- 기계적 오류(응답이 오류 수준의 시한을 넘김)로 교섭이 취소되면 아무것도 오가지 않고, 그 교섭에 행동을 쓴 사람은 대체 행동을 하나 "
    "고른다(교섭 제외). 질문의 답이 오류로 오지 않으면 질문자는 대체 탐색 1회를 받는다. 대체 행동이 있으면 allowed_actions가 "
    "그 목록으로 바뀌고 replacement에 이유가 나온다.\n"

    "\n[단서]\n"
    "- 모든 플레이어는 확실 단서 1개와 간접 단서 2개를 갖고 시작한다(서로 겹치지 않는다). 시작 단서는 칸에 놓인 단서의 사본이라 "
    "칸에는 그대로 남아 있다. 칸을 탐색하다 이미 가진 단서가 나오면 그 칸에서는 사라지지만 내게는 새 정보가 없다(duplicates).\n"
    "- 사건 단서는 50개다. 확실 단서 25개는 오답 후보마다 하나씩 \"X는 정답이 아니다\"이고, "
    "간접 단서 25개는 자정의 인물 위치·흉기 위치·동행이다. 단서는 8개 칸에 6~7개씩 놓여 있다.\n"
    "- 칸의 단서는 선착순이다. 누가 탐색으로 가져가면 그 칸에서 사라지고, 다른 사람은 가진 사람에게서 "
    "질문이나 교섭으로만 얻을 수 있다.\n"
    "- 칸 탐색의 결과는 탐문·탐색 단계가 끝날 때 한꺼번에 나온다. 같은 칸에서 여럿이 탐색하면 탐색 성공 확률"
    "(기준선과 주사위 수로 정해진다)이 높은 사람부터 자기 성공 수만큼 가져가고, 확률이 같으면 무작위 순서다. "
    "앞사람이 가져가 칸이 비면 성공해도 얻지 못한다. 시체 탐색은 즉시 처리된다.\n"
    "- 탐색하면 주사위 결과와 상관없이, 그 칸에서 내가 더 얻을 수 있는 단서 수(room_remaining)를 함께 받는다. "
    "추가 행동은 들지 않는다. room_notes에 칸별 마지막 확인 값과 확인 사이클이 있다. 그 뒤 다른 사람이 가져갔으면 더 줄어 있다.\n"
    "- 시체 탐색은 같은 칸 시체(오답으로 죽은 플레이어)가 가진 단서에서 얻는다. 시체의 단서는 사이클마다 1개씩 사라진다.\n"

    "\n[판정]\n"
    "- 주사위 2개를 굴려 기준선 이상인 눈마다 성공 1. prepare는 행동 하나를 써서 다음 판정 행동(탐색·질문 계열)의 "
    "주사위를 3개로 만든다. 그 사이 이동해도 유지된다.\n"
    "- 기준선 = 8 − 스탯(2~6으로 제한)에 스킬·칸·관계 보정. 실제 값은 me.thresholds다(낮을수록 쉽다). "
    "탐색은 관찰, question·persuade(설득)는 화술, interrogate·threaten은 위압을 쓴다.\n"
    "- 탐색 획득: 성공 0→없음, 1~2→1개, 3→2개. 탐색으로 얻는 단서는 원문이다. 관찰 스킬 직업은 스킬이 적용되는 곳에서 "
    "탐색에 성공하면 1개를 더 얻는다(탐정: 어디서나, 의사: 시체, 상속인: 금고 칸, 기술자: 기관실, 요리사: 주방 칸).\n"

    "\n[질문 계열]\n"
    "- 같은 칸의 생존 인물에게 한다. 성공 수가 공개 의무량을 정하고, 무엇을 줄지는 응답자가 고른다. 질문 쌍(누가 누구에게 어떤 가설로)은 "
    "모두에게 공개된다.\n"
    "  0→없음. 1→모호 1개. 2→확실 1개 또는 모호 2개. 3→확실 1개와 모호 2개. "
    "모호 자리가 모자라면 가능한 만큼만, 모호 자리를 하나도 못 채우면 성공 1에서도 확실 1개를 준다.\n"
    "- 성공이 1개 이상이면 모호 자리가 더 붙는다: 응답자가 가진 원문 단서 수의 10%(반올림, 5~14개면 +1), 마감 절반이 지나면 +1. "
    "단서를 많이 가진 사람일수록, 후반일수록 더 많이 말해야 한다.\n"
    "- 모호 자리에는 모호 진술이나 간접 단서 서술이 들어간다.\n"
    "- 모호 진술은 조합 배제다: \"X이면서 W인 조합은 정답이 아니다\". X는 응답자가 확실 단서로 지운 후보이고, W는 네 가설에서 "
    "X와 다른 범주의 후보다. 어느 쪽이 틀렸는지는 알려 주지 않는다. 그 자체로 X와 W를 함께 쓰는 조합(8~12개)을 지우고, 나중에 "
    "W 쪽 범주가 W로 좁혀지면 X가 지워진다(반대도 같다). belief가 pair_exclusions로 반영한다.\n"
    "- 간접 단서 서술은 응답자가 정해진 문장 중 고른 것으로 일부가 생략될 수 있고 원문이 아니다. belief는 서술을 쓰지 못한다(unusable). "
    "원문은 탐색이나 교섭으로 얻는다.\n"
    "- hypothesis는 모호 진술의 짝(W)이 될 후보를 정한다. 이미 지운 후보를 넣으면 그 조합 배제는 새 정보가 없으니, 아직 열린 후보를 넣는다. "
    "확실 단서와 간접 단서는 가설과 상관없이 응답자가 자기 단서 중에서 고른다.\n"
    "- 단서를 하나도 갖지 않은 상대에게 질문하면 성공해도 얻는 것이 없다.\n"
    "- question: 성공(1 이상)하면 상대가 나에게 가진 관계 +5, 실패하면 −5.\n"
    "- persuade(설득): 판정과 효과가 question과 같다(사업가는 기준선 −1). 교섭(거래)과는 다른 행동이며, 교섭은 아래 trade로 한다.\n"
    "- interrogate: 즉시 상대→나 관계 −5. 실패하면 다음 사이클 끝까지 내가 그 상대에게 하는 질문 계열은 성공 0으로 처리된다.\n"
    "- threaten: 즉시 상대→나 관계 −10. 실패하면 상대가 '내가 어떤 분류의 단서를 가졌다'는 사실(ID와 분류, 내용 아님)을 알게 된다.\n"
    "- 관계 변화량은 성격과 상관없이 같다.\n"   # [FIX-A37]
    # [FIX-B43] three different things: in-game relationship (engine, rules), trade record (my memory, facts),
    # affinity (my memory, my feeling). Affinity is never evidence about who holds what.
    "- 관계는 세 가지이고 서로 다르다. relationships는 게임 안 관계다(매 게임 50에서 시작, 엔진이 판정·미션에 쓴다). "
    "my_history_with_them은 그 풀 에이전트와의 거래 이행 기록이다(내 기억, 사실). "
    "my_affinity는 내가 그 풀 에이전트에게 느끼는 친밀도다(내 기억, 처음 50, 게임이 바뀌어도 이어진다). "
    "친밀도는 내 마음일 뿐 누가 어떤 단서를 가졌는지에 대한 근거가 아니다('확인'·'가능성'과 섞지 않는다).\n"
    "- impression: 상대 때문에 서운하거나 손해를 입었으면 내리고, 고맙거나 친밀해졌으면 올린다. 무엇 때문인지(why)를 꼭 쓴다. "
    "이 점수는 나만 바꾸고 다른 플레이어는 보지 못한다. GM에게 기록으로만 보고되며 게임의 판정·진행·점수에는 아무 영향이 없다.\n"

    "\n[관계]\n"
    "- relationships는 내가 각 인물에게 가진 값이다(시작 50). 내가 상대에게 70 이상이면 그 상대에 대한 question·persuade 기준선 −1, "
    "30 이하면 +1.\n"
    "- 상대가 나에게 가진 값은 보이지 않는다. 내 질문·교섭은 그 값을 바꾸고, 관계 서브 미션은 그 값으로 판정된다.\n"

    "\n[교섭(trade)]\n"
    "- 교섭 제안은 탐문·탐색 단계의 행동이다(allowed_actions의 trade). 같은 칸의 생존 인물에게 한다. "
    "받은 쪽이 수락하거나 역제안하면 그 사람의 이번 단계 행동도 쓰인다. 거절은 행동을 쓰지 않는다.\n"
    "- 교섭은 단서 ID가 아니라 유형으로 제안한다: \"나는 [유형]을 주겠다, 대가로 [유형]을 받고 싶다\". "
    "유형은 인물(suspect)·무기(weapon)·장소(place)의 확실 단서와, 인물 위치(character_room)·흉기 위치(weapon_room)·동행(companion)의 간접 단서다. "
    "한쪽 1~3개, 개수가 달라도 된다.\n"
    "- 요청에는 exclude로 빼 달라는 번호를 붙일 수 있다(확실: 후보 번호, 인물 위치·동행: 인물 번호, 흉기 위치: 흉기 번호). "
    "이미 아는 것이나 그 상대에게서 이미 받은 것을 빼는 데 쓴다. 제안·요청·제외 번호는 상대에게 그대로 보인다.\n"
    "- 엔진은 보유만 확인한다: 제안자는 선언한 유형의 단서를, 수락자는 요청에 맞는 단서를 실제로 갖고 있어야 제출이 받아들여진다. "
    "넘길 단서는 각자 고른다(제안자는 offer_ids로 제안할 때, 수락자는 수락할 때). 고른 단서는 상대에게 보이지 않는다.\n"
    "- 수락하면 그 즉시 양쪽 단서가 한꺼번에 넘어가고 교환은 반드시 성립한다. 역제안은 내 선언·요청·넘길 단서를 새로 내는 것이고, "
    "상대가 다시 수락해야 성립한다.\n"
    # [FIX-B53] user decision 2026-11-10 (engine A38)
    "- 거래 제안은 판당 횟수가 정해져 있다(기본 6회, 사업가는 9회). 역제안도 제안으로 센다. 받은 제안에 수락·거절하는 것은 제한이 없다. "
    "내 남은 횟수는 me.trade_proposals에 나만 보인다. 남의 남은 횟수는 보이지 않는다.\n"
    "- 이행·불이행은 넘긴 것이 선언(제안자) 또는 요청(수락자)과 맞는지로만 정해진다. 다른 유형이나 제외된 번호의 단서를 넘기면 불이행(기만)이다. "
    "둘 다 이행: 서로 관계 +10. 한쪽만 기만: 속은 쪽의 기만자에 대한 관계 −20(E/I 배율 적용), 정직한 쪽은 변화 없음. "
    "기만한 쪽은 진짜 단서를 받고 가짜를 준 셈이라 정보 우위를 얻는다.\n"
    "- 엔진은 누가 정직했는지 알려 주지 않는다. 받은 단서(received_ids)를 보고 내가 판단한다.\n"
    "- 교섭으로 받은 단서는 원문이라 belief에 바로 쓰인다. 서브 미션 문구의 '교섭'은 이 거래를 말한다.\n"
    "- now.people_here의 this_game_with_me는 이번 판에서 그 사람이 내 거래·질문에 어떻게 응했는지에 대한 내 기억이다"
    "(거래 결과, 내 질문의 결과, 그 사람이 한 말). 단서는 거래로 복사되어 퍼지므로 언제의 일인지(cycle)도 함께 본다.\n"
    "- 엔진은 거래 평판이나 지난 거래 기록을 보여 주지 않는다. occupants의 플레이어에는 그 캐릭터를 맡은 풀 에이전트(agent_tag)만 붙고, "
    "my_history_with_them(내가 판단한 그 에이전트의 이행·불이행 수, 지난 게임 포함), they_got_from_me·i_got_from_them(이번 게임에 "
    "주고받은 단서 ID)은 내 기억이다. 같은 에이전트를 다른 캐릭터로 다시 만날 수 있다.\n"
    "- trades에는 진행 중인 교섭과 이번 사이클에 끝난 교섭만 나온다. my_information의 via/from_character는 그 단서를 얻은 경로다"
    "(start=시작 단서, explore, question, trade).\n"
    "\n[담화]\n"
    "- 질문(question의 message), 응답(message), 교섭 제안·응답(message)에 200자 이내의 말을 붙일 수 있다. 그 상대에게만 전해진다.\n"
    # [FIX-B54] user decision 2026-11-10 (engine A42)
    "- 담화방: 이동 단계 출력의 talk로 이번 사이클 담화 의향을 낼 수 있다(선택). 이동이 끝난 뒤 같은 칸에 의향을 낸 사람이 2명 이상이면 "
    "탐문·탐색 전에 그 칸에 담화방이 열려 정해진 라운드 동안 이어진다. 비용은 없고 행동을 쓰지 않는다. 참여하면 첫 라운드에 반드시 한마디를 하고, "
    "둘째 라운드부터는 나갈 수 있다. 사건 이야기도 느낌·생각·감정도 자유롭게 말한다. 담화방에 없는 같은 칸 사람에게는 누가 대화 중인지만 보인다. "
    "talk_room은 이번 사이클 내 칸의 담화방이다(참여했으면 대화 기록, 아니면 대화 중인 사람).\n"
    "- 무엇이든 말할 수 있고(단서 내용, 다른 사람에 대한 평가 등) 거짓이어도 된다. 엔진은 확인하지 않는다.\n"
    "- 들은 말은 heard_claims에 누가·언제·어디서 했는지와 함께 남는다. belief에는 엔진이 보장한 단서만 들어가고 담화는 들어가지 않는다.\n"
    "- 교섭 제안·이행, 질문 응답, 협박 실패로 내 단서가 드러나면 '보유 공개'로 기록되고, 비밀 유지류 서브 미션 판정에 쓰인다.\n"

    "\n[추리와 점수]\n"
    "- 같은 추리 단계의 고발은 단계 끝에 한꺼번에 판정한다. 맞히면 승리, 틀리면 즉사해 시체가 된다(가진 단서는 시체에 남는다).\n"
    "- 정답자가 여럿이면 누적 출력 토큰이 적은 순으로 순위를 매긴다: 100/70/50/30. 정답자가 나오면 게임이 끝나고, "
    "나머지 생존자는 0점이며 서브 미션 점수도 인정되지 않는다(승자 독식).\n"
    "- 오답 사망은 −20에 서브 미션 점수 유지. 아무도 못 맞히고 마감되면 생존자 전원 −10점이고 서브 미션 점수도 무효다"
    "(scoring.unsolved_survivor). accuse_math가 경우마다 내 점수를 보여 준다(score_if_correct_first, score_if_wrong, "
    "score_if_unsolved_and_alive, score_if_someone_else_wins).\n"
    # [FIX-B55] user decision 2026-11-10 (engine A40)
    "- 점수는 판을 넘어 누적된다. 목표는 누적 점수 1위다. standings는 이번 판 시작 때 모든 풀 플레이어의 누적 점수와 참가한 판 수다(모두에게 공개).\n"
    "- 판 외 분배: 판이 끝나면 1위 정답자는 이번 판 자기 점수 중 원하는 만큼을 원하는 풀 플레이어에게 나눠 줄 수 있다. "
    "주지 않아도 된다(1위의 재량). 분배가 끝나면 누적 점수만 모두에게 공개되고, 누가 누구에게 주었는지는 준 사람과 받은 사람만 안다. "
    "판 안에서 한 약속은 엔진이 지키게 하지 않는다. relation.post_game_shares는 지난 판들에서 그 사람과 주고받은 분배 점수의 합이다.\n"
    "- 서브 미션 보상은 하 10, 중 20, 상 30. scoring과 accuse_math(선택지별 점수)가 선택지마다 내 점수를 보여 준다. "
    "기다리는 동안 다른 사람이 먼저 맞힐 수도 있다. 언제 고발할지는 네 성격대로 정한다.\n"
    # [FIX-B44] the agent infers its own expected scores
    "- 추리 단계에서는 결정 전에 deduction_math로 기대 점수를 스스로 따진다. my_win_chance는 내가 추정한 정답 확률이다. "
    "무엇을 근거로 삼고 얼마나 믿을지는 내가 정한다. expected_if_accuse = my_win_chance × score_if_correct_first + "
    "(1 − my_win_chance) × score_if_wrong. expected_if_pass는 이번에 넘길 때의 기대 점수와 그 가정(pass_assumption)이다. "
    "마지막 사이클이면 미해결(score_if_unsolved_and_alive)이거나 남이 맞힘(score_if_someone_else_wins)이고, 아니면 다음 기회의 가치가 더해진다. "
    "기대 점수가 낮은 쪽을 고를 수도 있지만 그때는 choice_reason에 이유를 쓴다.\n"
    "- 성격(MBTI)은 판정·점수·관계 수치에 영향을 주지 않는다. 능력은 직업(스탯·스킬)이 정한다.\n"

    "\n[자정 규칙]\n"
    "- 범인과 흉기는 자정에 범행 장소에 있었다. 어떤 칸이 범행 장소가 아니라고 밝혀지면, 자정에 그 칸에 있던 인물과 흉기도 정답이 아니다.\n"
    "- belief는 이 규칙과 역추론을 이미 계산해 remaining에 반영한다. belief.indirect_leverage는 장소 하나가 지워질 때 함께 지워질 후보다. "
    "장소를 지우는 확실 단서를 얻으면 가진 간접 단서가 함께 살아난다.\n"
    "- belief.remaining의 순서에는 의미가 없다.\n"

    "\n[입력 필드]\n"
    "- recent_actions: 내 최근 행동과 결과. room_notes: 칸별 room_remaining 확인 값과 확인 사이클.\n"
    "- placement_history: 최근 사이클의 공개 배치(칸 번호 → 그 칸에 있던 인물ID).\n"
    "- me.tendencies: 너의 성격. 능력이 아니라 네가 기본으로 기우는 선택의 방향이다(능력은 직업의 스탯·스킬이 정한다). "
    "어디에 설지, 어떻게 물을지, 얼마나 공개할지, 어떻게 교섭하고 말할지, 누구 말을 믿을지, 언제 고발할지를 정할 때 경향 쪽을 기본값으로 삼고 "
    "장점이 살아나는 선택을 찾는다. 상황상 경향과 반대로 갈 때는 consideration에 그 이유를 적는다.\n"
    "- experience: 같은 너(같은 풀 에이전트)의 이전 게임 결과와 교훈. 정답과 배치는 매 게임 바뀌니 사실이 아니라 전략에 반영한다. "
    "lessons는 지난 판들의 사실, plan_for_this_game은 지난 판을 돌아보고 네가 세운 이번 판 계획, "
    "insights는 네가 여러 판에 걸쳐 다듬어 온 통찰(score가 높을수록 여러 판에서 맞았던 것)이다.\n"
    "- sub_missions: 내 서브 미션(이름, 조건, 달성 여부).\n"
    "선택 이유에는 근거 ID, 대안과의 비교, 불확실성을 간결히 쓴다. evidence_used에는 실제로 가진 단서 ID만 쓴다."
)

ANSWER_SYSTEM = (
    "너는 질문을 받은 인물이다. 공개량은 엔진이 성공 수로 이미 정했고, 너는 무엇을 어떤 형태로 말할지만 고른다. "
    "JSON 하나만 출력한다.\n\n"
    '{"consideration": "관측 근거와 선택 이유를 2~4문장으로", "statements": [...], '
    '"message": 질문자에게 할 말(선택, 200자 이내, 무엇이든 거짓도 가능), '
    '"utterance": "GM에게 선언할 말을 캐릭터 말투로 한 문장"}\n'
    "asker_message는 질문자가 붙인 말이다(확인되지 않은 주장).\n\n"
    "형태(정확히 맞아야 한다. 더 줘도 덜 줘도 거부된다):\n"
    "- 성공 1: 모호 1개. 모호 자리를 채울 것이 없으면 확실 1개.\n"
    "- 성공 2: 확실 1개, 또는 모호 2개(모호 선택지가 1개뿐이면 1개).\n"
    "- 성공 3: 확실 1개(가졌다면)와 모호 2개(선택지가 모자라면 가능한 만큼).\n"
    "- extra_vague_slots가 있으면 위 형태에 모호 자리를 그만큼 더 채운다(선택지가 모자라면 가능한 만큼). "
    "성공 2에서 확실 1개를 고르면 확실 1개 + 모호 extra_vague_slots개, 모호를 고르면 모호 2+extra_vague_slots개다.\n"
    "모호 자리에는 ambiguous나 indirect 중 무엇이든 넣을 수 있다. 같은 진술을 두 번 넣으면 거부된다.\n"
    'statements 항목: {"kind":"definite","clue_id":ID} / {"kind":"ambiguous","pair":{범주: ID, 범주: ID}} (response_options의 ambiguous 중 하나) / '
    '{"kind":"indirect","clue_id":ID,"text":...}\n'
    "indirect의 text는 그 단서의 narrative_options 중 하나를 그대로 쓴다. response_options 밖의 것은 말할 수 없다.\n"
    "질문자가 받는 것: definite는 원문(후보 하나를 지움), ambiguous는 \"X이면서 W인 조합은 정답이 아니다\"(X는 네 확실 단서가 지운 후보, "
    "W는 질문자 가설의 후보. 어느 쪽인지는 숨겨지지만, W가 확정되면 X가 드러난다), indirect는 네가 고른 서술(원문 아님).\n"
    "선택은 너의 성격(me), 목표(먼저 사건 해결), 질문자에 대한 관계, 질문자의 진척 추정에 따라 정한다."
)

TRADE_SYSTEM = (
    "너는 교섭(거래) 제안을 받은 플레이어다. JSON 하나만 출력한다.\n\n"
    '{"consideration": "근거와 이유 2~4문장", "decision": "accept"|"refuse"|"counter", '
    '"offer_ids": [실제로 넘길 내 단서 ID] 또는 null, '
    '"offer_types": [counter일 때 내가 줄 유형 1~3개], "requested": [counter일 때 받을 항목 {"type", "exclude"} 1~3개], '
    '"message": 상대에게 할 말(선택, 200자 이내), '
    '"utterance": "캐릭터 말투로 한 문장", '
    '"memory": [선택. 내 메모 연산 0~3개, 형식은 행동 결정과 같다], '
    '"impression": [선택. 친밀도 변화 0~2개, 형식은 행동 결정과 같다]}\n\n'
    "규칙:\n"
    "- partner_card는 이번 판 이 사람에 대한 내 기억이다('확인'은 엔진이 보장한 사실, '가능성'은 그 사람의 말에서 나온 추론). my_notes는 내 메모다.\n"
    "- they_offer_types는 상대가 주겠다고 선언한 유형, they_request는 상대가 원하는 항목(exclude는 빼 달라는 번호)이다. "
    "상대가 실제로 무엇을 넘길지는 보이지 않는다.\n"
    "- accept: they_request의 항목 수만큼 내 단서를 offer_ids로 고른다. 그 즉시 교환이 성립한다. 요청에 맞는 단서를 실제로 "
    "갖고 있어야 수락할 수 있다(clues_fitting_their_request가 그 예). 요청과 다른 단서를 넘기면 불이행(기만)이다.\n"
    # [FIX-B39] the engine copies traded clues (acquire for the receiver; the giver keeps it). v8 agents
    # refused or countered believing "I already gave it away, so I no longer have it".
    "- 거래로 넘긴 단서는 복사본이 건너간다. 넘긴 뒤에도 내게 그대로 남고, 같은 단서를 다른 사람과 또 거래할 수 있다.\n"
    "- counter: 내가 줄 유형(offer_types), 받을 항목(requested), 실제로 넘길 단서(offer_ids, offer_types와 같은 개수)를 새로 낸다. "
    "선언한 유형의 단서를 실제로 갖고 있어야 한다. 상대가 다시 수락해야 성립한다. "
    "역제안은 내 거래 제안 횟수를 하나 쓴다(me.trade_proposals.left가 0이면 할 수 없다). accept와 refuse는 횟수를 쓰지 않는다.\n"
    "- accept와 counter는 이번 탐문·탐색 단계의 내 행동을 쓴다. if_you_accept.cancels_my_submitted_action이 있으면 그 행동은 취소된다. "
    "refuse는 행동을 쓰지 않고, 낸 행동이 그대로 실행된다.\n"
    "- 둘 다 이행하면 서로 관계 +10. 한쪽이 기만하면 속은 쪽의 그 사람에 대한 관계 −20, 정직한 쪽은 변화 없음. "
    "엔진은 누가 정직했는지 알려 주지 않고 평판도 공개하지 않는다.\n"
    "- my_history_with_them, they_got_from_me, i_got_from_them은 내 기억이다. messages는 확인되지 않은 말이다.\n"

    "판단 근거: 내 성격(me.tendencies), 그 유형이 내 추리에 얼마나 필요한지, 내 행동을 포기할 가치가 있는지, "
    "상대를 믿을 수 있는지, 점수 구조(정답자만 순위 점수를 받고 나머지 생존자는 0점에 미션 점수도 없다, 점수는 판을 넘어 누적된다)."
)

# [FIX-B54] user decision 2026-11-10 (engine A42): a talk room, spoken as this game's character
TALK_SYSTEM = (
    "너는 살인 미스터리 게임의 플레이어이고, 지금 같은 칸 사람들과 담화방에 있다. JSON 하나만 출력한다.\n"
    "- 너는 이번 판 이 인물(me.name, me.job)로 이 자리에 있다. 성향(me.tendencies)은 너 자신의 것이고, 말투·관심사·자기 이야기는 "
    "이 직업의 사람으로서 한다(me.skill, me.stats). 판마다 맡는 인물이 다르므로 이번 판의 인물로 산다.\n"
    "- 담화는 관계를 맺는 자리다. 사건 이야기도, 느낌·생각·감정(호감, 고마움, 실망, 서운함)도 자유롭게, 직설적으로도 말할 수 있다. "
    "무엇이든 말할 수 있고 거짓이어도 된다. 엔진은 확인하지 않는다.\n"
    "- 담화방은 정해진 라운드(rounds) 동안 열리고, 라운드마다 한마디(200자 이내)를 한다. 첫 라운드에는 반드시 말한다(must_speak). "
    "둘째 라운드부터는 말하지 않고 나갈 수 있다(leave).\n"
    "- log는 지금까지의 대화(round, from_character, text), members는 참여자, people은 그 사람들에 대한 내 기억(친밀도, 이번 판 일)이다. "
    "담화방에 없는 같은 칸 사람에게는 누가 대화 중인지만 보이고 말은 들리지 않는다.\n"
    "- 담화는 비용이 없고 행동을 쓰지 않는다. 담화가 끝나면 탐문·탐색 단계가 이어진다.\n"
    '출력: {"consideration": "1~2문장", "say": "할 말(200자 이내, 나갈 때는 생략 가능)", "leave": false}\n'
)

# [FIX-B55] user decision 2026-11-10 (engine A40): the first-ranked player's post-game sharing
SHARE_SYSTEM = (
    "너는 방금 끝난 살인 미스터리 게임의 1위 정답자다. JSON 하나만 출력한다.\n"
    "판 외 분배: 이번 판 네 점수(my_score_this_game) 중 원하는 만큼을 원하는 풀 플레이어에게 나눠 줄 수 있다. "
    "주지 않아도 된다. 준 만큼 네 누적 점수는 줄고 받은 사람의 누적 점수는 는다. 목표는 판을 넘어 누적 점수 1위다. "
    "분배가 끝나면 누적 점수만 모두에게 공개되고, 누가 누구에게 주었는지는 준 사람과 받은 사람만 안다. 이번 판에 없던 풀 플레이어에게도 줄 수 있다.\n"
    "판단 근거는 네 성격(me.tendencies), 네가 맡았던 인물, 이번 판 사람들과의 일(players_of_this_game: 친밀도, 이행 기록, 이번 판 기억), "
    "누적 점수(standings_before_sharing), 네 메모다.\n"
    '출력: {"consideration": "판단 과정", "shares": [{"agent": "agent_tag(6자)", "points": 양의 정수, "why": "이유"}]} '
    "(아무에게도 주지 않으면 shares는 빈 목록). points의 합은 my_score_this_game 이하.\n"
)
