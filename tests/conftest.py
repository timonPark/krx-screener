"""DB 를 쓰는 테스트의 공통 준비.

테스트 전용 DB 가 없으면 이 픽스처를 쓰는 테스트는 건너뛴다 (기본 ``pytest`` 는 DB 없이도 통과한다).
테스트 전용 DB 는 ``docker compose up -d`` 로 띄우고 다음 환경변수를 준다.

    KRX_SCREENER_TEST_DATABASE_URL=postgresql://krx:krx@127.0.0.1:5433/krx_screener_test

주의: 이 픽스처는 매번 ``public`` 스키마를 통째로 비운다. 테스트 전용 DB 를 가리킬 때만 안전하다.
"""

import os

import pytest

from krx_screener import db
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
