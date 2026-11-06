# [#12] 기존 SQLite 대기열을 Postgres 저장소로 교체

- 이슈: https://github.com/timonPark/krx-screener/issues/12 (에픽 #2, 스펙 #1)
- 브랜치: feat/12-replace-sqlite (main 위)
- 작업일: 2026-11-07
- 사용자 결정: 명령줄(`run` 의 대기열 등록·`analyze`·`list`)을 **없애지 않고 Postgres 위에서 그대로 유지**한다 (이슈의 범위 선택지를 물어서 정함).

## 수행한 변경

- `src/krx_screener/analysis_queue.py` 신규: 명령줄이 쓰는 대기열(`AnalysisQueue`). `enqueue`·`rows`·`all_rows`·`record`·`mark_running`·`import_done`·`outcomes`·`done_durations`·`lock`·`latest_date`·`archived_codes`.
  이전 `store.py`(SQLite) 의 같은 일을 PostgreSQL 세션 저장소 위에서 한다.
- `src/krx_screener/store.py`, `tests/test_store.py` **삭제**. `--db` 옵션과 `krx-screener.db` 에 대한 코드가 없어졌다 (SQLite 의존성 제거).
- 스키마 **v2** (`db.py`): `sessions.source`(`web`|`cli`, 기준일마다 `cli` 세션 하나는 부분 유일 인덱스), `session_items.archived`, `session_items.elapsed_seconds`.
- `repository.py`: 보관(archived)한 종목은 `items`·`counts`·`total`·정리 순위·낡음 계산에서 뺀다. `mark_done` 이 `elapsed_seconds` 를 받는다.
- `processing.py`: 완료 처리를 `finish_item` 으로 모았다 — 웹 처리와 명령줄 `analyze` 가 같이 쓴다 (에이전트별 등급·점수·보고서 번호·소요 시간, 한 트랜잭션).
- `cli.py`: `run`·`analyze`·`list` 가 `AnalysisQueue` 를 쓴다. 연결은 환경변수 `KRX_SCREENER_DATABASE_URL`.
- `results.py`: `analysis.csv` 의 `run_id` 열이 `report_id` 로 바뀌었다.
- `api.py`: DB 에 `db init` 을 안 한 상태(스키마가 오래됨)에서 서버 오류(500) 대신 한국어 503 안내 + `/api/health` 가 같은 안내.
- 테스트: `test_analysis_queue.py` 28개(이전 `test_store.py` 의 동작 + 새 저장소 고유 동작), `test_queue_commands.py` 25개(진행 표시·잠금·에이전트 등급·웹 세션·`list`·장애 안내),
  기존 `test_analyze_command.py`·`test_results_and_cli.py` 를 새 저장소로 옮김, 웹 쪽 보관·소요 시간·오래된 스키마 테스트.
- 문서: `README.md`, `CLAUDE.md`(SQLite 문장), `docs/adr/0001`, `docs/backend-design.md`(SQLite 줄), `GLOSSARY.md`(분석 대기열 상태), `.env.example`.

## 설계 결정 (이슈 #12 범위 안에서 내가 정한 것)

1. **대기열 = 기준일마다 `source = 'cli'` 세션 하나.** 웹 화면이 만든 세션과 섞이지 않는다 (같은 기준일이어도 서로 건드리지 않는 것을 테스트로 확인). 웹 화면의 세션 목록에는 같이 보여서,
   명령줄로 분석한 결과도 화면에서 보고 정리·순위를 매길 수 있다. 스키마에 `source` 열이 필요해서 마이그레이션 v2 를 더했다.
2. **후보에서 빠진 끝난 분석은 지우지 않고 보관(`archived`)한다.** 이전 SQLite 에서는 순번을 10만으로 밀어 두는 방식이었는데, 세션의 순번은 겹치면 안 되고 화면이 모든 종목을 보여서
   별도 열로 숨겼다. 보관한 종목은 목록·개수·순위·낡음에 나오지 않고, 다시 후보가 되면 순번과 등급이 그대로 돌아온다 (쿼터를 쓴 결과라서 이 동작을 지켰다).
