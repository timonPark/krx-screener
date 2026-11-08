"""정리할 때 업종(테마)별 해석과 종합 평가를 DB 에 저장한다 (``POST /api/sessions/{id}/organize``).

세션은 저장소로 직접 만든다 (업종과 점수를 정확히 정해 둔다). 저장 위치: ``session_theme_summaries``, ``session_market_summaries``.
전종목 수치는 목록을 만들 때 ``sessions.universe_stats`` 에 저장해 두고 정리 때 쓴다.
"""

import json
import uuid
from datetime import date

import pytest

from krx_screener import summary
from krx_screener.repository import Item

AS_OF = date(2026, 11, 5)
STATS = {
    "listing_total": 2800, "after_static": 2600, "evaluated": 2000, "candidates": 4, "undetermined": 50,
    "zero_volume_excluded": 40, "fail_each": {"avg_trading_value": 1500, "above_ma20": 800, "above_ma60": 1000},
}


def make(repo, rows, universe_stats=None):
    """``rows`` = (순번, 업종코드, 업종명, 점수) 들. 점수가 None 이면 실패한 종목이다. 모두 끝난 상태로 만든다."""
    session = repo.create_session(AS_OF)
    repo.add_items(
        session.id,
        [
            Item(seq=seq, code=f"{seq:06d}", name=f"종목{seq}", market="KOSPI", sector_code=code, sector_name=name,
                 rsi=60.0, flow_status="partial", foreign_net_5d=100, institution_net_5d=200)
            for seq, code, name, _ in rows
        ],
    )
    for seq, _, _, score in rows:
        if score is None:
            repo.mark_failed(session.id, f"{seq:06d}", "분석 실패")
        else:
            decision = "Hold" if score >= 0 else "Underweight"
            repo.mark_done(session.id, f"{seq:06d}", decision=decision, score=score, agents_rated=10, agents_total=10)
    if universe_stats is not None:
        repo._conn.execute(
            "UPDATE sessions SET universe_stats = %s::jsonb WHERE id = %s",
            (json.dumps(universe_stats), session.id),
        )
    return session


ROWS = [
    (1, "26", "건설", 0.4),
    (2, "26", "건설", 0.2),
    (3, "33", "항공사", -0.5),
    (4, "33", "항공사", -0.7),
    (5, "99", "제약", None),  # 실패: 요약에서 빠진다
]


def organize(client, session_id):
    return client.post(f"/api/sessions/{session_id}/organize")


def themes(pg_conn, session_id):
    rows = pg_conn.execute(
        "SELECT theme_code, theme_name, item_count, avg_score, rating_counts, flow_counts, avg_rsi, "
        "foreign_net_5d, institution_net_5d, interpretation FROM session_theme_summaries "
        "WHERE session_id = %s ORDER BY theme_code",
        (session_id,),
    ).fetchall()
    keys = ("code", "name", "count", "avg", "ratings", "flows", "rsi", "foreign", "inst", "text")
    return {r[0]: dict(zip(keys, r, strict=True)) for r in rows}


def market(pg_conn, session_id):
    row = pg_conn.execute(
        "SELECT scored_count, avg_score, rating_counts, flow_counts, universe_stats, assessment "
        "FROM session_market_summaries WHERE session_id = %s",
        (session_id,),
    ).fetchone()
    return None if row is None else dict(zip(("scored", "avg", "ratings", "flows", "universe", "text"), row, strict=True))


# --- 저장 ---


def test_organizing_saves_one_row_per_sector_and_one_market_row(client, repo, pg_conn):
    session = make(repo, ROWS, STATS)
    assert organize(client, session.id).status_code == 200

    found = themes(pg_conn, session.id)
    assert set(found) == {"26", "33"}  # 실패한 종목의 업종(99)은 없다
    assert (found["26"]["name"], found["26"]["count"]) == ("건설", 2)
    assert found["26"]["avg"] == pytest.approx(0.3)
    assert found["26"]["ratings"]["Hold"] == 2
    assert found["33"]["avg"] == pytest.approx(-0.6)
    assert found["33"]["ratings"]["Underweight"] == 2
    assert (found["26"]["foreign"], found["26"]["inst"]) == (200, 400)
    assert "건설" in found["26"]["text"] and "평균 평가점수 +0.30" in found["26"]["text"]

    overall = market(pg_conn, session.id)
    assert overall["scored"] == 4
    assert overall["avg"] == pytest.approx((0.4 + 0.2 - 0.5 - 0.7) / 4)
    assert overall["ratings"] == {"Buy": 0, "Overweight": 0, "Hold": 2, "Underweight": 2, "Sell": 0}
    assert overall["universe"] == STATS
    assert "20일선 위 60.0%" in overall["text"]


def test_a_session_without_saved_whole_market_numbers_says_so(client, repo, pg_conn):
    session = make(repo, ROWS)  # 전종목 수치 없음 (이 기능 전에 만든 목록)
    organize(client, session.id)
    overall = market(pg_conn, session.id)
    assert overall["universe"] is None
    assert "전종목 수치가 저장되어 있지 않" in overall["text"]


