# 통합 Murder Mystery 작업 공간

Python 3.10+ 표준 라이브러리만 사용한다. 원본 저장소는 각 디렉터리에 이력과 함께 보존했다.
확인 버전은 sources.lock.json. GM은 main이 아니라 gm-engine-v1 브랜치다.
org-profile은 Organization 소개 저장소(.github) 사본이다.

## 키 없이 자동 한 판

```bash
python run_game.py --seed 7 --progress --data runs/baseline.sqlite3 --out baseline.json
```

12명의 C 스크립트 플레이어 + 실제 C GM + 실제 A HTTP 엔진이다.
B의 LLM 판단 실행과 구별해야 한다. 기본 마감은 120사이클이다.
짧은 연결 점검: `python run_game.py --seed 7 --deadline 10 --out smoke.json`.
smoke.json은 10사이클 마감으로 정상 종료한 실제 기록이다(사건 해결은 아님).
--data 생략 시 DB는 종료 후 삭제된다. --out은 요약이며 전체 연구 기록은 DB를 보존해야 한다.

## B LLM 플레이어 연결

```bash
cp .env.example .env
# .env에 GAME_API_KEY와 GAME_MODEL을 설정한 뒤 실행
python run_game.py --llm --seed 7 --deadline 10 --data runs/llm.sqlite3 --out llm.json --progress
```

GAME_PROVIDER는 원본 B가 지원하는 openai/console/zen이다. 모델과 키는 사용자가 지정한다.
실제 호출 비용이 발생한다. 키를 파일이나 명령 결과에 기록하지 않는다.
run_game.py가 B의 Zen 클라이언트를 LLMAgent에 주입하고 C의 player factory 슬롯으로 연결한다.
B와 엔진의 동명 llm 모듈 충돌을 로더에서 격리한다. 판단 로그는 runs/게임ID/에 저장된다.
실제 LLM 호출은 키가 없어 미검증이다. 통합 실행기는 별도 스레드에서 2초마다 현재 pool/session 역할의 heartbeat를 보낸다.
따라서 순차 모델 호출 중에도 GM/플레이어 임대를 갱신한다. LLM HTTP 대기 한도는 60초다.
요약의 live_llm은 런타임 종류 표시다. API 성공 여부는 판단 로그와 player_fallbacks도 함께 확인한다.

## 전체 기록 내보내기

```bash
cd Game-engine
python integration_cli.py --data ../runs/baseline.sqlite3 export --game-id GAME_ID --output ../runs/export.json
```

게임 ID는 결과 JSON의 game_id를 사용한다. 전체 기록에는 비공개 정보가 있으므로 연구용으로 취급한다.

## 통합 판단

엔진의 공식 상태/주사위/승패와 GM의 역할 배정/미션/진행/보고는 이미 연결 가능하다.
README의 오래된 모의 계약보다 GM gm-engine-v1의 run_scenario와 B adapter를 기준으로 연결했다.
기존 main만 모으면 최신 경쟁형 정책이 빠진다. LLM 없이도 자동 한 판이 가능하며,
실제 B 실행에는 명시적 클라이언트 주입이 필요했다.
원본 라이선스와 출처 문서는 각 저장소에 보존되어 있다. GitHub 원격 변경은 하지 않았다.

루트의 `.env`는 실행 시 자동으로 읽는다. 기존 셸 환경변수가 우선한다.
`GAME_MODEL`은 사용할 모델 ID 문자열이다(예: `gpt-6-luna`). 실제 사용 가능 여부는 제공자와 계정 권한에 따른다.

## LLM 원문 자동 기록 (통합 실행기 수정)

B의 모든 request 호출은 EngineLLMClient → llm/invoke를 거친다.
엔진 DB에 요청, 제공자 원본 응답 JSON/본문, 반환 텍스트, 사용량, 실패를 보관한다.
정상 종료 시 runs/<game_id>/full-record.json을 자동 저장하며 콘솔과 요약의 full_record_file에 경로를 표시한다.
--out은 여전히 요약이다. 전체 기록의 llm_records에서 원문을 확인한다.
키 없는 스크립트 실행에는 LLM 호출이 없으므로 llm_records도 비어 있다.
제공자가 반환하지 않은 내부 추론은 포함되지 않는다. 기존 실행에서 수집하지 않은 원문은 복구할 수 없다.
중단/오류 시 자동 파일 export는 실행되지 않으므로 --data로 DB를 보존하고 위 CLI로 내보낸다.

`--out result.json`은 `runs/<game_id>/result.json`으로 저장한다. 경로를 입력해도 파일명만 사용한다.
전체 기록 `full-record.json`과 같은 디렉터리에 모이며, 해당 이름은 요약 파일명으로 사용할 수 없다.

`--deadline N`은 실제 경과 초가 아니라 게임 사이클 마감이다. N번째 추리 단계까지 정답자가 없으면
`resolution: deadline`, `winners: []`로 정상 종료한다. 질문의 `response deadline`은 별도 응답 제한시간이며,
늦은 응답은 엔진의 기본 시간초과 처리에 맡기고 드라이버는 계속 진행한다.

## 읽을 수 있는 LLM 자연어 로그

