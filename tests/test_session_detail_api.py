"""세션 전체 조회 API (#23). 웹 API 경계에서 검증한다.

저장소에 데이터를 넣어 두고(`repo`) 같은 테스트 DB 를 웹 API 로 조회한다.
"""

import uuid
from datetime import UTC, date, datetime

import pytest
from fakes import make_externals, repo_config
from fastapi.testclient import TestClient

from krx_screener.api import Dependencies, create_app
from krx_screener.db import DatabaseError
from krx_screener.repository import Item

# 화면(frontend/README.md)이 기대하는 종목 한 줄의 키
ITEM_KEYS = {
    "seq", "code", "name", "market", "sector_code", "sector_name", "description", "is_caution", "is_foreign",
    "close", "avg_trading_value", "rsi", "flow_status", "foreign_net_5d", "institution_net_5d", "state",
    "analyzed_at", "error_message", "report_id", "decision_label", "score", "agents_rated", "agents_total", "rank",
}


def seed(repo, as_of=date(2026, 11, 5)):
    session = repo.create_session(as_of)
    repo.add_items(
        session.id,
        [
            Item(
                seq=2, code="207940", name="삼성바이오로직스", market="KOSPI", flow_status="unconfirmed",
            ),
            Item(
                seq=1, code="028050", name="삼성E&A", market="KOSPI", close=64800, avg_trading_value=201_712_536_740,
                rsi=67.2, flow_status="partial", foreign_net_5d=-624_606, institution_net_5d=645_940,
                sector_code="26", sector_name="건설", description="플랜트 전 분야의 종합 솔루션을 제공하는 EPC 전문기업",
            ),
            Item(seq=3, code="030530", name="원익홀딩스", market="KOSDAQ", flow_status="strong", is_caution=True),
            Item(seq=4, code="064400", name="LG씨엔에스", market="KOSPI", flow_status="weak", is_foreign=True),
        ],
    )
    return session


def get(client, session_id):
    return client.get(f"/api/sessions/{session_id}")


def by_code(body):
    return {i["code"]: i for i in body["items"]}


# --- 모양 ---


def test_a_session_comes_back_whole_in_seq_order_with_the_agreed_keys(client, repo):
    session = seed(repo)
    response = get(client, session.id)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"session", "processing", "ranked_at", "rank_stale", "counts", "items"}
    assert [i["seq"] for i in body["items"]] == [1, 2, 3, 4]  # 넣은 순서와 상관없이 순번 순
    assert all(set(i) == ITEM_KEYS for i in body["items"])
    assert body["session"] == {
        "id": str(session.id), "as_of": "2026-11-05", "created_at": body["session"]["created_at"], "total": 4,
    }
    assert body["session"]["created_at"].endswith("Z")


def test_values_and_empty_values_are_shown_as_agreed(client, repo):
    body = get(client, seed(repo).id).json()
    first = by_code(body)["028050"]
    assert (first["name"], first["market"], first["close"], first["rsi"]) == ("삼성E&A", "KOSPI", 64800, 67.2)
    assert first["avg_trading_value"] == 201_712_536_740
    assert (first["sector_code"], first["sector_name"]) == ("26", "건설")
    assert (first["foreign_net_5d"], first["institution_net_5d"]) == (-624_606, 645_940)
    unread = by_code(body)["207940"]  # 수급을 읽지 못한 종목: 합계는 0 이 아니라 null
    assert (unread["flow_status"], unread["foreign_net_5d"], unread["institution_net_5d"]) == ("미확인", None, None)
    assert (unread["close"], unread["rsi"], unread["analyzed_at"], unread["score"], unread["rank"]) == (None,) * 5


def test_the_marks_and_korean_flow_labels(client, repo):
    items = by_code(get(client, seed(repo).id).json())
    assert items["030530"]["is_caution"] is True and items["030530"]["is_foreign"] is False
    assert items["064400"]["is_foreign"] is True
    assert {code: items[code]["flow_status"] for code in items} == {
        "028050": "부분", "207940": "미확인", "030530": "양호", "064400": "약함",
    }


@pytest.mark.parametrize(
    ("decision", "label"),
    [("Buy", "매수"), ("Overweight", "비중 확대"), ("Hold", "보유"), ("Underweight", "비중 축소"), ("Sell", "매도"),
     ("REVIEW", "REVIEW")],
)
def test_the_decision_is_shown_in_korean(client, repo, decision, label):
    session = seed(repo)
    repo.mark_done(session.id, "028050", decision=decision, score=0.3, agents_rated=10, agents_total=10)
    assert by_code(get(client, session.id).json())["028050"]["decision_label"] == label


