# API 테스트 방법

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
| `externals` | 외부 경계의 가짜들 — 기본 세계 |
| `client` | 진짜 테스트 DB + `externals` 로 만든 웹 API 테스트 클라이언트 |

## 외부 경계와 가짜 (`tests/fakes.py`)

| 경계 (`Externals` 필드) | 가짜 | 기본값 / 쓰는 법 |
|---|---|---|
| `listing` 전종목 목록 | `make_listing([...])` (`helpers.py`) | 종목 3개 (삼성E&A, 삼성바이오로직스, 대한항공) |
| `prices` 종목별 시세 | `FakePrices(히스토리들, failing=[...])` | 세 종목 모두 필터를 통과하는 시세. `failing` 이거나 모르는 종목은 `PriceFetchError` |
| `trend` 수급 | `FakeTrend(행들, failing=[...])` | 세 종목 모두 외국인·기관 순매수(양호). `failing` 이면 오류, 모르는 종목은 **빈 목록**(실제 서버와 같다) |
| `sector` 업종 | `FakeSector({코드: SectorInfo(...)})` | 세 종목의 업종. 모르는 종목은 `None` (읽지 못함) |
| `analysis` 분석 서버 | `FakeAnalysisServer(decisions=..., errors=..., polls_before_done=N, health_error=...)` | 모두 바로 끝나고 등급은 `Hold`. 보낸 요청은 `.started` 에 기록된다 |
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
    assert response.json() == {"status": "ok", "db": "ok", "schema_version": 1}
    assert pg_conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 1
```

이 틀 자체의 테스트(`tests/test_api_base.py`)는 가짜들이 실제 소비 코드(분석 호출, 수급 판정, 시세 저장소)에서 그대로 쓰이는지도 확인한다. 가짜를 새로 만들 때 참고한다.