--llm 실행 시 runs/<game_id>/llm-output.txt에 모델이 반환한 consideration과 발화 text를
줄바꿈을 유지해 실시간 저장한다. llm-raw.txt는 JSON을 포함한 응답 텍스트 원문이다.
두 파일은 성공적으로 반환된 LLM 응답부터 생성되며, 중단해도 이미 쓴 문장은 남는다.
오류와 제공자 전체 응답은 엔진 DB/full-record.json에 보관한다.
현재 B는 JSON 행동 결정과 짧은 consideration을 요구한다. 긴 역할극 대화가 생성되는 구조는 아니다.
이 로그는 모델이 실제 반환한 문장만 보여주고 행동 ID를 가짜 발화로 바꾸지 않는다.

## 2026-11-02: 참가 인원과 연구 로그

```bash
python run_game.py --llm --player-num 4 --deadline 10 --out llm.json
```

- `--player-num`: GM을 제외한 플레이어 수, 3~12, 기본 12. 첫 게임의 경험 분산 정책보다 명시한 인원을 우선합니다. 비활성 캐릭터/후보를 시나리오에서 삭제하는 옵션은 아닙니다.
- 기존 `--player package.module:factory`는 그대로 유지합니다.
- 실행 폴더 `runs/<game_id>/`에 아래 파일을 생성합니다. HTML은 브라우저에서 열고 검색할 수 있습니다.
  - `public-log.html/json`: 대외 공개용. 참가 인원, 단계 전환, 질문 쌍, 종료 사유만 허용 목록으로 출력합니다. 엔진의 shared 이벤트에도 개인 목표가 들어갈 수 있어 원문 전체를 공개하지 않습니다.
  - `table-log.html/json`: 운영자용 플레이 기록. 비공개 대화와 획득 단서가 포함될 수 있습니다. 모델의 행동 선언, 검증된 응답, 규칙 엔진 결과를 출처와 함께 표시합니다. 기존 실행은 실제 엔진 이벤트의 문장화이며 과거에 없던 대사를 창작하지 않습니다.
  - `detail.jsonl`: 실행 중 즉시 추가되는 상세 기록. 관측, 결정 ID, 모델 호출, 원문 응답, 검증/대체 처리, 엔진 요청과 결과, GM 코드 정책 명령을 기록합니다.
  - `detail-log.html/json`: 실행 종료 또는 실패 정리 시 상세 기록 및 엔진 원본/provider 기록을 펼쳐 보는 화면입니다. decision_id로 검색하면 호출과 행동을 함께 추적할 수 있습니다.
- HTML은 실행 종료 시 생성되는 재생 화면입니다. 실행 중 기록은 `detail.jsonl`에 남습니다. 실시간 웹 채팅 서버는 아닙니다.
- GM은 현재 규칙 기반 코드이며 LLM GM으로 바꾸지 않았습니다. 모델이 반환하지 않는 내부 사고는 복원하지 않습니다. 판단 근거는 모델의 명시적 자기보고이며 실제 관측·실행 결과와 비교해야 합니다.
- 모델의 `utterance`는 행동 선언을 기록하는 필드입니다. 별도의 자유 대화 채널이나 상대 플레이어의 추가 기억 입력으로 사용하지 않습니다. 질문 응답은 기존 엔진의 공개 가능 진술 규칙을 따릅니다.
- 근거 설명은 2~4문장으로 요청합니다. 기존 엔진의 출력 토큰 기반 순위 규칙은 유지되므로 예전 실행과 출력 길이·순위를 직접 비교할 때 이 프롬프트 변경을 구분해야 합니다.
- 인증 필드와 알려진 키는 상세 기록에서도 가립니다. 상세 로그와 table-log는 연구자만 열람하세요.

기존 기록을 화면으로 변환:

```bash
python research_trace.py runs/<game_id>/full-record.json
```

과거 로그에는 새 decision_id가 없으므로 호출과 실행의 정확한 연결을 소급해서 생성하지 않습니다.

## 30개 시드 순차 반복 실행

```bash
# 실제 LLM: .env 사용, 시드 1~30
python run_batch.py --llm --start-seed 1 --count 30 --batch-dir runs/experiment-30

# 키 없는 연결 점검
python run_batch.py --start-seed 1 --count 2 --deadline 1 --batch-dir runs/batch-smoke
```

기본 게임 마감은 120사이클, 한 게임 실행 제한은 3600초다. `--deadline`, `--time-limit`으로 바꾼다.
실제 모델 속도에 따라 3600초가 부족할 수 있다. LLM 모드는 모든 시드에 API 비용이 발생한다.
배치는 순차 실행하며 한 시드가 실패해도 다음 시드를 진행한다. 게임 마감으로 승자가 없는 경우도
정상 완료로 취급한다. 각 시드는 독립 DB/새 풀을 사용하므로 이전 판의 학습 이력을 이어받지 않는다.

`runs/experiment-30/batch.json`에 시드별 상태·game_id·결과 파일 경로를 기록한다.
시드별 `seed-N/attempt-N/`에 game.sqlite3, stdout.json, stderr.log를 남긴다.
자연어 로그·전체 기록·summary.json은 기존대로 runs/<game_id>/에 저장된다.
같은 옵션과 --batch-dir로 재실행하면 정상 완료 시드는 건너뛰고 실패·중단 시드만 새 DB로 재시도한다.
Ctrl+C는 현재 자식에게 전달해 정리 기회를 주고 배치를 중단한다. 실패 상태는 stderr.log로 확인한다.
