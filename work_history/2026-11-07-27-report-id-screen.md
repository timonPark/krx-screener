# [#27] 화면의 보고서 번호를 report_id로 바꾸기

- 이슈: https://github.com/timonPark/krx-screener/issues/27 (에픽 #7, 스펙 #1)
- 브랜치: feat/27-report-id-screen (main 위)
- 작업일: 2026-11-07

## 수행한 변경

- `frontend/report.js` 신규: `reportHref(template, reportId)` — 주소 틀의 `{report_id}` 를 번호(주소용으로 인코딩)로 바꾼다. 틀이나 번호가 없으면 `null`.
  DOM 이 없어서 Node 로 직접 확인할 수 있게 `app.js` 에서 나눴다.
- `frontend/app.js`: 보고서 칸이 `run_id` 대신 `report_id` 를 보여주고 `reportHref` 로 링크를 만든다. 번호가 없으면 "—".
- `frontend/config.js`: 자리표시자를 `{report_id}` 로, 주소 예시 두 개를 주석에 적었다. 기본값은 `null`(글자로만 보임) 그대로.
- `frontend/mock.js`: 예시 데이터가 `run_id` 대신 `종목코드_저장일_저장시각` 모양의 `report_id` 를 낸다 (앞 3종목은 실제 번호 모양의 고정 시각, 예시 모드에서 처리하면 지금 시각).
- 문서: `frontend/README.md`(API 계약), `docs/frontend-spec.md`(7·12장, 파일 표).
- 테스트 `tests/test_frontend_contract.py` 19개 (Node 22 이상이 없으면 건너뜀).

## 설계 결정 (이슈 #27 범위 안에서 내가 정한 것)

- **링크 주소 기본값은 `null` 로 둔다.** 이슈도 "링크 주소를 정하면" 이라고 했고, 웹 화면(포트 4321, `npm run dev`)을 켜 두었는지는 사용자 환경이라 기본값으로 박을 수 없다. 대신 주석과 문서에 예시 두 개를 적었다.
- **`mock.js` 의 키가 백엔드 응답 모델과 정확히 같은지 대조하는 계약 테스트**를 넣었다 (`SessionDetailOut`·`SessionOut`·`CountsOut`·`ItemOut`). 예시 모드가 실제 모양과 어긋나 있었기 때문이다
  (`run_id` 가 남아 있고 `report_id` 가 없었다). 앞으로 백엔드 응답 모델을 바꾸면 예시 데이터가 따라가지 않을 때 테스트가 알려 준다.
- 옛 `{run_id}` 자리표시자는 호환하지 않는다. 낡은 설정은 눈에 띄게 깨진다 (엉뚱한 번호로 조용히 연결되지 않는다).
- `replace` 대신 `replaceAll` — 주소에 자리표시자가 두 번 들어가도 모두 바뀐다.

## 검증 결과

- `pytest`: 테스트 DB 연결 시 **601 passed** (이전 582), `ruff` 통과. Node 가 없는 환경에서는 계약 테스트 19개가 건너뛰어진다.
- 변이 검사 7종 모두 테스트가 잡았다(확인 후 원복): 자리표시자를 `{run_id}` 로, 번호 인코딩 제거, 번호 없이도 링크, 화면이 다시 `run_id` 를 읽음, mock 이 `run_id` 를 다시 냄,
  mock 이 번호 모양을 틀리게 냄, mock 에서 `report_id` 누락.
- **실제 TradingAgents-KR 서버로 링크가 열리는지 확인** (읽기 전용으로 잠깐 띄우고 껐다. 분석을 시작하는 `POST /api/runs` 는 호출하지 않아 쿼터는 들지 않았다):
  - 보고서 목록의 번호가 우리가 저장하는 형식과 같다 (`030530_20261106_181027`, `028050_20261106_175036` …).
  - `GET http://127.0.0.1:8000/api/reports/028050_20261106_172435/download?format=html` → **200 `text/html` 168KB** (주소 예시 2).
  - `GET http://127.0.0.1:4321/report?id=028050_20261106_172435` → **200**, 제목 "리포트 열람 — TradingAgents-KR" (주소 예시 1, 웹 화면). 없는 번호·경로 이탈 번호는 404.
  - TradingAgents-KR 저장소는 바꾸지 않았다 (서버만 잠깐 실행). 확인 뒤 8000·4321 포트의 프로세스를 모두 종료했다.
- 완료 조건 대조: 보고서 칸에 `028050_20261106_172435` 같은 번호가 보인다 ✓ (예시 모드 데이터와 화면 코드가 `report_id` 를 읽음 — 아래 한계 참고) /
  링크 주소를 정하면 TradingAgents-KR 보고서 화면이 열린다 ✓ (위, HTTP 수준).

## 알려진 한계

- **실제 브라우저로 화면을 열어 보지는 못했다.** 화면 스크린샷 도구는 이미 열려 있는 브라우저의 원격 디버깅 연결이 필요해서 쓰지 못했다.
  표의 보고서 칸이 22자 번호로 길어졌을 때의 모양은 눈으로 확인하지 않았다 — 표는 가로 스크롤 영역(`.scroll`, `min-width: 1280px`)이고 칸은 `nowrap` 이라 칸이 넓어질 뿐 줄바꿈·잘림은 없는 구조다.
  DOM 이 실제 응답으로 그려지는 것은 #28 에서 확인한다.
- 링크 주소(`reportUrlTemplate`)는 사용자가 `config.js` 에 직접 적어야 한다 (설정 화면 없음). TradingAgents-KR 가 꺼져 있으면 링크가 열리지 않는다.
- 예시 데이터(`mock.js`)의 값은 백엔드와 키만 같다. 값은 다른 곳이 있다 (예: 대기 종목의 `agents_total` 은 mock 이 10, 백엔드는 0 / 정리 응답의 `unscored` 가 없음 / 409 문구).
  #28 에서 실제 API 로 화면을 확인하며 맞출 것이 있는지 본다.

## 다음 작업 핸드오프

- #28 화면과 실제 API 연동 확인 (브라우저로 `krx-screener serve` 주소를 열어 DOM 확인 — 위 한계의 값 차이도 이때 본다).
- #12 SQLite 대기열 → Postgres 교체, #29 GLOSSARY, #30 설계 문서·ADR·README 갱신.
