"""DB 를 쓰는 테스트의 공통 준비.

테스트 전용 DB 가 없으면 이 픽스처를 쓰는 테스트는 건너뛴다 (기본 ``pytest`` 는 DB 없이도 통과한다).
테스트 전용 DB 는 ``docker compose up -d`` 로 띄우고 다음 환경변수를 준다.

    KRX_SCREENER_TEST_DATABASE_URL=postgresql://krx:krx@127.0.0.1:5433/krx_screener_test

주의: 이 픽스처는 매번 ``public`` 스키마를 통째로 비운다. 테스트 전용 DB 를 가리킬 때만 안전하다.
"""

import os
from datetime import date

import pytest
from fakes import make_externals, repo_config
from fastapi.testclient import TestClient

from krx_screener import db
from krx_screener.api import Dependencies, create_app
from krx_screener.repository import SessionRepository

TEST_URL_ENV = "KRX_SCREENER_TEST_DATABASE_URL"


def _reset(conn) -> None:
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")


@pytest.fixture
def pg_conn():
    """스키마가 막 만들어진 깨끗한 테스트 DB 연결."""
    url = os.environ.get(TEST_URL_ENV, "")
    if not url:
        pytest.skip(f"{TEST_URL_ENV} 이 없음")
    conn = db.connect(url)
    conn.autocommit = True
    _reset(conn)
    db.init_schema(conn)
    yield conn
    _reset(conn)
    conn.close()


@pytest.fixture
def repo(pg_conn):
    return SessionRepository(pg_conn)


@pytest.fixture
def externals(tmp_path):
    """외부 경계의 가짜들 (기본 세계). 필요한 경계만 ``dataclasses.replace`` 로 바꿔 쓴다."""
    return make_externals(tmp_path / "state_logs")


@pytest.fixture
def client(pg_conn, externals):
    """웹 API 테스트 클라이언트: 진짜 테스트 DB + 가짜 외부 경계. 이 경계(HTTP)에서 동작을 검증한다."""
    url = os.environ[TEST_URL_ENV]
    # 오늘은 2026-11-09(월)로 고정한다: 가짜 시세의 마지막 거래일이 11-05(목)이다
    app = create_app(
        Dependencies(
            connect=lambda: db.connect(url),
            externals=externals,
            config=repo_config(),
            today=lambda: date(2026, 11, 9),
            retry_wait=0,
        )
    )
    return TestClient(app)


@pytest.fixture
def hold_lock(pg_conn):
    """세션의 처리 잠금을 다른 연결로 쥐는 함수. 돌려주는 연결을 닫으면 잠금이 풀린다 (처리 중이던 연결이 끊긴 상황)."""
    held = []

    def hold(session_id):
        conn = db.connect(os.environ[TEST_URL_ENV])
        conn.autocommit = True
        held.append(conn)
        assert SessionRepository(conn).try_lock(session_id)
        return conn

    yield hold
    for conn in held:
        conn.close()


@pytest.fixture
def queue_db(pg_conn, monkeypatch, tmp_path):
    """명령줄 분석 대기열용: 테스트 DB 를 환경변수로 가리키고, 분석 상태 로그 폴더를 임시 폴더로 둔다.

    명령(`run`·`analyze`·`list`)은 자기 연결을 열어 환경변수의 접속 정보로 DB 에 닿는다. 돌려주는 연결은 결과를 확인하는 데 쓴다.
    """
    monkeypatch.setenv(db.ENV_URL, os.environ[TEST_URL_ENV])
    monkeypatch.setenv("TRADINGAGENTS_RESULTS_DIR", str(tmp_path / "state_logs"))
    return pg_conn
