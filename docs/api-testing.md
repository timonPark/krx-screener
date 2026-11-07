# API 테스트 방법

짝 문서: [백엔드 설계](backend-design.md) (검증할 동작), [화면 명세](frontend-spec.md). 처음 쓰는 사람은 [README](../README.md) 의 준비 절차를 먼저 한다.

백엔드의 동작은 **웹 API(HTTP) 경계 하나**에서 검증한다. 구현 세부(SQL, 내부 함수 호출 순서)가 아니라 바깥에서 보이는 동작만 본다: 상태 코드, 응답 값, 처리 상태의 변화, 잠금, 점수·순위.

- **진짜**: PostgreSQL (테스트 전용 데이터베이스). DB 는 동작의 일부(잠금 포함)라서 가짜로 바꾸지 않는다.
- **가짜**: 바깥 세계와 닿는 여섯 곳 (`Externals`). 아래 표.

## 준비

```bash
docker compose up -d                      # 개발용 PostgreSQL (127.0.0.1:5433)
export KRX_SCREENER_TEST_DATABASE_URL=postgresql://krx:krx@127.0.0.1:5433/krx_screener_test
pytest                                    # DB 가 필요한 테스트까지 돈다
```

환경변수가 없으면 DB 를 쓰는 테스트는 **건너뛰고**, 나머지는 그대로 돈다 (`pytest` 는 DB 없이도 통과한다).

> 테스트 픽스처는 매번 `public` 스키마를 통째로 비운다. **테스트 전용 DB(`krx_screener_test`)만** 가리켜야 하고, 개발 DB 를 가리키면 데이터가 지워진다.

## 픽스처 (`tests/conftest.py`)

| 픽스처 | 내용 |
|---|---|
| `pg_conn` | 스키마가 막 만들어진 테스트 DB 연결 (자동 커밋). 테스트가 끝나면 비운다 |
| `repo` | `pg_conn` 위의 `SessionRepository` (세션·종목·등급 저장소) |
| `hold_lock` | 세션의 처리 잠금을 다른 연결로 쥐는 함수 (돌려받은 연결을 닫으면 잠금이 풀린다 — "처리 중이던 연결이 끊긴" 상황) |
| `queue_db` | 명령줄(`run`·`analyze`·`list`) 테스트용: 환경변수가 테스트 DB 를 가리키고 분석 상태 로그 폴더가 임시 폴더다. 돌려주는 연결로 결과를 본다 |
| `externals` | 외부 경계의 가짜들 — 기본 세계 |
| `client` | 진짜 테스트 DB + `externals` 로 만든 웹 API 테스트 클라이언트 |

## 외부 경계와 가짜 (`tests/fakes.py`)

| 경계 (`Externals` 필드) | 가짜 | 기본값 / 쓰는 법 |
|---|---|---|
| `listing` 전종목 목록 | `make_listing([...])` (`helpers.py`) | 종목 3개 (삼성E&A, 삼성바이오로직스, 대한항공) |
| `prices` 종목별 시세 | `FakePrices(히스토리들, failing=[...])` | 세 종목 모두 필터를 통과하는 시세. `failing` 이거나 모르는 종목은 `PriceFetchError` |
| `trend` 수급 | `FakeTrend(행들, failing=[...])` | 세 종목 모두 외국인·기관 순매수(양호). `failing` 이면 오류, 모르는 종목은 **빈 목록**(실제 서버와 같다) |
| `sector` 업종 | `FakeSector({코드: SectorInfo(...)})` | 세 종목의 업종. 모르는 종목은 `None` (읽지 못함) |
| `analysis` 분석 서버 | `FakeAnalysisServer(decisions=..., errors=..., polls_before_done=N, health_error=..., report_ids=..., agent_ratings=..., state_log_root=...)` | 모두 바로 끝나고 등급은 `Hold`, 보고서 번호도 준다. 끝나면 실제 서버처럼 상태 로그를 쓴다(에이전트는 기본 `Hold`). 보낸 요청은 `.started` 에 기록된다 |
| `state_log_root` 분석 상태 로그 폴더 | `write_state_log(root, 코드, 기준일, 등급들)` | 실제 로그 모양의 JSON 을 임시 폴더에 쓴다 |

### 한 경계만 바꾸기

```python
from dataclasses import replace
from fakes import FakePrices, make_externals

def test_when_prices_are_unreachable(tmp_path):
    externals = replace(make_externals(tmp_path), prices=FakePrices({}, failing=["028050"]))
    ...
```

`client` 픽스처를 쓰는 테스트에서 경계를 바꾸려면 `externals` 픽스처를 덮어쓴다 (테스트 파일에서 같은 이름의 픽스처를 정의).

