"""세션 생성·목록 API (#13). 웹 API 경계에서 검증한다.

DB 가 필요한 테스트는 테스트 전용 DB 가 있을 때만 돈다 (conftest 참고). 오늘은 2026-11-09(월)로 고정이고
가짜 시세의 마지막 거래일은 2026-11-05(목)다.
"""

import uuid
from dataclasses import replace
from datetime import date, datetime

import pytest
from fakes import FakePrices, make_externals, repo_config
from fastapi.testclient import TestClient

from krx_screener.api import Dependencies, create_app
from krx_screener.db import DatabaseError


def _count(pg_conn) -> int:
    return pg_conn.execute("SELECT count(*) FROM sessions").fetchone()[0]


# --- 만들기 (테스트 DB 필요) ---


def test_creating_a_session_returns_it_and_stores_it(client, pg_conn):
    response = client.post("/api/sessions", json={"as_of": "2026-11-05"})
    assert response.status_code == 202  # 목록은 뒤에서 만든다 (#56)
    body = response.json()
    assert set(body) == {"id", "as_of", "created_at", "total", "build", "criteria"}
    assert (body["as_of"], body["total"], body["build"]["state"]) == ("2026-11-05", 0, "building")  # 응답은 만들기를 시작한 모습
    finished = client.get(f"/api/sessions/{body['id']}").json()["session"]
    assert (finished["total"], finished["build"]["state"]) == (3, "ready")  # 기본 세계의 세 종목이 모두 목록에 들어간다

    row = pg_conn.execute("SELECT id, as_of FROM sessions").fetchone()
    assert (str(row[0]), row[1]) == (body["id"], date(2026, 11, 5))


def test_id_is_a_uuid_and_the_time_is_utc(client):
    body = client.post("/api/sessions", json={"as_of": "2026-11-05"}).json()
    assert uuid.UUID(body["id"]).version == 4
    assert body["created_at"].endswith("Z")
    datetime.fromisoformat(body["created_at"].replace("Z", "+00:00"))  # 읽을 수 있는 시각


def test_two_sessions_on_the_same_day_are_separate(client, pg_conn):
    first = client.post("/api/sessions", json={"as_of": "2026-11-05"}).json()
    second = client.post("/api/sessions", json={"as_of": "2026-11-05"}).json()
    assert first["id"] != second["id"]
    assert _count(pg_conn) == 2


# --- 목록 (테스트 DB 필요) ---


def test_list_is_empty_at_first(client):
    response = client.get("/api/sessions")
    assert (response.status_code, response.json()) == (200, [])


def test_list_is_most_recent_first_and_has_the_same_shape(client):
    older = client.post("/api/sessions", json={"as_of": "2026-11-04"}).json()
    newer = client.post("/api/sessions", json={"as_of": "2026-11-05"}).json()
    listed = client.get("/api/sessions").json()
    assert [s["id"] for s in listed] == [newer["id"], older["id"]]
    assert set(listed[0]) == set(newer)  # 만들 때 받은 값과 같은 모양
    assert (listed[0]["id"], listed[0]["as_of"], listed[0]["created_at"]) == (newer["id"], newer["as_of"], newer["created_at"])
    assert (listed[0]["total"], listed[0]["build"]["state"]) == (3, "ready")  # 값은 다 만든 뒤의 것이다


# --- 잘못된 기준일은 한국어 메시지로 거절되고 아무것도 남지 않는다 (테스트 DB 필요) ---


@pytest.mark.parametrize("as_of", ["2026/11/05", "내일", "2026-13-40"])
def test_wrong_date_format_is_a_korean_422(client, pg_conn, as_of):
    response = client.post("/api/sessions", json={"as_of": as_of})
    assert response.status_code == 422
    assert response.json() == {"detail": "기준일 형식이 올바르지 않습니다 (예: 2026-11-05)."}
    assert _count(pg_conn) == 0


def test_missing_date_is_a_korean_422(client, pg_conn):
    response = client.post("/api/sessions", json={})
    assert response.status_code == 422
    assert response.json() == {"detail": "기준일(as_of)을 입력하세요 (예: 2026-11-05)."}
    assert _count(pg_conn) == 0


def test_a_body_that_is_not_json_is_a_korean_422(client):
    response = client.post("/api/sessions", content="not json", headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.json() == {"detail": "요청 형식이 올바르지 않습니다."}


def test_a_future_date_is_rejected(client, pg_conn):
    response = client.post("/api/sessions", json={"as_of": "2026-11-10"})  # 오늘(11-09) 다음 날
    assert response.status_code == 422
    assert response.json() == {"detail": "기준일 2026-11-10 은 오늘(2026-11-09) 이후라 시세가 없습니다."}
    assert _count(pg_conn) == 0


@pytest.mark.parametrize("as_of", ["2026-11-07", "2026-11-06"])  # 토요일, 그리고 시세가 아직 없는 금요일
def test_a_day_without_prices_is_rejected_with_the_nearest_trading_day(client, pg_conn, as_of):
    response = client.post("/api/sessions", json={"as_of": as_of})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "거래일이 아니거나 시세가 아직 없습니다" in detail
    assert "가까운 이전 거래일: 2026-11-05" in detail
    assert _count(pg_conn) == 0


# --- DB 없이 도는 테스트 ---


def _app(tmp_path, connect, **overrides):
    externals = replace(make_externals(tmp_path), **overrides)
    deps = Dependencies(
        connect=connect, externals=externals, config=repo_config(), today=lambda: date(2026, 11, 9), retry_wait=0
    )
    return TestClient(create_app(deps))


def test_when_trading_days_cannot_be_checked_it_is_a_503_not_a_422(tmp_path):
    # 삼성전자 시세를 못 받는다 = 입력 잘못이 아니라 외부 장애
    client = _app(tmp_path, lambda: None, prices=FakePrices({}, failing=["005930"]))
    response = client.post("/api/sessions", json={"as_of": "2026-11-05"})
    assert response.status_code == 503
    assert "거래일을 확인하지 못했습니다" in response.json()["detail"]


def test_when_the_database_is_down_both_addresses_are_a_korean_503(tmp_path):
    def broken():
        raise DatabaseError("PostgreSQL 에 연결하지 못했습니다 (127.0.0.1:1/krx).")

    client = _app(tmp_path, broken)
    expected = {"detail": "PostgreSQL 에 연결하지 못했습니다 (127.0.0.1:1/krx)."}
    for response in (client.get("/api/sessions"), client.post("/api/sessions", json={"as_of": "2026-11-05"})):
        assert response.status_code == 503
        assert response.json() == expected
