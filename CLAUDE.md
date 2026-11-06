# CLAUDE.md

krx-screener 작업 지침서. 코스피·코스닥 전종목에서 규칙만으로 분석 후보를 고르고, 나눠서 분석해 평가점수로 순위를 매기는 Python 도구 (CLI + 웹 API·화면).
`/Users/2309-n0018/Project/TradingAgents-KR` 의 작업 방식을 참고하되 규모에 맞게 줄였다.
설계 배경은 `TradingAgents-KR/docs/screener-project-proposal.md`.

## 프로젝트 요지

- 1단계: 시세만으로 계산하는 규칙 필터 (모델 미사용, 계정 불필요). 기준은 `config.yaml`.
- 1.5단계: 1단계 통과 후보에만 수급 조회 (네이버 증권, 로그인 불필요). **표시만** 하고 후보를
  거르지 않는다 (`config.yaml` 의 `flow.mode`). 읽지 못하면 탈락시키지 않고 [[수급 미확인]].
- 3단계: 후보를 TradingAgents-KR 로컬 서버 API(`POST /api/runs`)로 보내 등급을 모은다 (명령줄 `analyze`, 웹 화면의 "처리 시작").
  **쿼터를 쓰므로** `--count` 필수(`--offset` 으로 구간 지정), 실행 전 확인, 한 번에 하나씩, 끝난 종목은 재전송 안 함.
  끝난 직후 서버의 분석 상태 로그에서 에이전트별 등급을 읽어 **평가점수**(등급을 낸 에이전트의 평균)를 저장한다.
- 웹: 세션 기반 API(`/api/sessions`: 목록 뽑기 → 나눠서 처리 → 조회 → 정리)와 화면(`frontend/`)을 `krx-screener serve` 가 한 주소에서 연다.
  같은 세션은 동시에 처리하지 않는다(세션 잠금). 로그인이 없어 기본은 이 컴퓨터(`127.0.0.1`)에서만 연다. 설계는 `docs/backend-design.md`.
- 2단계(가벼운 모델)는 범위 밖 (후보가 너무 많을 때만).
- 1단계 결과는 `results/YYYY-MM-DD/` 의 `candidates.csv` + `run.json` (파일).
- 3단계 분석 상태는 DB 대기열에 둔다. 저장소는 PostgreSQL (`docs/adr/0001-postgres-analysis-queue.md`,
  접속 정보는 환경변수 `KRX_SCREENER_DATABASE_URL`)이고, 명령줄 대기열은 기준일마다 `source = 'cli'` 세션 하나다
  (`src/krx_screener/analysis_queue.py`, 웹 화면과 같은 저장소).
  `analysis.csv` 는 DB 에서 내보낸 사본이다. `--redo` 가 실패해도 기존 등급은 지우지 않는다.
- 실행은 수동. `--date` 기본값은 직전 영업일, 시세는 날짜별로 캐시한다.
- 용어는 `GLOSSARY.md`. 특히 **후보** 는 **등급** 이 아니고, **평가점수** 는 **등급** 이 아니다.

## 작업 규칙

- 브랜치 1개 = 작업 1개. 이름: `{type}/{slug}` (type: feat, fix, refactor, test, docs, chore).
- `main` 직접 커밋·푸시 금지 (초기 골격 커밋 제외). 머지는 사용자 승인 후.
- 브랜치마다 `work_history/yyyy-mm-dd-{slug}.md` 를 만들어 작업과 함께 커밋한다.
  내용: 수행한 변경 / 설계 결정 변경 사항 / 검증 결과 / 다음 작업 핸드오프.
- 커밋은 한 커밋 = 한 논리적 변경. 메시지는 한국어, 첫 줄 50자 이내, 본문에 "왜".
  `--no-verify`·`--amend`·강제 푸시 금지. `git add .` 대신 파일을 명시한다.
- `.env`·자격증명·`results/`·`cache/` 는 커밋하지 않는다.
- 커밋 전 `ruff check src tests` 와 관련 `pytest` 통과.

## i18n 경계

- 사용자에게 보이는 텍스트(터미널 출력, 에러 메시지, 문서, 커밋 메시지) = **한국어**.
- 코드 식별자·환경변수·CLI 플래그·CSV 컬럼 키·설정 키 = **영어**.

## 설계 제약

- TradingAgents-KR 코드에 **의존하지 않는다**. 복사하지도 않는다. 같은 라이브러리(pykrx,
  FinanceDataReader)를 직접 부르고, TradingAgents-KR 는 "어떤 호출이 동작하는가" 를 확인하는
  참고용으로만 읽는다.
- 종목코드는 숫자 6자리가 아니다. `0126Z0` 같은 영숫자 코드(94개)와 `00680K` 같은 우선주가
  있다. 코드는 문자열로 다루고 `\d{6}` 을 가정하지 않는다.
- TradingAgents-KR 는 HTTP API 로만 연결한다 (코드 의존 금지). 서버에는 취소 API 가 없고 `run_id`
  상태는 서버 메모리에만 있다. 서버는 숫자 6자리 종목코드만 받아서 영숫자 코드는 보내지 않는다.
  `run_id` 는 저장·노출하지 않고, 보고서는 `report_id`(서버가 이벤트로 알려 준다)로 가리킨다.
  에이전트별 등급은 서버와 **같은 컴퓨터**의 분석 상태 로그 파일에서 읽는다 (API 로는 받을 수 없다).
- 데이터가 없다는 이유로 후보를 버리지 않는다 (수급 미확인으로 표시하고 통과).
- 필터 임계값은 백테스트로 정한 값이 아니다. 후보 수가 감당 가능한지만 보고 조정한다.
- 수급은 네이버 증권 비공식 경로(최근 10거래일, 수량(주) 기준)만 쓴다. KRX 로그인은 만들지
  않는다 (계정 잠금 위험, 복잡도). 빈 응답을 "수급 0" 으로 읽지 않는다 — 없는 종목코드도 HTTP 200
  에 빈 목록을 준다. 기준일이 10거래일보다 과거면 전부 미확인이다.
- 유니버스 제외 규칙은 종류별 개수를 확인한 뒤 확정한다 (미확정 상태).

## 참조

- `README.md` (실행 방법), `GLOSSARY.md`, `config.yaml`, `work_history/`
- `docs/backend-design.md` (백엔드 설계), `docs/frontend-spec.md` (화면 명세), `docs/api-testing.md` (테스트 방법), `docs/adr/` (결정 기록)
- `frontend/README.md` (화면 파일)