### 분석 상태 로그

```python
from datetime import date
from fakes import write_state_log

# 강세는 비중 확대, 포트폴리오 매니저는 매도, 뉴스 분석가는 읽지 못한 칸(None)
write_state_log(
    externals.state_log_root, "028050", date(2026, 11, 5),
    {"bull": "Overweight", "portfolio_manager": "Sell", "news": None},
)
```

에이전트 이름: `market`, `news`, `fundamentals`, `bull`, `bear`, `research_manager`, `aggressive`, `conservative`, `neutral`, `portfolio_manager`. 빠진 에이전트는 `Hold`. 감성 분석가와 트레이더 칸에는 원래 `등급` 줄이 없다 (실제 로그에서 확인한 구조).

## 예: 요청을 보내고 DB 상태를 검증

```python
def test_health_reports_the_schema_version_and_the_db_agrees(client, pg_conn):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok", "schema_version": 4}
    assert pg_conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 2
```

## 명령줄과 화면

- **명령줄**(`run`·`analyze`·`list`)은 `CliRunner` 로 구동하고 `queue_db` 로 결과를 본다. 분석 서버는 `cli.AnalysisClient` 를 가짜 클라이언트로 바꿔 끼운다 (`tests/test_analyze_command.py`, `tests/test_queue_commands.py`).
- **화면 계약**(`tests/test_frontend_contract.py`)은 Node 22 이상이 있을 때만 돈다 (없으면 건너뜀). 예시 데이터(`src/mock.ts`)의 키가 백엔드 응답 모델과 같은지, 보고서 링크·오류 알림 규칙이 맞는지 본다.
- **화면 서빙**(`tests/test_frontend_serving.py`)은 진짜 `frontend/` 를 서빙해서 모든 파일이 열리고 JS 가 모듈 형식으로 나가는지 본다.
- **화면 타입**(`tests/test_frontend_types.py`)은 `frontend/src/types.ts` 의 인터페이스가 백엔드 응답 모델과 같은 키·같은 null 허용인지 대조한다 (#65, Node 없이 돈다).
- **화면 빌드**: 화면을 쓰는 테스트(계약·서빙·`serve`·브라우저)는 시작할 때 `frontend/dist` 를 빌드한다(`conftest.py` 의 `frontend_dist`, 수 초). 저장소 루트에서 `npm ci` 를 먼저 해 두어야 하고, 안 했으면 이 테스트들은 건너뛴다.
- **브라우저 테스트**(`tests/test_browser_screen.py`, #58)는 헤드리스 Chrome 을 CDP 로 구동해서 실제 화면을 조작한다. Node 22 이상, Chrome, 테스트 DB 가 **모두** 있어야 돈다 (하나라도 없으면 건너뜀).
  Chrome 은 macOS 기본 경로(`/Applications/Google Chrome.app`)를 쓰고, 다른 곳이면 `KRX_SCREENER_CHROME=<실행 파일 경로>` 로 알려 준다. 전체가 약 30초 걸린다 (`pytest tests/test_browser_screen.py` 로 따로 돌릴 수 있다).
  - 서버는 실제 백엔드 + 테스트 DB + 실제 `frontend/` 이고 시세·수급·분석 서버만 가짜다 (`tests/browser_harness.py`). 서버 안의 가짜는 테스트 전용 `POST /__ctl` 로 실행 중에 조작한다:
    분석 실패(`errors`), 처리가 끝나기 전 처리 중으로 답하는 횟수(`polls`), 분석 서버 꺼짐(`health_error`), DB 장애(`db_down`), 목록을 만들 때 시세를 N개까지만 받고 멈춤(`build_gate`).
  - 화면의 갱신 간격(`dist/config.js`)은 테스트에서만 짧게 바꿔 내려준다 (갱신 코드는 그대로 돈다).
  - 시나리오(`tests/browser/scenarios.mjs`)는 화면에서 본 사실을 JSON 으로 내고 판정은 pytest 가 한다. 시나리오: 새 목록(진행·중복 409·조건 문구), 처리와 정리(갱신·정리 막힘·낡음), 오류 알림이 남거나 사라지는 규칙, 만들지 못한 목록, 예시 모드. 콘솔 오류가 있으면 실패한다.
  - 새 시나리오는 `scenarios.mjs` 에 함수를 더하고 `test_browser_screen.py` 에 테스트를 더한다. 실패하면 그때의 화면과 서버 상태가 오류 출력에 같이 나온다.

이 틀 자체의 테스트(`tests/test_api_base.py`)는 가짜들이 실제 소비 코드(분석 호출, 수급 판정, 시세 저장소)에서 그대로 쓰이는지도 확인한다. 가짜를 새로 만들 때 참고한다.
