# [#26] 백엔드가 frontend 정적 파일을 같이 서빙 (apiBase 빈 값)

- 이슈: https://github.com/timonPark/krx-screener/issues/26 (에픽 #7, 스펙 #1)
- 브랜치: feat/26-serve-frontend (main 위)
- 작업일: 2026-11-07

## 수행한 변경

- `api.py`: `Dependencies.frontend_dir` 추가. 있으면 `create_app` 이 그 폴더를 `/` 에 정적 파일로 마운트한다 (`StaticFiles(html=True)`).
  **API 라우터 뒤에** 마운트해서 `/api/...` 를 가리지 않는다.
- `server.py` 신규: `build_dependencies(config_path, ...)` — 실제 경계를 이어 붙인다 (전종목 목록 `fetch_listing`, 시세 `fdr_fetcher`, 수급 `naver_trend`,
  업종 `naver_sector`, 분석 서버 `AnalysisClient(config.analysis.base_url)`, 분석 상태 로그 폴더, DB `db.connect`). 만들 때 DB·네트워크에 연결하지 않는다.
- `cli.py`: `krx-screener serve [--host] [--port] [--allow-remote] [--config] [--cache] [--frontend]` — uvicorn 으로 화면과 API 를 한 주소에서 연다.
- `pyproject.toml`: `uvicorn>=0.30` 의존성 (이전에 "#26 에서 더한다" 고 적어 둔 것).
- 문서: `README.md` "웹 화면", `frontend/README.md` 실행 방법, `docs/frontend-spec.md` 상태, `.env.example`.
- 테스트: `test_frontend_serving.py` 13개, `test_server.py` 17개. **화면 파일(`frontend/`)은 바꾸지 않았다** (예시 모드가 그대로 동작해야 해서).

## 설계 결정 (이슈 #26 범위 안에서 내가 정한 것)

이슈는 "정적 서빙"만 말하지만, 지금 서버를 실제로 띄우는 코드(실제 경계 조립, uvicorn 실행)가 없어서 "브라우저에서 한 주소로 화면이 열리고 API를 부른다" 를 달성할 수 없었다.
그래서 서빙과 함께 **실행 진입점(`serve`)과 조립(`server.py`)** 까지 넣었다. 이 부분은 이슈에 없는 결정이다:

- **기본 주소는 `127.0.0.1:8100`.** 포트는 임의로 정했다 (TradingAgents-KR 는 8000, 개발용 DB 는 5433, 프론트 README 의 예시가 8100).
- **이 컴퓨터 밖으로 여는 것은 `--allow-remote` 가 있어야 한다.** 이 서버에는 로그인이 없고, 접속하면 누구나 `POST …/process` 로 분석(= Max 플랜 쿼터 사용)을 시작할 수 있다.
  `0.0.0.0`·LAN 주소·`::` 는 거절(종료 코드 2)하고, 허용해도 경고를 한 번 더 띄운다.
- **분석 상태 로그 폴더는 TradingAgents-KR 서버와 같은 규칙**(`TRADINGAGENTS_RESULTS_DIR`, 없으면 `~/.tradingagents/logs`)으로 정했다. #21 이 남긴 "운영 쪽에서 이 폴더를 조립하는 코드가 없다" 를 여기서 채웠다.
- **DB 가 아직 없어도 서버는 뜬다.** 연결은 요청마다 하므로, 화면이 한국어 오류(`/api/health`, 503)로 알려준다 (스키마 적용은 `python -m krx_screener.db init` 그대로).
- 알 수 없는 API 주소(`/api/없음`)는 화면으로 대신 답하지 않고 404 다 (SPA 식 fallback 을 만들지 않았다).
- CORS 는 허용하지 않는다 (같은 주소라 필요 없다. 화면만 다른 포트에서 따로 띄우고 실제 백엔드를 부르는 방식은 지원하지 않음).

## 검증 결과

- `pytest`: 테스트 DB 연결 시 **582 passed** (이전 552), `ruff` 통과.
- 변이 검사 8종 모두 테스트가 잡았다(확인 후 원복): 화면 미마운트, 화면을 API 보다 먼저 마운트(`/api` 가려짐), `html=True` 제거, 기본 주소 `0.0.0.0`, 외부 주소 거절 제거,
  분석 서버 주소 고정, 로그 폴더 환경변수 무시, 시작 때 DB 연결.
- **실제 서버 프로세스로 확인** (`krx-screener serve --port 8191`, 개발 DB):
  - `/`·`/?mock=1` → 200 `text/html`, `app.js`·`api.js`·`config.js`·`mock.js` → 200 `text/javascript` (ES 모듈이 실행되는 형식), `styles.css` → `text/css`.
  - `/api/health` → `{"status":"ok","db":"ok","schema_version":1}`, `/api/sessions`·`/api/sessions/latest` → 200 JSON.
  - `/api/none`·`/nope.js`·`/%2e%2e/pyproject.toml` → 404 (화면 폴더 밖 파일은 열리지 않는다). `serve --host 0.0.0.0` → 거절, 종료 코드 2.
- **화면의 실제 `api.js` 를 Node 로 실제 서버에 연결해** 호출: `listSessions`·`getSession('latest')`(개발 DB 의 실제 세션, 종목 116개)의 응답 키가 화면 코드와 맞고,
  없는 세션은 `ApiError` 404 "세션을 찾지 못했습니다.", `organize('latest')` 는 409 "남은 116개가 끝나야 정리할 수 있습니다." + `remaining=116` (남은 종목이 있어 아무것도 바꾸지 않는다).
  `process`(쿼터 사용)와 `createSession`(개발 DB 에 세션 생성)은 호출하지 않았다.
- **실제 브라우저로는 열어 보지 못했다** (이 세션에 브라우저 자동화 도구가 없다). 화면 DOM 이 실제 응답으로 그려지는 것은 #28(화면과 실제 API 연동 확인)에서 확인한다.
- 완료 조건 대조: 한 주소로 화면이 열리고 API 를 부른다 ✓ (HTTP·`api.js` 수준) / 예시 모드(`?mock=1`)는 그대로 ✓ (화면 파일을 바꾸지 않았고 같은 페이지·`mock.js` 가 서빙된다).

## 알려진 한계

- **화면이 아직 `run_id` 를 보고서 번호로 쓴다** (`frontend/README.md` 의 Item 계약에 `run_id`). 백엔드는 `report_id` 를 준다 → #27.
- 화면은 `frontend/` 를 저장소 위치 기준(`src/krx_screener/` 의 위 폴더)으로 찾는다. 저장소 밖에 설치하면 `--frontend` 로 폴더를 줘야 한다 (패키지에 화면을 넣지 않았다).
- 서버 종료 때 처리 중인 분석(백그라운드)은 끊긴다. TradingAgents-KR 서버에서는 계속 돌 수 있고(취소 API 없음), 종목은 `running` 으로 남아 같은 구간을 다시 처리하면 이어진다 (#17·#18).
- uvicorn 을 직접 띄우는 다른 방법(`uvicorn --factory`)은 만들지 않았다. `serve` 하나로 정했다.

## 다음 작업 핸드오프

- #27 화면의 보고서 번호를 `report_id` 로, #28 화면과 실제 API 연동 확인 (이때 브라우저로 열어 DOM 확인 — `serve` 로 띄운 주소를 쓴다).
- #12 SQLite 대기열 → Postgres 교체, #29·#30 문서 (README 상태 줄·"없는 것" 절이 오래됨).
