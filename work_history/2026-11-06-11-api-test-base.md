# [#11] API 테스트 기반: 테스트 전용 Postgres와 외부 경계 가짜

- 이슈: https://github.com/timonPark/krx-screener/issues/11 (에픽 #2, 스펙 #1)
- 브랜치: feat/11-api-test-base (feat/10-session-repository 위)
- 작업일: 2026-11-06

## 수행한 변경

- `src/krx_screener/externals.py` 신규: 백엔드가 바깥과 닿는 **여섯 곳**을 `Externals` 로 정의한다 —
  전종목 목록, 종목별 시세, 수급, 업종, TradingAgents-KR 분석 서버, 분석 상태 로그 폴더.
- `src/krx_screener/api.py` 신규: 웹 API 틀. `create_app(Dependencies(connect, externals))`, 오류 형식
  `{"detail": 한국어}`, `GET /api/health` (스키마 버전 포함, DB 문제는 503).
- `tests/fakes.py` 신규: 각 경계의 가짜 (`FakePrices`, `FakeTrend`, `FakeSector`, `FakeAnalysisServer`),
  실제 로그 모양의 상태 로그를 쓰는 `write_state_log`, 기본 세계 `make_externals`.
- `tests/conftest.py`: `externals`, `client` 픽스처 추가 (진짜 테스트 DB + 가짜 경계 + 웹 API 테스트 클라이언트).
- `tests/test_api_base.py` 신규: 15개. `docs/api-testing.md` 신규: 쓰는 법.
- `pyproject.toml`: `fastapi`, 개발용 `httpx2`.

## 설계 결정 (이슈 #11 범위 안에서 내가 정한 것)

- **테스트 seam 은 웹 API(HTTP) 하나**, DB 는 진짜, 외부 경계만 가짜 (스펙의 결정을 그대로 구현).
- **외부 경계를 `Externals` 한 곳에 모았다.** 이후 이슈가 새 외부 호출을 추가하면 여기에 필드를 더하면 되고,
  테스트는 `dataclasses.replace` 로 한 경계만 바꾼다.
- **분석 상태 로그는 함수가 아니라 "폴더 위치"를 경계로 했다.** 스펙에 "임시 폴더에 실제 로그 모양의 JSON 을
  놓는다"고 되어 있어서, 읽기 코드(#21)를 실제 파일로 검증할 수 있게 하려는 것이다. 가짜는 실제 로그에서 확인한
  칸(에이전트 10개 + 등급 줄이 없는 2개)과 `run_settings` 모양을 따른다.
- 모르는 종목의 수급은 **빈 목록**을 돌려준다 (실제 네이버 응답과 같다 — 오류가 아니다).
- 업종 경계는 `SectorInfo | None` (읽지 못하면 None, 오류로 멈추지 않음). 실제 구현은 #16 이다.
- **개발용 테스트 클라이언트는 `httpx2`**. 처음에 `httpx` 를 넣었더니 Starlette 가 "httpx2 를 쓰라"는 경고를
  내서(pydantic 조직의 차세대 HTTP 클라이언트) 안내를 따랐다. 경고 없이 통과한다.
- 서버 실행(uvicorn)은 이번에 넣지 않았다 (#26 에서). 이 이슈는 테스트 클라이언트만 필요하다.

## 검증 결과

- `pytest`: 테스트 DB 연결 시 **254 passed**, DB 없이 **211 passed, 43 skipped**. `ruff` 통과.
- 완료 조건: 요청을 보내고 DB 상태를 확인하는 예시 테스트 통과 ✓ (`/api/health` 응답의 스키마 버전이 DB 의
  `schema_migrations` 와 같음), 가짜 쓰는 법 문서화 ✓ (`docs/api-testing.md`).
- 가짜가 **실제 소비 코드에서 그대로 쓰이는지**도 테스트했다: 분석 호출(`run_one`), 수급 판정(`fetch_flow`),
  시세 저장소(`PriceStore`), 종류 분류(`classify`).

## 중간에 고친 것

- 가짜 상태 로그의 감성 분석가·트레이더 칸에 "등급 줄 없음"이라는 안내 문구를 넣었더니 "등급이 없는 칸"을
  확인하는 검사가 틀리게 나왔다. 문구를 빼서 고쳤다.

## 알려진 한계

- 이 이슈에서 API 는 `/api/health` 하나뿐이라, "여러 요청에 걸친 동작"(잠금, 게이팅)의 검증 예시는 각 기능
  이슈(#17, #18, #24)가 더한다.
- 업종 경계의 실제 구현(#16)과 상태 로그 읽기(#21)는 아직 없다. 경계만 정의했다.

## 다음 작업 핸드오프

- #12 기존 SQLite 대기열을 Postgres 저장소로 교체, 이후 #13 세션 API.
