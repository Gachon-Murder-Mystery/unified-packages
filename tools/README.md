# tools

캠페인 점검·정리용 스크립트. 프로젝트 루트(`unified-packages-v9/`)에서 `PYTHONUTF8=1 python -B tools/<파일> ...`로 실행한다.

| 파일 | 용도 | 사용법 |
|---|---|---|
| review_campaign.py | 판별 건강 지표(대리 응답, 오류, 질문 시간초과)와 결과(거래, 기만, 남은 조합, 확실 단서, 추리 도전) | `tools/review_campaign.py . <캠페인>` |
| mbti_monitor.py | 판별 MBTI 축(E/I, S/N, T/F, J/P)의 행동 차이와 16명 간 분산(수렴 감시) | `tools/mbti_monitor.py . <캠페인>` |
| rollback_aborted.py | 공유 풀에서 중단된 판(operator_stopped)이 남긴 경험 이벤트·학습 집계·평판·에이전트 상대 기록을 되돌림. 기본은 점검만, `--apply`로 적용(백업 후) | `tools/rollback_aborted.py runs/<캠페인> <game_id 앞부분> [--apply]` |
| archive_runs.py | 대체된 캠페인과 그 판 폴더를 `runs/_archive/<이름>.zip`으로 압축(검증 후 원본 삭제) | `tools/archive_runs.py runs [--apply]` |
| audit_memory.py | [v9] 기억 분리 점검: 파일 주인, 참가한 판만 기억, GM/플레이어 기억 분리, 엔진 비밀 누출, 판 안 작업 기억의 영속 여부. 통찰 유사도(수렴 경고)도 보고 | `tools/audit_memory.py runs/<캠페인>` |
| replay_traces.py | [v9] 캠페인의 `regression_traces.jsonl` 재생: GM 이상 상황이 같은 유형으로 다시 진단되는지 확인. 판 실패·모델 장애 기록은 목록만 | `tools/replay_traces.py runs/<캠페인>/regression_traces.jsonl` |
| game_report.py | 게임 보고서 양식 1부(한 판 보고서)를 기록에서 생성: 개요, 배정표, 칸별 단서, 사이클별 전개(행동·거래·미션·남은 조합), 전환점과 판단 이유 발췌, 최종 결과 | `tools/game_report.py runs/<캠페인> <시드> [--out <파일>]` |
| relationship_monitor.py | T/F가 관계에 따라 갈리는지: 관계 수준별 수락률, 질문 방식, 기만. [v9] 에이전트 친밀도 변화와 판 끝 보고 점수 분포 | `tools/relationship_monitor.py . <캠페인>` |
| analyze_sim.py | `runs/sim-capacity/results.jsonl` 요약(시나리오 × 직업 × 사이클) | `tools/analyze_sim.py .` (루트 경로) |

용량 시뮬레이터 본체는 루트의 `sim_clue_capacity.py`, 기록 검증은 루트의 `verify_persistence.py`.
