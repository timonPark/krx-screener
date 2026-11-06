# [#9] PostgreSQL 연결 설정과 스키마 만들기

- 이슈: https://github.com/timonPark/krx-screener/issues/9 (에픽 #2, 스펙 #1)
- 브랜치: feat/9-postgres-schema (feat/stage1-filter 에서 분기)
- 작업일: 2026-11-06

## 수행한 변경

- `src/krx_screener/db.py` 신규: 접속 주소를 환경변수 `KRX_SCREENER_DATABASE_URL` 로만 읽고, 연결하고,
  스키마를 만든다. `python -m krx_screener.db init` 으로 실행.
- 스키마(버전 1): `sessions`, `session_items`, `item_agent_ratings` + 적용 이력 `schema_migrations`.
  종목 줄에는 스펙의 Item 계약 값(수급·업종·처리 상태·보고서 번호·등급·점수·순위)을, 에이전트 줄에는
  에이전트별 등급을 둔다. **보고서 본문 칼럼은 없다.**
- `docker-compose.yml` + `docker/postgres/init/`: 개발용 PostgreSQL 16. 포트 5433, 127.0.0.1 에서만 접속,
  `krx_screener`(개발)와 `krx_screener_test`(테스트 전용) 두 데이터베이스.
- `pyproject.toml`: `psycopg[binary]>=3.2`. `.env.example`: 접속 주소 안내.
- `tests/test_db.py`: 24개 (DB 없이 7개, 테스트 DB 가 있을 때 17개).

## 설계 결정 (이슈 #9 범위 안에서 내가 정한 것)

- **환경변수 이름**: `KRX_SCREENER_DATABASE_URL`(개발), `KRX_SCREENER_TEST_DATABASE_URL`(테스트). 다른 프로젝트의
  `DATABASE_URL` 과 겹치지 않게 접두어를 붙였다.
- **마이그레이션 방식**: 버전이 붙은 SQL 을 순서대로 한 번씩 적용하고 `schema_migrations` 에 남긴다. 한 버전은
  한 트랜잭션이라 중간에 실패하면 적용되지 않은 상태로 남고, 적용 중에는 세션 잠금으로 동시 적용을 막는다.
  적용한 버전은 고치지 않고 새 버전을 추가한다. 외부 마이그레이션 도구는 쓰지 않았다 (표 3개 규모).
- **코드값은 영어로 저장**한다 (`pending/running/done/failed`, 수급 `strong/partial/weak/unconfirmed`, 등급
  `Buy/Overweight/…`). 스펙의 API 응답은 한국어(`flow_status`, `decision_label`)이므로 **API 계층(#23)이 변환**한다.
  i18n 경계(표준 출력 = 한국어, 내부 코드값 = 영어)를 따른 것이다. 허용값은 DB 의 CHECK 제약으로 막는다.
- **세션 id 는 DB 가 만들지 않고** 호출하는 쪽이 UUID 를 만든다 (확장 모듈에 기대지 않는다).
- 오류 메시지는 **호스트·포트·DB 이름만** 보여주고 사용자 이름과 비밀번호는 넣지 않는다 (테스트로 확인).
- 개발용 비밀번호(`krx`)는 로컬 개발 전용이고 포트를 127.0.0.1 에만 열었다.
- **`cli.py`·`README.md`·`CLAUDE.md` 는 이번 브랜치에서 건드리지 않았다.** 이 파일들에는 아직 커밋하지 않은
  이전 작업(SQLite 대기열)의 변경이 섞여 있어서, 고치면 그 변경이 이번 커밋에 같이 들어간다. 그래서 명령줄
  진입점은 `python -m krx_screener.db init` 별도 모듈로 두었다. 웹 서버(#17 이후)가 시작할 때 `init_schema` 를 부른다.

## 검증 결과

- `pytest` 전체: 테스트 DB 연결 시 **216 passed**, DB 없이 **199 passed, 17 skipped**
- `ruff check src tests` 통과
- 실제 개발 DB: `init` 첫 실행에 "적용한 버전: 1", 두 번째는 "이미 최신", 테이블 4개 확인, 접속 정보 없이
  실행하면 한국어 오류와 종료 코드 1.
- 테스트가 확인하는 것: 빈 DB 에서 스키마 생성, 반복 실행 안전, Item 계약 칼럼 존재, 본문 칼럼 없음, 기본값,
  잘못된 값(상태·수급·등급·순번·순위·에이전트 수)을 DB 가 거절, 세션 안에서 종목 코드 중복 거절(다른 세션은 허용),
  세션이 없으면 종목 거절, 에이전트 등급의 외래키·허용값·중복, 세션 삭제 시 종목·등급만 같이 삭제, 실패한 변경이
  흔적을 남기지 않음.

## 알려진 한계

- 테스트 DB 픽스처는 매번 `public` 스키마를 통째로 비운다. 테스트 전용 DB(`krx_screener_test`)를 가리킬 때만
  안전하며, 개발 DB 를 가리키면 데이터가 지워진다 (`.env.example` 에 적어 두었다).
- 도커 컨테이너 안의 데이터는 `docker compose down -v` 로 볼륨까지 지우면 사라진다.

## 다음 작업 핸드오프

- #10 저장소 계층, #11 API 테스트 기반 (테스트 DB 연결 방식은 이번 `test_db.py` 의 `conn` 픽스처를 참고).
- 이 브랜치는 아직 푸시하지 않았고 PR 도 열지 않았다.
