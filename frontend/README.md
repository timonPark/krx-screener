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
| 4' 정리 요약 보고서 | `GET /api/sessions/{id}/report` (`id` = `latest` 가능, `?format=md\|html`) | 첨부 파일 `krx-screener-summary-{기준일}.md`(기본, 차트는 텍스트 막대그래프, 끝에 종목별 결과 표) 또는 `?format=html`(SVG 차트 포함, `.html`) · 정리한 적이 없으면 `409 {"detail": "정리를 먼저 실행하세요. …"}` · `format` 이 `md`·`html` 이 아니면 `422` |
| 4'' 정리 요약 차트 | `GET /api/sessions/{id}/charts/{이름}.svg` (`id`에 `latest` 가능, 이름: `rating`·`flow`·`breadth`·`theme-score`·`theme-ratings`) | `image/svg+xml` (`Cache-Control: no-cache`) · 정리한 적이 없으면 `409` · 모르는 이름이거나 전종목 수치가 없는 목록의 `breadth` 면 `404` |
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
  "items": [Item, ...],         // 세션 전체, seq 순
  "summary": Summary | null     // 정리할 때 저장한 업종별 해석과 종합 평가. 정리한 적이 없으면 null
}

// Summary (정리하기 전에는 null). 등급·수급 분포의 키는 한국어이고 순서가 고정이다 (매수 → 매도, 양호 → 미확인)
{
  "created_at": "2026-11-08T10:35:04Z",          // 정리한 시각
  "market": {
    "scored_count": 114, "avg_score": -0.32,     // 순위에 들어간 후보 수, 평균 평가점수(저장값 −2 ~ +2). 점수가 있는 후보가 없으면 null
    "rating_counts": { "매수": 0, "비중 확대": 0, "보유": 27, "비중 축소": 87, "매도": 0 },
    "flow_counts": { "양호": 34, "부분": 70, "약함": 10, "미확인": 0 },
    "universe_stats": { "evaluated": 2000, "candidates": 116, "fail_each": { "above_ma20": 800 } },   // 목록을 만들 때 저장한 전종목 수치. 저장 전에 만든 목록은 null
    "assessment": "2026-11-05 기준 점수가 있는 후보 114개의 …"   // 수치로 만든 종합 평가 문장
  },
  "themes": [                                      // 후보가 많은 업종 순. 테마 = 네이버 업종
    { "code": "24", "name": "반도체와반도체장비", "item_count": 29, "avg_score": -0.42,
      "rating_counts": {…}, "flow_counts": {…}, "avg_rsi": 57.3,
      "foreign_net_5d": -3123385, "institution_net_5d": 2292171,   // 수급을 읽은 후보의 합(주). 읽은 후보가 없으면 null
      "interpretation": "반도체와반도체장비: 점수가 있는 후보 29개의 …" }
  ]
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
- 정리 요약: `summary` 가 있으면(정리한 뒤) 맨 아래에 "정리 요약" 카드를 보인다: 종합 평가 문장과 수치, 업종(테마)별 표와 해석 문장, 차트 5개(`<img>` 로 `GET /api/sessions/{id}/charts/{이름}.svg` 를 불러 보인다, 정리 시각을 주소에 붙여 다시 정리하면 새 그림을 받는다), "보고서 (.md)"·"보고서 (.html, 차트 포함)" 링크(`GET /api/sessions/{id}/report`, 둘째는 `?format=html`).
  차트는 서버가 SVG 로 한 번만 그리고 화면과 HTML 보고서가 같은 그림을 쓴다. 같은 정리면 조회가 돌아도 그림을 다시 만들지 않는다(깜빡임·재요청 방지). 전종목 수치가 없는 목록은 시장 폭 차트를 그리지 않는다.
  평가점수는 조회 표와 같이 저장값에 100 을 곱해 보이고, 해석 문장과 보고서는 저장값(−2 ~ +2)으로 적는다 (카드 아래에 그렇게 적는다). `rank_stale` 이면 카드 머리에 "낡음"이 붙는다.
  예시 모드(`?mock=1`)는 서버가 없어 보고서를 내려받을 수 없고 그렇다고 알린다.
- 색: 등급·점수는 국내 관례(매수·+ 빨강, 매도·− 파랑). 수급 점은 색과 모양(채움·테두리·점선)을 같이 바꾼다.
- 보고서 번호: TradingAgents-KR 의 `report_id`(예: `028050_20261106_172435`)를 보여준다. `config.reportUrlTemplate` 에 `{report_id}` 가 들어간 주소를 정하면 그 보고서 화면으로 가는 링크가 된다
  (예: `http://localhost:4321/report?id={report_id}`). 정하지 않으면 글자로만 보인다. 서버의 실행 번호(`run_id`)는 나오지 않는다.
