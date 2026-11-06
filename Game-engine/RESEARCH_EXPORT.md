# 전체 게임 기록과 LLM 원문 export

2026-11-01. 분석 자료의 저장·export는 엔진이 담당한다. 에이전트의 판단 정책은 B/C에 둔다.
API가 반환한 문장·판단 설명·추론 필드/요약을 보존하며, 제공자가 반환하지 않은 내부 추론을 생성하거나
복원하지 않는다. 과거에 수집하지 않은 출력은 소급 복구할 수 없다.

## 전체 기록 내려받기

```powershell
python integration_cli.py --data data/competitive-demo.sqlite3 export --game-id "게임 ID" --output data/exports/game-records.json
```

`export`의 기본 범위는 `all`이다. 파일은 UTF-8로 직접 저장하며 PowerShell 출력 리다이렉션에 의존하지 않는다.
기존 경쟁형 요약은 `--scope summary`로 선택한다. 경쟁형 `/competition` 운영자 화면의
**전체 기록 내려받기 (JSON)** 버튼으로도 같은 자료를 받는다.

HTTP는 `POST /integration/v1/games/export`에 `{game_id, operator_token, scope:"all"}`을 보낸다.
운영자만 가능하며 GM·플레이어·관전자 자격으로는 받을 수 없다. 게임 진행 중/종료 후 모두 가능하다.
기존 단일 게임과 legacy 외부 게임도 같은 CLI/API로 전체 기록을 받는다.

전체 JSON에는 다음을 넣는다.

- `game`: 사건 정답·단서·캐릭터·관계·미션·계획·교섭·판정 등 저장된 게임 상태
- `events`: 경쟁형에서 별도 보관한 이벤트까지 포함한 전체 순서 기록
- `archived_records`: 핫 상태에서 분리한 행동 영수증·명령·질문·교섭 메시지 기록
- `experience_events`: 해당 회차의 공식 경험 이벤트
- `llm_records`: 호출 시작·완료·실패 기록과 요청·응답 원문·사용량·오류
- `capture`: 수집 호출 수, 종료 기록 없는 호출 수, 기록이 없는 참가자, 수집 범위 설명

인증 키·역할 토큰·난수 비밀·인증 헤더는 제거한다. 그 외 문장은 공백·줄바꿈·한국어를 유지한다.
사건 정답과 타인의 비공개 단서/판단은 연구용 전체 export에 포함되므로 플레이어에게 배포하는 자료가 아니다.
다른 회차 기록이나 전체 저장소/환경을 덤프하는 기능도 아니다.

## 권장: 엔진을 통한 LLM 호출

외부 B/C 에이전트의 LLM 클라이언트를 아래 클라이언트로 주입하면 **제공자 응답을 엔진이 직접 수집**한다.
기존 `request(system,user,budget)`가 `(text,usage)`를 반환하는 형태를 유지한다.

```python
from engine_client import EngineClient, EngineLLMClient

role = EngineClient(base_url, credentials=game_role_credentials, timeout=60)
llm = EngineLLMClient(role, api_key, model=model_id,
                      connection="openai", stage="player.decision",
                      correlation={"action_id": action_id})
text, usage = llm.request(system_prompt, observation_json, budget=2000,
                          call_id=stable_call_id)
# B/C 에이전트 생성자의 client 인자에 llm을 넘긴다. GM도 자신의 역할 자격으로 동일하게 사용한다.
```

`call_id`를 생략하면 UUID를 만든다. 네트워크 재전송 시 같은 ID와 같은 입력을 사용하면 제공자를 재호출하지
않고 저장 결과를 돌려준다. 진행 중인 호출은 `call_in_progress`, 같은 ID의 다른 입력은 `conflict`다.
이미 실패한 호출은 재전송해도 다시 과금 호출하지 않는다. 의도적인 재시도만 새 ID를 사용한다.

`POST /integration/v1/llm/invoke`가 해당 경로다. 역할 인증 후 시작 기록을 커밋하고 SQLite 잠금을 풀어
제공자를 호출한다. 엔진 watchdog은 기다리지 않는다. 키는 호출에만 쓰고 저장·export하지 않는다.
기존 Zen 게이트웨이의 Responses/Chat 전송 방식을 사용하며 스트리밍/다른 제공자 SDK를 일반화한 프록시는 아니다.

응답은 `response.raw_body`(제공자 본문), `response.json`(원본 JSON), `response.returned_text`를 저장한다.
`reasoning_content`, `reasoning.summary`, 도구 호출 등 제공자가 JSON에 포함한 필드를 삭제하지 않는다.
파싱 실패 시에도 받은 본문을 실패 기록에 남긴다. `usage`도 제공자가 반환한 값을 보존한다.
없는 사용량이나 추론 내용을 추측해 채우지 않는다.