def test_organizing_again_replaces_the_previous_summaries(client, repo, pg_conn):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    before = themes(pg_conn, session.id)
    assert before["33"]["count"] == 2

    # 다시 분석해서 점수가 바뀌고 한 종목이 실패로 바뀐 뒤 다시 정리한다
    repo.mark_done(session.id, "000003", decision="Hold", score=0.9, agents_rated=10, agents_total=10)
    repo.mark_failed(session.id, "000004", "다시 분석 실패")
    organize(client, session.id)

    after = themes(pg_conn, session.id)
    assert (after["33"]["count"], after["33"]["avg"]) == (1, pytest.approx(0.9))
    assert pg_conn.execute("SELECT count(*) FROM session_theme_summaries WHERE session_id = %s", (session.id,)).fetchone()[0] == 2
    assert pg_conn.execute("SELECT count(*) FROM session_market_summaries WHERE session_id = %s", (session.id,)).fetchone()[0] == 1
    assert market(pg_conn, session.id)["scored"] == 3


def test_archived_stocks_are_not_summarized(client, repo, pg_conn):
    session = make(repo, ROWS, STATS)
    pg_conn.execute("UPDATE session_items SET archived = true WHERE session_id = %s AND code = '000003'", (session.id,))
    organize(client, session.id)
    assert themes(pg_conn, session.id)["33"]["count"] == 1
    assert market(pg_conn, session.id)["scored"] == 3


def test_a_session_with_nothing_scored_still_gets_a_market_row_and_no_themes(client, repo, pg_conn):
    session = make(repo, [(1, "26", "건설", None), (2, "33", "항공사", None)])
    assert organize(client, session.id).status_code == 200
    assert themes(pg_conn, session.id) == {}
    overall = market(pg_conn, session.id)
    assert (overall["scored"], overall["avg"]) == (0, None)
    assert "점수가 있는 후보가 없" in overall["text"]


def test_summaries_of_one_session_do_not_touch_another(client, repo, pg_conn):
    one, two = make(repo, ROWS, STATS), make(repo, ROWS[:2])
    organize(client, one.id)
    organize(client, two.id)
    assert set(themes(pg_conn, one.id)) == {"26", "33"}
    assert set(themes(pg_conn, two.id)) == {"26"}


def test_deleting_a_session_deletes_its_summaries(client, repo, pg_conn):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    pg_conn.execute("DELETE FROM sessions WHERE id = %s", (session.id,))
    assert themes(pg_conn, session.id) == {} and market(pg_conn, session.id) is None


# --- 정리가 못 하는 경우 / 실패 ---


def test_nothing_is_saved_when_organizing_is_refused(client, repo, pg_conn):
    session = repo.create_session(AS_OF)
    repo.add_items(session.id, [Item(seq=1, code="000001", name="가", market="KOSPI")])  # 아직 대기 중
    assert organize(client, session.id).status_code == 409
    assert themes(pg_conn, session.id) == {} and market(pg_conn, session.id) is None


def test_a_failure_while_summarizing_leaves_the_ranking_untouched(client, repo, pg_conn, monkeypatch):
    """순위와 요약은 한 트랜잭션이다: 요약이 실패하면 순위도 정리 시각도 바뀌지 않는다."""
    session = make(repo, ROWS, STATS)

    def broken(*args, **kwargs):
        raise RuntimeError("요약 실패")

    monkeypatch.setattr(summary, "summarize", broken)
    with pytest.raises(RuntimeError):
        organize(client, session.id)

    assert pg_conn.execute("SELECT ranked_at FROM sessions WHERE id = %s", (session.id,)).fetchone()[0] is None
    assert all(i.rank is None for i in repo.items(session.id))
    assert themes(pg_conn, session.id) == {} and market(pg_conn, session.id) is None


def test_the_organize_response_does_not_change(client, repo):
    session = make(repo, ROWS, STATS)
    assert organize(client, session.id).json() == {"ranked": 4, "unscored": 0}


# --- 목록을 만들 때 전종목 수치를 저장한다 ---


def test_building_a_list_saves_the_whole_market_numbers(client, pg_conn):
    response = client.post("/api/sessions", json={"as_of": "2026-11-05"})
    assert response.status_code == 202
    session_id = uuid.UUID(response.json()["id"])

    stats = pg_conn.execute("SELECT universe_stats FROM sessions WHERE id = %s", (session_id,)).fetchone()[0]
    items = pg_conn.execute("SELECT count(*) FROM session_items WHERE session_id = %s", (session_id,)).fetchone()[0]
    assert stats["candidates"] == items == 3  # 기본 세계: 3종목이 모두 필터를 통과한다
    assert stats["evaluated"] >= stats["candidates"]
    assert {"listing_total", "after_static", "evaluated", "undetermined", "zero_volume_excluded", "fail_each"} <= set(stats)
    assert set(stats["fail_each"]) >= {"avg_trading_value", "above_ma20", "above_ma60"}