3. **상태 이름을 웹과 맞췄다**: `pending`/`running`/`done`/`failed`. 이전의 `error`·`timeout`·`skipped` 는 `failed` 와 사유로 합쳤고, 서버가 받지 않는 영숫자 코드는 처음부터 `failed` + 사유로 넣고
   화면(CLI)에서는 "건너뜀" 으로 보인다. 그래서 **`list --state` 의 값이 바뀌었다** (`pending, running, done, failed, skipped`).
4. **`analyze` 도 웹 처리와 같은 완료 처리를 쓴다** (`finish_item`): 에이전트별 등급·점수·보고서 번호가 같이 저장된다. 그래서 명령줄로 분석한 결과도 웹 화면에서 점수·순위가 나온다.
5. **`analyze` 는 같은 세션 잠금(#18)을 쥔다.** 웹 화면이나 다른 `analyze` 가 같은 목록을 처리 중이면 같은 종목을 두 번 보내 쿼터를 쓰게 되므로 시작하지 않는다 (보낼 것이 없으면 잠그지 않는다).
   처리하는 종목은 `running` 으로 표시한다 (끊겨서 남으면 다음 실행이 다시 보낸다). `--redo` 로 다시 분석하는 끝난 종목은 `running` 으로 바꾸지 않는다 — 실패해도 끝난 채로 기존 등급이 유지되어야 해서.
6. **`--db` 옵션 제거.** 접속 정보는 환경변수 하나(`KRX_SCREENER_DATABASE_URL`)로만 받는다 (웹 서버·`db init` 과 같다).
7. **`analysis.csv` 의 `run_id` 열 → `report_id`.** 서버의 실행 번호는 서버 메모리에만 있어 저장하지 않기로 했다 (#20). 이전 파일의 `run_id` 열은 읽지 않는다.
8. **`run` 이 DB 에 연결하지 못하면**: 결과 파일(`candidates.csv`, `run.json`)은 이미 저장했다고 알리고 종료 코드 1. 나중에 `analyze` 를 실행하면 후보 파일에서 다시 등록한다.
9. 처리 도중 소요 시간을 `elapsed_seconds` 로 저장한다. 웹 처리로 끝낸 분석도 `analyze --dry-run` 의 예상 시간 근거가 된다.
10. CLI 의 수급 열은 이제 항상 보인다 (이전에는 후보 파일에 수급 칼럼이 없으면 숨겼다). 저장값이 `미확인` 으로 채워지기 때문이다.

## 이슈가 PR 에 적으라고 한 것: 기존 SQLite 의 시범 결과 3개를 옮길지

**옮기지 않는다. SQLite 파일을 변환하는 코드는 없다.** 그러나 그 3개는 사라지지 않는다: `results/2026-11-05/analysis.csv`(DB 에서 내보낸 사본)에 같은 결과가 있고,
**그 기준일을 처음 대기열에 등록할 때 자동으로 가져온다** (이전부터 있던 "analysis.csv 가져오기" 경로). 실제 저장소 데이터로 확인했다:

```text
analyze --date 2026-11-05 --offset 0 --count 5 --dry-run   → 후보 100개 중 1~5번 5개 — 보낼 것 2개 · 이전 결과 재사용 3개
                                                              예상 소요: 약 10분 (끝난 3개의 평균 312초 기준)
list --state done → 028050 삼성E&A 보유 296초 · 207940 삼성바이오로직스 비중 축소 311초 · 003490 대한항공 보유 331초
```

가져온 결과에는 보고서 번호·에이전트별 등급·점수가 없다 (이전 방식으로 끝난 것이라 서버의 로그가 덮어써졌을 수 있다). 필요하면 `--redo` 로 다시 분석하면 채워진다 (쿼터 사용).
`krx-screener.db` 파일 자체는 `.gitignore` 의 `*.db` 라 저장소에 없고, 더 이상 쓰지 않으니 사용자가 지워도 된다 (이 작업에서는 건드리지 않았다).

## 검증 결과

- `pytest`: 테스트 DB 연결 시 **655 passed** (이전 541+…, 이번 PR 시작 전 610), DB 없이 **366 passed, 289 skipped** (CLI 대기열 테스트는 이제 DB 가 필요해서 DB 없이는 건너뛴다). `ruff` 통과.
- 이전 `test_store.py`·`test_analyze_command.py`·`test_results_and_cli.py` 가 고정하던 동작(끝난 종목 재사용, 재분석 실패 시 기존 등급 유지, 후보에서 빠진 완료 기록 보존, `analysis.csv` 한 번 가져오기,
  구간 처리, dry-run, 확인 질문, 서버 꺼짐 등)을 모두 새 저장소에서 유지한다 (같은 단언으로 옮김, 바뀐 값은 위 결정 3·4·7).
- 변이 검사 15종 중 **14종을 테스트가 잡았다** (확인 후 원복): 순번을 옮겨 두지 않음, 끝난 종목을 보관 대신 삭제, 웹 `items`·`total`·정리 순위가 보관한 종목을 셈, `analyze` 가 잠금을 안 쥠·`running` 표시 안 함,
  공용 완료 처리 우회, 영숫자 코드를 보냄, `list --state` 가 실제 상태로 거름, 오래된 스키마 안내 제거, 웹 처리가 소요 시간을 저장 안 함, 웹 세션과 대기열 공유, `run` 의 DB 오류 안내 제거.
  살아남은 1종(`rows` 의 `NOT archived` 제거)은 동치 변이다: 보관한 종목의 순번은 항상 100만 이상이라 `rows` 의 순번 범위에 걸리지 않는다. 방어용 조건으로 둔다.
- **실제 CLI 로 확인** (쿼터를 쓰지 않는 명령만, 확인 전용 DB `krx_screener_e2e`):
  - 스키마 v1 인 DB 에서 `list` → "DB 스키마가 최신이 아닙니다. `python -m krx_screener.db init` 을 실행하세요." (종료 코드 1). `db init` 뒤 v2.
  - 위 시범 결과 가져오기, `list`(전체 100개 — 대기 95 · 완료 3 · 건너뜀 2), `analyze --count 3 --yes`(보낼 것 0개, 서버에 연결하지 않음).
  - `krx-screener serve` 로 웹 서버를 띄워 `GET /api/sessions` 에 이 대기열이 세션(종목 100개)으로 보이고, 종목 줄에 종가·수급·등급이 있음을 확인.
- 실제 분석(`analyze` 로 TradingAgents-KR 에 종목을 보내는 것)은 쿼터를 쓰는 일이라 돌리지 않았다. 가짜 분석 서버로 같은 흐름을 테스트했다.

## 알려진 한계

- **기존 개발 DB 는 `python -m krx_screener.db init` 을 한 번 실행해야 한다** (스키마 v2). 안 하면 서버·명령이 한국어 안내(503/종료 코드 1)를 낸다. 이 작업에서는 개발 DB(`krx_screener`, 실제 세션이 있음)를 건드리지 않았다.
- CLI 대기열 테스트가 DB 를 필요로 해서, DB 없이 돌리면 CLI 관련 테스트 일부(입력 검사 몇 개 제외)가 건너뛰어진다.
- `analysis.csv` 의 `status` 값은 이전처럼 `done`/`error`/`skipped` 이지만 `timeout` 은 더 이상 나오지 않는다 (실패 + 사유로 합침). 시간 초과 여부는 사유 문구로 알 수 있다.
- 명령줄 세션은 웹 화면에서 일반 세션처럼 보여서, 화면에서 "처리"를 누르면 후보 파일이 아닌 세션의 종목을 같은 방식으로 처리한다 (같은 잠금으로 `analyze` 와 동시에 돌지는 않는다).
  화면은 세션의 출처(`source`)를 표시하지 않는다.
- 대기열 `enqueue` 는 같은 세션을 웹이 처리하는 중에도 순번을 다시 매길 수 있다 (종목 단위 갱신은 순번이 아니라 종목코드로 하므로 처리는 이어진다).

## 다음 작업 핸드오프

- #29 GLOSSARY 에 새 용어(세션·평가점수·순위·정리) 추가: 이번에 "분석 대기열" 정의를 고쳤고(상태 이름), 세션의 `source`(`web`/`cli`)·"보관" 은 새 용어 후보다.
- #30 설계 문서·ADR·README 의 "현재 코드와의 차이"·상태 줄 갱신 (README 상단 "상태" 줄과 `docs/backend-design.md` 14장의 "없는 것" 목록이 오래됨).
- 후속 제안(이슈 미생성): 세션에 목록 조건 저장·표시 (#28 PR 참고).