def test_a_finished_stock_shows_its_result(client, repo):
    session = seed(repo)
    when = datetime(2026, 11, 6, 1, 20, tzinfo=UTC)
    repo.mark_done(
        session.id, "028050", decision="Hold", score=0.3, agents_rated=9, agents_total=10,
        report_id="028050_20261106_172435", analyzed_at=when,
    )
    done = by_code(get(client, session.id).json())["028050"]
    assert (done["state"], done["decision_label"], done["score"]) == ("done", "보유", 0.3)
    assert (done["agents_rated"], done["agents_total"]) == (9, 10)
    assert (done["report_id"], done["analyzed_at"]) == ("028050_20261106_172435", "2026-11-06T01:20:00Z")


def test_a_failed_stock_shows_the_reason(client, repo):
    session = seed(repo)
    repo.mark_failed(session.id, "207940", "시간 초과 (900초)")
    failed = by_code(get(client, session.id).json())["207940"]
    assert (failed["state"], failed["error_message"]) == ("failed", "시간 초과 (900초)")
    assert failed["decision_label"] == ""  # 아직 등급이 없으면 빈 문자열


# --- 개수와 상태 ---


def test_counts_cover_every_state(client, repo):
    session = seed(repo)
    assert get(client, session.id).json()["counts"] == {"pending": 4, "running": 0, "done": 0, "failed": 0}
    repo.mark_done(session.id, "028050", decision="Hold", score=0.0, agents_rated=10, agents_total=10)
    repo.mark_failed(session.id, "207940", "x")
    repo.mark_running(session.id, "030530")
    assert get(client, session.id).json()["counts"] == {"pending": 1, "running": 1, "done": 1, "failed": 1}


def test_ranking_fields_are_empty_until_a_ranking_is_made(client, repo, pg_conn):
    session = seed(repo)
    body = get(client, session.id).json()
    assert (body["ranked_at"], body["rank_stale"]) == (None, False)
    pg_conn.execute("UPDATE sessions SET ranked_at = '2026-11-06T02:00:00Z' WHERE id = %s", (session.id,))
    assert get(client, session.id).json()["ranked_at"] == "2026-11-06T02:00:00Z"


# --- 어떤 세션을 보여주는가 ---


def test_a_session_just_made_through_the_api_can_be_viewed_whole(client):
    created = client.post("/api/sessions", json={"as_of": "2026-11-05"}).json()
    body = get(client, created["id"]).json()
    assert body["session"] == created  # 만들 때 받은 값과 같다
    assert body["counts"] == {"pending": 3, "running": 0, "done": 0, "failed": 0}
    first = by_code(body)["028050"]
    # 목록(#14)·수급(#15)·업종(#16)이 한 응답에 모여 있고, 아직 처리하지 않은 종목이다
    assert (first["state"], first["flow_status"], first["sector_name"]) == ("pending", "양호", "건설")
    assert (first["foreign_net_5d"], first["institution_net_5d"]) == (500, 250)
    assert (first["decision_label"], first["score"], first["rank"], first["report_id"]) == ("", None, None, "")


def test_latest_is_the_most_recent_session_and_sessions_stay_apart(client, repo):
    older = seed(repo, date(2026, 11, 4))
    newer = seed(repo, date(2026, 11, 5))
    repo.mark_done(older.id, "028050", decision="Buy", score=1.0, agents_rated=10, agents_total=10)
    latest = get(client, "latest").json()
    assert latest["session"]["id"] == str(newer.id)
    assert latest["counts"]["done"] == 0  # 다른 세션의 처리 결과가 섞이지 않는다
    assert get(client, older.id).json()["counts"]["done"] == 1


def test_an_empty_session_is_a_valid_answer(client, repo):
    session = repo.create_session(date(2026, 11, 5))
    body = get(client, session.id).json()
    assert (body["items"], body["session"]["total"]) == ([], 0)
    assert body["counts"] == {"pending": 0, "running": 0, "done": 0, "failed": 0}


# --- 오류 ---


@pytest.mark.parametrize("session_id", [str(uuid.uuid4()), "abc", "12345"])
def test_an_unknown_or_malformed_session_is_a_korean_404(client, session_id):
    response = get(client, session_id)
    assert response.status_code == 404
    assert response.json() == {"detail": "세션을 찾지 못했습니다."}


def test_latest_without_any_session_says_to_make_one(client):
    response = get(client, "latest")
    assert response.status_code == 404
    assert response.json() == {"detail": "세션이 없습니다. 먼저 새 목록을 뽑으세요."}


def test_when_the_database_is_down_it_is_a_korean_503(tmp_path):
    def broken():
        raise DatabaseError("PostgreSQL 에 연결하지 못했습니다 (127.0.0.1:1/krx).")

    deps = Dependencies(connect=broken, externals=make_externals(tmp_path), config=repo_config())
    response = TestClient(create_app(deps)).get("/api/sessions/latest")
    assert response.status_code == 503
    assert response.json() == {"detail": "PostgreSQL 에 연결하지 못했습니다 (127.0.0.1:1/krx)."}
