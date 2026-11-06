# krx-screener

코스피·코스닥 전종목에서 **모델 없이 규칙만으로** 분석해 볼 만한 후보를 고르는 CLI.
종목 하나를 깊게 분석하는 [TradingAgents-KR](../TradingAgents-KR) 에 넘길 후보 목록을 만드는 것이 목적이다.

> 상태: 1단계·1.5단계 완료. 3단계(`analyze`)는 구현됐고 후보 일부로 시범 실행 중이다.

## 단계

| 단계 | 내용 | 상태 |
|---|---|---|
| 유니버스 | 전종목에서 우선주·스팩·관리종목·거래정지 의심·신규상장·리츠 제외 | 완료 |
| 1단계 | 시세 기반 규칙 필터 (거래대금, 20/60일선). RSI 는 값만 저장하고 조건으로 쓰지 않음 | 완료 |
| 1.5단계 | 후보에만 수급 조회(네이버 증권), 표시만 하고 거르지 않음. 실패 시 "수급 미확인" | 완료 |
| 2단계 | 가벼운 모델로 훑기 | 범위 밖 (후보가 너무 많을 때만) |
| 3단계 | TradingAgents-KR API 로 전체 분석, 최종 등급이 매수·비중 확대인 종목 모으기 | 구현됨 (시범 실행 중) |

## 사용

```bash
uv venv --python 3.13 && uv pip install -e ".[dev]"
# run·analyze·list 는 분석 대기열(PostgreSQL)을 쓴다: 아래 "웹 화면" 의 DB 준비(docker compose up -d, 환경변수, db init)를 먼저 한다
.venv/bin/krx-screener universe          # 전종목 목록의 종류별 개수
.venv/bin/krx-screener run               # 1단계 필터 실행 (기본 기준일: 직전 영업일, 약 1~2분)
.venv/bin/krx-screener run --date 2026-11-05
.venv/bin/krx-screener analyze --offset 0 --count 20   # 목록 1~20번만 TradingAgents-KR 로 분석 (쿼터 사용)
.venv/bin/krx-screener analyze --offset 20 --count 20  # 이어서 21~40번 (--offset 은 건너뛸 개수)
```

### 웹 화면 (세션 기반 스크리닝)

```bash
docker compose up -d                                  # 개발용 PostgreSQL (127.0.0.1:5433)
export KRX_SCREENER_DATABASE_URL=postgresql://krx:krx@127.0.0.1:5433/krx_screener
.venv/bin/python -m krx_screener.db init              # 스키마 만들기 (여러 번 해도 안전)
.venv/bin/krx-screener serve                          # http://127.0.0.1:8100/ 에서 화면과 API 를 함께 연다
# 백엔드 없이 화면만 보기: http://127.0.0.1:8100/?mock=1
```

`serve` 는 화면(`frontend/`)과 웹 API(`/api/...`)를 **한 주소**에서 연다. 로그인이 없어서 기본은 이 컴퓨터(`127.0.0.1`)에서만 열리고,
다른 주소로 열려면 `--allow-remote` 가 필요하다 (접속한 누구나 분석 = 쿼터 사용을 시작할 수 있다). 분석은 TradingAgents-KR 서버
(`config.yaml` 의 `analysis.base_url`)가 **같은 컴퓨터**에 떠 있어야 하고, 에이전트별 등급은 그 서버의 로그 폴더
(`TRADINGAGENTS_RESULTS_DIR`, 기본 `~/.tradingagents/logs`)에서 읽는다. DB 가 아직 없어도 서버는 뜨고 `/api/health` 가 한국어로 알려준다.

결과는 `results/<기준일>/` 에 저장된다.
- `candidates.csv`: 후보 (종목코드, 종목명, 종가, 평균 거래대금(근사), 20/60일선, RSI, 투자주의환기·외국기업 표시,
  최근 5거래일 외국인·기관 순매수 합계(주), 수급 상태 `양호`/`부분`/`약함`/`미확인`)
- `run.json`: 그날 쓴 설정, 단계별 종목 수, 탈락 사유별 개수, 판정 불가 종목과 사유

`analyze` 는 TradingAgents-KR 로컬 서버(`config.yaml` 의 `analysis.base_url`)가 떠 있어야 하고, 종목마다 몇 분이 걸리며 Max 플랜 쿼터를 쓴다. 그래서 `--count` 가 필수이고 실행 전에 확인을 묻는다 (`--yes` 로 생략). 후보를 거래대금 큰 순서로 한 개씩 보내고, 결과는 `results/<기준일>/analysis.csv` 에 종목마다 저장한다. 후보 목록과 상태는 분석 대기열(PostgreSQL, 환경변수 `KRX_SCREENER_DATABASE_URL`)에 적고, 멈췄다가 다시 실행하면 끝나지 않은 종목부터 이어서 한다. 대기열은 기준일마다 웹 화면의 세션 하나로도 보이고(점수·보고서 번호·순위도 웹과 같이 저장된다), 웹 화면이나 다른 `analyze` 가 같은 목록을 처리 중이면 시작하지 않는다. `krx-screener list` 는 대기열의 전체 목록과 처리 여부를 보여준다 (`--state pending|running|done|failed|skipped`). 후보에서 빠진 끝난 분석은 지우지 않고 보관한다. 이미 끝난 종목은 다시 보내지 않는다 (`--redo` 로 다시 분석하며, 실패해도 기존 등급은 지우지 않는다). `--dry-run` 은 서버에 연결하지 않고 보낼 목록과 예상 시간만 보여준다. 영숫자 종목코드(예: `0126Z0`)는 서버가 거절해서 보내지 않고 "건너뜀" 으로 기록한다.

시세는 `cache/` 에 기준일별로 저장되어 같은 기준일로 다시 돌리면 네트워크를 쓰지 않는다 (`--refresh` 로 다시 받음).

## 읽을 때 주의

- 평균 거래대금은 **종가 × 거래량으로 근사한 값**이다. 실제 거래대금이 아니다.
- 수급은 네이버 증권(비공식 경로)의 **수량(주)** 이고 금액이 아니다. 최근 10거래일만 제공되어 기준일이 그보다 과거면 전부 "미확인"이다. "미확인"은 수급 0이 아니다.
- 종목 종류(소속부·상장일)는 조회 시점 기준이고, 신규상장 60거래일은 평일 수로 센 근사값이다.
- 필터 임계값(`config.yaml`)은 백테스트로 정한 값이 아니다. "후보 수가 감당 가능한가"만 보고 정한 시작값이다.
- 3단계의 등급은 모델 분석의 결론이며 투자 성과를 보증하지 않는다. 후보 목록으로 읽는다.
- 후보는 **등급이 아니다.** 분석해 볼 만한 종목이라는 뜻일 뿐, 매수 판단이 아니다.
- 필터를 통과하지 못한 종목 중에 실제 분석에서는 좋은 등급이 나왔을 종목이 있을 수 있다.

용어는 [GLOSSARY.md](GLOSSARY.md), 작업 방식은 [CLAUDE.md](CLAUDE.md) 를 본다.