## 기존 클라이언트를 유지하는 방법

```python
from llm_recording import RecordingLLMClient

recorded = RecordingLLMClient(existing_llm_client, role,
    model=model_id, provider="existing-provider", stage="player.decision",
    correlation={"action_id": action_id}, spool="data/player-llm-spool.jsonl")
result = recorded.request(system_prompt, observation_json, budget=2000)
recorded.flush()  # 업로드 실패 뒤 동일 ID로 재전송
```

이 어댑터는 **기존 클라이언트가 반환한 값**을 그대로 보존하며 반환형을 바꾸지 않는다.
기존 클라이언트가 제공자 추론 필드를 이미 버렸다면 어댑터로 복구할 수 없다. 이 경우 위 엔진 게이트웨이를
사용하거나 제공자 응답을 받는 지점에서 `role.record_llm(record)`를 호출해야 한다.

로컬 JSONL 저널에 기록·fsync 후 엔진에 전송한다. 전송 실패는 숨기지 않고 예외를 발생시키며
`flush()`로 미확인 항목을 재전송한다. 저널에는 역할 토큰을 저장하지 않는다. 끊긴 마지막 JSONL 행은
조용히 건너뛰지 않고 복구 필요 오류를 낸다. 저널 파일은 에이전트별로 분리해 사용한다.

수동 수집 경로는 `POST /integration/v1/llm/records`이며 게임 역할 자격과 `record`를 보낸다.
`call_id`, `status`(`started/completed/failed`)가 필수이고 `stage/runtime/model/provider/capture`,
`request/response/usage/error/correlation/started_at/finished_at`을 지원한다.
`correlation`에 action/question/command ID를 넣어 엔진 기록과 연결한다.

수집은 append-only이고 같은 호출·상태·내용은 멱등 처리한다. 다른 내용으로 덮어쓰거나 완료·실패를
동시에 확정할 수 없다. 외부 수집 API의 단일 기록은 UTF-8 JSON 2 MiB, HTTP 요청은 3 MiB 한도이며 초과 시 잘라 저장하지 않고
거부한다. 큰 출력을 직접 수집할 때는 서로 다른 call_id의 분할 기록과 correlation에 원 호출 ID·순서를 넣는다.
엔진이 직접 받은 제공자 응답에는 외부 업로드 한도를 적용하지 않아 큰 원문도 보존한다.
프로세스 중단 등으로 종료 기록이 없는 호출은 `capture.unfinished_calls`로 확인한다.

GM 승계 전에 시작한 호출은 같은 옛 자격으로 그 호출의 종료 기록만 남길 수 있다.
옛 GM의 새 호출이나 게임 명령은 계속 거부된다. 기록이 게임 판정·행동·GM 권한을 만들지는 않는다.
엔진 프록시는 일시 정지·종료 후 새 제공자 호출을 시작하지 않는다. 이미 진행 중인 호출의 종료 기록은 받는다.

## 내장 데모 및 수집 범위

앱이 생성하는 기존 단일 게임/멀티 데모의 Zen 게이트웨이에도 원문 기록을 자동 연결했다.
멀티 데모는 플레이어/GM 식별자와 임기를 기록한다. 키 없는 오프라인 동작은 LLM 호출 기록을 만들지 않는다.
`launcher.py`의 기존 스크립트 시연도 LLM 호출을 하지 않으므로 LLM 원문이 없는 것이 정상이다.

`archive_source`는 엔진이 부여하며 `engine-gateway`, `engine-native-gateway`, `agent-submitted`를 구분한다.
외부 에이전트가 임의로 자기 기록의 출처를 엔진 게이트웨이로 지정할 수 없다.
`capture.coverage`는 instrumented-calls-only다. 외부 프로세스가 별도로 호출하고 보내지 않은 내용을
엔진이 관찰할 수는 없다. B/C 실제 실행에서 **모든 LLM 호출을 이 클라이언트/수집 경로로 통일**해야 한다.

## 검증

`python -B -m unittest test_research -q`는 실제 제공자 호출 없이 다음을 확인한다.
원문·반환 추론 필드·사용량 보존, 파싱/HTTP 오류, UTF-8 CLI 파일, 재시작, 동시/반복 요청 멱등성,
프록시 중복 과금 호출 방지, 큰 HTTP 입력, 업로드 장애·저널 재전송, 옛 GM의 종료 기록 한정 허용,
관전자/플레이어/GM의 전체 export 거부 및 일반 관찰에서 원문 비노출.
브라우저 검사는 운영자의 실제 JSON 다운로드와 관전자의 버튼 비노출도 확인한다.
