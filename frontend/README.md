# krx-screener 화면 (frontend)

`docs/backend-design.md` 11장의 화면. 동작 명세는 [docs/frontend-spec.md](../docs/frontend-spec.md), 실행 방법은 [README](../README.md) 를 본다. HTML·CSS 와 **TypeScript** 소스(`src/*.ts`)이고, `npm run build` 가 `dist/*.js` (ES 모듈)로 바꿔서 브라우저가 읽는다 (`dist/` 는 저장소에 두지 않는다).
디자인 원본은 Claude 디자인 캔버스 "KRX Screener 화면 디자인".

| 파일 | 내용 |
|---|---|
| `index.html` | 세 구역: 세션 / 조회 표 / 처리·정리 |
| `styles.css` | 색·글꼴 토큰(`:root`)과 컴포넌트 스타일 |
| `src/app.ts` | 화면 상태, 그리기, 버튼 동작, 처리 중 주기적 갱신 |
| `src/api.ts` | 백엔드 호출 (경로는 아래 API 계약) |
| `src/report.ts` | 보고서 링크 주소 만들기 |
| `src/retry.ts` | "실패한 종목 다시 처리" 가 보낼 종목과 뺄 종목(서버가 받지 않는 영숫자 코드) 고르기 |
| `src/criteria.ts` | 세션의 조건(`criteria`)을 필터·제외·수급 문구로 바꾸기 |
| `src/notice.ts` | 오류 알림 (동작 오류와 조회 오류가 사라지는 때를 구분) |
| `src/mock.ts` | 백엔드 없이 보는 예시 API (`?mock=1`) |
| `src/types.ts` | 백엔드 응답의 타입 (`Session`·`SessionDetail`·`Item` …). 백엔드 모델과 `tests/test_frontend_types.py` 가 대조한다 |
| `src/config.ts` | API 주소, 갱신 간격, 보고서 링크 주소 |

## 실행

**먼저 빌드한다** (저장소 루트에서, Node 22.18 이상): `npm ci && npm run build` → `frontend/dist/`. 소스(`src/`)를 고친 뒤에도 다시 빌드한다 (`npm run build -- --watch` 로 저장할 때마다 빌드).
ES 모듈이라 `file://` 로 열면 막힌다. 아무 정적 서버로 연다. 빌드하지 않으면 `serve` 가 한국어로 안내하고 뜨지 않는다.

```bash
npm run typecheck    # 타입 검사 (tsc --noEmit)
npm run lint         # 모든 변수·함수(매개변수·반환값)에 타입이 적혀 있는지 검사 (ESLint typedef 규칙). frontend/src 와 tests/browser 가 대상
```

**코드 규칙**: 모든 변수와 함수의 데이터 타입을 명시한다 (추론에 맡기지 않는다). 함수를 담는 `const f = (x: number): string => …` 의 변수 자체는 매개변수와 반환 타입으로 대신한다. 규칙은 `eslint.config.ts` 에 있다.

**백엔드와 함께 (권장)** — 백엔드가 이 폴더를 같이 서빙한다 (`apiBase: ''` 그대로, 같은 주소의 `/api` 를 불러서 CORS 불필요).

```bash
.venv/bin/krx-screener serve        # 저장소 루트에서. DB 설정은 README 의 "웹 화면" 참고
# 실제 데이터: http://127.0.0.1:8100/
# 예시 데이터: http://127.0.0.1:8100/?mock=1
```

**화면만 (백엔드 없이)** — 아무 정적 서버로 연다. 예시 데이터(`?mock=1`)만 쓸 수 있다.

```bash
cd frontend && python3 -m http.server 8080     # http://localhost:8080/?mock=1
```

다른 포트에서 화면만 따로 띄우고 실제 백엔드를 부르려면 `src/config.ts` 의 `apiBase` 를 백엔드 주소로 바꾸고 백엔드에 CORS 허용이 필요하다
(백엔드는 지금 CORS 를 허용하지 않는다).

## API 계약 (화면이 기대하는 모양)

설계 문서에 경로가 정해져 있지 않아 화면 쪽에서 제안한 형태다. 바꾸면 `src/api.ts` 만 고친다.
키는 영어(i18n 경계), 사용자에게 보이는 값(`flow_status`, `decision_label`, 오류 `detail`)은 한국어.

