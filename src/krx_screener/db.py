"""PostgreSQL 연결과 스키마 (이슈 #9).

접속 정보는 환경변수 ``KRX_SCREENER_DATABASE_URL`` 로만 받는다 (코드·설정 파일에 넣지 않는다).
스키마는 버전이 붙은 SQL 을 순서대로 한 번씩 적용한다 (``schema_migrations`` 에 적용한 버전을 남긴다).

저장 범위: 세션 → 세션 안의 종목 → 종목별 에이전트 등급. **보고서 본문은 저장하지 않는다.**
상태·등급·수급 상태 같은 코드값은 영어(내부값)로 저장하고, 사용자에게 보여줄 때 한국어로 바꾼다.

개발용 DB 는 ``docker compose up -d`` 로 띄우고, 스키마는 다음으로 만든다.

    KRX_SCREENER_DATABASE_URL=postgresql://krx:krx@127.0.0.1:5433/krx_screener \\
        python -m krx_screener.db init
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

import psycopg
from psycopg.conninfo import conninfo_to_dict

ENV_URL = "KRX_SCREENER_DATABASE_URL"
EXAMPLE_URL = "postgresql://krx:krx@127.0.0.1:5433/krx_screener"

# 스키마를 바꾸는 중에 다른 프로세스가 같은 변경을 동시에 적용하지 못하게 하는 잠금 번호.
_MIGRATION_LOCK = 7_240_001


class DatabaseError(RuntimeError):
    """DB 에 연결하지 못했거나 접속 정보가 올바르지 않을 때. 메시지는 사용자에게 그대로 보여준다."""


# (버전, 이름, SQL). 적용한 뒤에는 고치지 않고 새 버전을 추가한다.
MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "초기 스키마: 세션, 세션 종목, 에이전트별 등급",
        """
        CREATE TABLE sessions (
            id          uuid        PRIMARY KEY,
            as_of       date        NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            ranked_at   timestamptz
        );
        CREATE INDEX sessions_created_at_idx ON sessions (created_at DESC);

        CREATE TABLE session_items (
            session_id          uuid             NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
            seq                 integer          NOT NULL CHECK (seq >= 1),
            code                text             NOT NULL,
            name                text             NOT NULL,
            market              text             NOT NULL,
            close               bigint,
            avg_trading_value   bigint,
            rsi                 double precision,
            flow_status         text             NOT NULL DEFAULT 'unconfirmed'
                CHECK (flow_status IN ('strong', 'partial', 'weak', 'unconfirmed')),
            foreign_net_5d      bigint,
            institution_net_5d  bigint,
            sector_code         text             NOT NULL DEFAULT '',
            sector_name         text             NOT NULL DEFAULT '',
            description         text             NOT NULL DEFAULT '',
            is_caution          boolean          NOT NULL DEFAULT false,
            is_foreign          boolean          NOT NULL DEFAULT false,
            state               text             NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'running', 'done', 'failed')),
            analyzed_at         timestamptz,
            error_message       text             NOT NULL DEFAULT '',
            report_id           text             NOT NULL DEFAULT '',
            decision            text             NOT NULL DEFAULT ''
                CHECK (decision IN ('', 'Buy', 'Overweight', 'Hold', 'Underweight', 'Sell', 'REVIEW')),
            score               double precision,
            agents_rated        integer          NOT NULL DEFAULT 0 CHECK (agents_rated >= 0),
            agents_total        integer          NOT NULL DEFAULT 0 CHECK (agents_total >= 0),
            rank                integer          CHECK (rank IS NULL OR rank >= 1),
            PRIMARY KEY (session_id, seq),
            UNIQUE (session_id, code)
        );
        CREATE INDEX session_items_state_idx ON session_items (session_id, state);

        CREATE TABLE item_agent_ratings (
            session_id  uuid NOT NULL,
            code        text NOT NULL,
            agent       text NOT NULL,
            rating      text NOT NULL
                CHECK (rating IN ('Buy', 'Overweight', 'Hold', 'Underweight', 'Sell')),
            PRIMARY KEY (session_id, code, agent),
            FOREIGN KEY (session_id, code)
                REFERENCES session_items (session_id, code) ON DELETE CASCADE
        );
        """,
    ),
    (
        2,
        "명령줄 대기열: 세션 출처(source), 종목 보관(archived), 분석 소요 시간(elapsed_seconds)",
        """
        -- 명령줄(run/analyze/list)의 분석 대기열은 기준일마다 source = 'cli' 세션 하나다 (웹 화면이 만든 세션과 섞이지 않는다)
        ALTER TABLE sessions ADD COLUMN source text NOT NULL DEFAULT 'web'
            CHECK (source IN ('web', 'cli'));
        CREATE UNIQUE INDEX sessions_cli_as_of_idx ON sessions (as_of) WHERE source = 'cli';

        -- 후보 목록에서 빠졌지만 끝난 분석(쿼터를 쓴 결과)은 지우지 않고 보관한다. 보관한 종목은 목록·개수·순위에 나오지 않는다
        ALTER TABLE session_items ADD COLUMN archived boolean NOT NULL DEFAULT false;

        -- 끝난 분석에 걸린 시간(초). 예상 시간을 재는 근거다
        ALTER TABLE session_items ADD COLUMN elapsed_seconds double precision
            CHECK (elapsed_seconds IS NULL OR elapsed_seconds >= 0);
        """,
    ),
    (
        3,
        "목록 만들기 진행: 세션의 만들기 상태(build_state)와 진행 숫자",
        """
        -- 새 목록은 세션을 먼저 만들고(building) 뒤에서 종목을 채운다. 기존 세션은 모두 완성된 것(ready)이다.
        -- '중단됨'(만들던 서버가 죽음)은 저장하지 않고 만드는 잠금이 없는 building 으로 계산한다.
        ALTER TABLE sessions ADD COLUMN build_state text NOT NULL DEFAULT 'ready'
            CHECK (build_state IN ('building', 'ready', 'failed'));
        ALTER TABLE sessions ADD COLUMN build_stage text NOT NULL DEFAULT '';      -- prices | flow | sector | ''
        ALTER TABLE sessions ADD COLUMN build_done integer NOT NULL DEFAULT 0 CHECK (build_done >= 0);
        ALTER TABLE sessions ADD COLUMN build_total integer NOT NULL DEFAULT 0 CHECK (build_total >= 0);
        ALTER TABLE sessions ADD COLUMN build_error text NOT NULL DEFAULT '';

        -- 같은 기준일 목록을 동시에 두 번 만들지 못하게 한다 (웹 세션만, 명령줄 대기열은 상관없다)
        CREATE UNIQUE INDEX sessions_building_as_of_idx ON sessions (as_of)
            WHERE build_state = 'building' AND source = 'web';
        """,
    ),
]


def database_url(env: Mapping[str, str] | None = None) -> str:
    """환경변수에서 접속 주소를 읽는다. 없으면 설정 방법을 알려 주는 오류를 낸다."""
    url = (env if env is not None else os.environ).get(ENV_URL, "").strip()
    if not url:
        raise DatabaseError(
            f"PostgreSQL 접속 정보가 없습니다. 환경변수 {ENV_URL} 에 접속 주소를 설정하세요 "
            f"(개발용 예: {EXAMPLE_URL})."
        )
    return url


def _where(url: str) -> str:
    """오류 메시지에 쓸 접속 위치(호스트·포트·DB 이름). 사용자 이름과 비밀번호는 넣지 않는다."""
    try:
        info = conninfo_to_dict(url)
    except psycopg.Error:
        return "알 수 없음"
    host = info.get("host") or "localhost"
    port = info.get("port") or "5432"
    return f"{host}:{port}/{info.get('dbname', '')}"


def connect(url: str | None = None, *, connect_timeout: int = 5) -> psycopg.Connection:
    """DB 에 연결한다. 실패하면 비밀번호 없이 원인을 알려 주는 한국어 오류를 낸다."""
    target = url or database_url()
    try:
        return psycopg.connect(target, connect_timeout=connect_timeout)
    except psycopg.OperationalError as exc:
        raise DatabaseError(
            f"PostgreSQL 에 연결하지 못했습니다 ({_where(target)}). "
            "서버가 켜져 있는지, 주소·사용자·비밀번호가 맞는지 확인하세요 "
            "(개발용 DB 는 `docker compose up -d`)."
        ) from exc
    except psycopg.Error as exc:
        raise DatabaseError(
            f"PostgreSQL 접속 주소 형식이 올바르지 않습니다 (예: {EXAMPLE_URL})."
        ) from exc


def init_schema(conn: psycopg.Connection) -> list[int]:
    """아직 적용하지 않은 스키마 버전을 순서대로 적용하고, 이번에 적용한 버전 목록을 돌려준다.

    여러 번 불러도 안전하다 (이미 적용한 버전은 건너뛴다). 한 버전은 한 트랜잭션으로 적용해서
    중간에 실패하면 그 버전은 적용되지 않은 상태로 남는다.
    """
    applied: list[int] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version integer PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for version, name, sql in MIGRATIONS:
            if version in done:
                continue
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (version, name) VALUES (%s, %s)", (version, name))
            applied.append(version)
    return applied


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["init"]:
        print("사용법: python -m krx_screener.db init", file=sys.stderr)
        return 2
    try:
        with connect() as conn:
            applied = init_schema(conn)
    except DatabaseError as exc:
        print(exc, file=sys.stderr)
        return 1
    if applied:
        print(f"스키마를 만들었습니다 (적용한 버전: {', '.join(map(str, applied))}).")
    else:
        print("스키마가 이미 최신입니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