| 기능 | 요청 | 응답 |
|---|---|---|
| 1 목록 뽑기 | `POST /api/sessions` `{"as_of": "2026-11-05"}` | `202 Session` (만드는 중) · 같은 기준일을 만드는 중이면 `409 {"detail": "…", "session_id": "…"}` |
| 세션 목록 | `GET /api/sessions` | `Session[]` (최근 순) |
| 3 조회 | `GET /api/sessions/{id}` (`id` = `latest` 가능) | `SessionDetail` |
| 2 나눠서 처리 | `POST /api/sessions/{id}/process` `{"offset": 0, "count": 10}` | `202 {"started": true, "queued": 7}` · 처리 중이면 `409 {"detail": "이미 처리 중입니다."}` |
| 2' 실패한 종목 다시 처리 | `POST /api/sessions/{id}/process-failed` (본문 없음) | `202 {"started": true, "queued": 2}` · 다시 보낼 종목이 없으면 `200 {"started": false, "queued": 0}` · 처리 중이면 `409 {"detail": "이미 처리 중입니다."}` |
| 4 정리 | `POST /api/sessions/{id}/organize` | `200 {"ranked": 115}` · 만드는 중이면 `409 {"detail": "...", "remaining": 0}` · 남았으면 `409 {"detail": "...", "remaining": 3}` |

오류는 모두 `{"detail": "한국어 메시지"}` — 화면이 그대로 띄운다. 동작(처리·정리·새 목록)이 실패해서 띄운 오류는 다음 동작이 성공하거나 세션을 바꿀 때까지 두고, 조회 실패로 띄운 오류는 다음 조회가 성공하면 사라진다.

```jsonc
// Session
{ "id": "3f2a9c1b7d4e...", "as_of": "2026-11-05", "created_at": "2026-11-06T00:12:00Z", "total": 116,
  "build": { "state": "ready", "stage": "", "done": 0, "total": 0, "error": "" },
  "criteria": { "filters": { "min_avg_trading_value_krw": 1000000000, "avg_trading_value_window": 20, "price_above_ma": [20, 60], "rsi_max": null },
                "exclude": ["preferred", "spac", "managed", "zero_volume", "new_listing", "reit"],
                "flow": { "mode": "mark", "window_days": 5 } } }   // 목록을 뽑을 때 쓴 조건. 기록이 없으면 null (화면은 "조건 기록 없음")
// build.state: building | ready | failed | interrupted. building 의 stage: prices → flow → sector, done/total 은 그 단계의 개수
// 화면 표시는 `{as_of}-{id 앞 8자리}`

// SessionDetail
{
  "session": Session,
  "processing": false,          // 이 세션을 처리 중인지 (잠금). true 면 화면이 10초마다 다시 부름 (쉬는 중에도 30초마다 확인한다)
  "ranked_at": null,            // 정리 실행 시각, 안 했으면 null
  "rank_stale": false,          // 정리 이후 재분석된 종목이 있으면 true
  "counts": { "pending": 112, "running": 0, "done": 3, "failed": 1 },
  "items": [Item, ...]          // 세션 전체, seq 순
}

// Item
{
  "seq": 1, "code": "028050", "name": "삼성E&A", "market": "KOSPI",
  "sector_code": "", "sector_name": "건설", "description": "…",
  "is_caution": false, "is_foreign": false,
  "close": 64800, "avg_trading_value": 201712536740, "rsi": 67.2,   // 거래대금은 원 단위 (화면이 억으로 바꿈)
  "flow_status": "부분",                                             // 양호 | 부분 | 약함 | 미확인
  "foreign_net_5d": -624606, "institution_net_5d": 645940,           // 못 읽으면 null
  "state": "done",                                                   // pending | running | done | failed
  "analyzed_at": "2026-11-06T01:20:00Z", "error_message": "", "report_id": "028050_20261106_172435",   // 없으면 ""
  "decision_label": "보유", "score": 0.3, "agents_rated": 10, "agents_total": 10,
  "rank": null                                                       // 정리 전 null
}
```

## 화면 규칙 (설계 문서와 맞춘 부분)

- 처리 시작: 범위 안 미완료 개수 × 5분으로 예상 시간 표시. 처리 중이거나 범위에 할 일이 없으면 버튼을 막고 이유를 적는다. 서버에 취소가 없다는 안내를 띄운다.
- 정리: 대기·처리 중이 하나라도 있으면 막고 "남은 N개"를 보여준다. 실패는 남은 개수에 넣지 않는다.
- 순위 낡음: `rank_stale` 이면 표 위 안내 띠 + 순위 칸에 "낡음".
- 색: 등급·점수는 국내 관례(매수·+ 빨강, 매도·− 파랑). 수급 점은 색과 모양(채움·테두리·점선)을 같이 바꾼다.
- 보고서 번호: TradingAgents-KR 의 `report_id`(예: `028050_20261106_172435`)를 보여준다. `config.reportUrlTemplate` 에 `{report_id}` 가 들어간 주소를 정하면 그 보고서 화면으로 가는 링크가 된다
  (예: `http://localhost:4321/report?id={report_id}`). 정하지 않으면 글자로만 보인다. 서버의 실행 번호(`run_id`)는 나오지 않는다.
