"""정리 요약을 읽는 두 가지: 세션 조회의 ``summary`` 와 마크다운 보고서 (``GET /api/sessions/{id}/report``).

요약은 정리할 때 DB 에 저장된다 (``test_organize_summary_api.py``). 여기서는 그것을 API 로 내려주고 보고서로 만드는 쪽을 웹 API 경계에서 검증한다.
"""

import uuid
from datetime import date

import pytest
from test_organize_summary_api import ROWS, STATS, make, organize

from krx_screener.repository import Item

RATING_KEYS = ["매수", "비중 확대", "보유", "비중 축소", "매도"]
FLOW_KEYS = ["양호", "부분", "약함", "미확인"]


def detail(client, session_id):
    return client.get(f"/api/sessions/{session_id}").json()


def report(client, session_id):
    return client.get(f"/api/sessions/{session_id}/report")


# --- 세션 조회의 summary ---


def test_the_detail_has_no_summary_before_organizing(client, repo):
    session = make(repo, ROWS, STATS)
    assert detail(client, session.id)["summary"] is None


def test_the_detail_carries_the_saved_summary_after_organizing(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    summary = detail(client, session.id)["summary"]

    assert set(summary) == {"created_at", "market", "themes"}
    assert summary["created_at"].endswith("Z")  # 다른 시각과 같은 UTC 문자열
    market = summary["market"]
    assert set(market) == {"scored_count", "avg_score", "rating_counts", "flow_counts", "universe_stats", "assessment"}
    assert market["scored_count"] == 4
    assert market["avg_score"] == pytest.approx((0.4 + 0.2 - 0.5 - 0.7) / 4)
    assert market["universe_stats"] == STATS
    assert "20일선 위 60.0%" in market["assessment"]

    # 화면에 보이는 값은 한국어다: 등급·수급 분포의 키가 한국어이고 순서가 정해져 있다 (저장은 영어 코드값)
    assert list(market["rating_counts"]) == RATING_KEYS
    assert market["rating_counts"] == {"매수": 0, "비중 확대": 0, "보유": 2, "비중 축소": 2, "매도": 0}
    assert list(market["flow_counts"]) == FLOW_KEYS


def test_the_themes_are_listed_with_the_most_stocks_first(client, repo):
    session = make(repo, [(1, "26", "건설", 0.4), (2, "33", "항공사", -0.5), (3, "33", "항공사", -0.7), (4, "33", "항공사", -0.1)])
    organize(client, session.id)
    themes = detail(client, session.id)["summary"]["themes"]

    assert [t["name"] for t in themes] == ["항공사", "건설"]
    first = themes[0]
    assert set(first) == {
        "code", "name", "item_count", "avg_score", "rating_counts", "flow_counts", "avg_rsi", "foreign_net_5d",
        "institution_net_5d", "interpretation",
    }
    assert (first["code"], first["item_count"]) == ("33", 3)
    assert first["rating_counts"]["비중 축소"] == 3
    assert first["interpretation"].startswith("항공사:")


def test_a_session_saved_without_whole_market_numbers_has_a_null_universe(client, repo):
    session = make(repo, ROWS)
    organize(client, session.id)
    assert detail(client, session.id)["summary"]["market"]["universe_stats"] is None


def test_the_latest_alias_returns_the_summary_too(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    assert client.get("/api/sessions/latest").json()["summary"]["market"]["scored_count"] == 4


def test_the_summary_is_kept_but_the_rank_is_marked_stale_after_a_reanalysis(client, repo):
    """정리한 뒤 다시 분석하면 순위가 낡았다고 표시된다 (요약도 같은 정리의 결과라 같이 낡았다). 요약은 그대로 보인다."""
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    repo.mark_done(session.id, "000003", decision="Hold", score=0.9, agents_rated=10, agents_total=10)
    body = detail(client, session.id)
    assert body["rank_stale"] is True
    assert body["summary"] is not None


# --- 보고서 ---


def test_the_report_needs_the_session_to_be_organized_first(client, repo):
    session = make(repo, ROWS, STATS)
    response = report(client, session.id)
    assert response.status_code == 409
    assert "정리를 먼저 실행" in response.json()["detail"]


def test_the_report_of_an_unknown_session_is_a_404(client):
    assert report(client, uuid.uuid4()).status_code == 404
    assert report(client, "not-a-uuid").status_code == 404


def test_the_report_is_a_markdown_download_named_after_the_date(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    response = report(client, session.id)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "charset=utf-8" in response.headers["content-type"].lower()
    assert response.headers["content-disposition"] == 'attachment; filename="krx-screener-summary-2026-11-05.md"'
    assert response.text.startswith("# KRX Screener 정리 요약 — 2026-11-05")


def test_the_report_holds_the_saved_assessment_and_every_theme(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    body = report(client, session.id).text

    stored = detail(client, session.id)["summary"]
    assert stored["market"]["assessment"] in body
    for theme in stored["themes"]:
        assert theme["name"] in body
        assert theme["interpretation"] in body  # 저장된 문장을 그대로 싣는다 (다시 만들지 않는다)
    # 종합 평가의 수치 표
    assert "| 점수가 있는 후보 | 4개 |" in body
    # 등급·수급 분포는 정해진 순서로 적는다 (DB 의 jsonb 는 키 순서를 지키지 않는다)
    assert "| 등급 | 매수 0 · 비중 확대 0 · 보유 2 · 비중 축소 2 · 매도 0 |" in body
    assert "| 수급 상태 | 양호 0 · 부분 4 · 약함 0 · 미확인 0 |" in body
    # 전종목 수치 (저장되어 있을 때)
    assert "시세를 확인한 종목" in body and "2,000" in body
    # 실패한 종목은 세지 않고, 어느 값도 파이썬의 None 으로 새지 않는다
    assert "None" not in body and "null" not in body


def test_the_report_says_when_there_are_no_whole_market_numbers(client, repo):
    session = make(repo, ROWS)
    organize(client, session.id)
    body = report(client, session.id).text
    assert "전종목 수치가 저장되어 있지 않" in body
    assert "시세를 확인한 종목" not in body  # 수치 표는 없다


def test_the_report_warns_when_the_ranking_is_stale(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    assert "낡았" not in report(client, session.id).text

    repo.mark_done(session.id, "000003", decision="Hold", score=0.9, agents_rated=10, agents_total=10)
    assert "정리 이후 다시 분석된 종목이 있어 낡았을 수 있습니다" in report(client, session.id).text


def test_the_report_states_the_score_scale_and_that_it_is_not_investment_advice(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    body = report(client, session.id).text
    assert "−2 ~ +2" in body and "100 을 곱한" in body  # 화면은 100 배로 보이므로 보고서의 스케일을 밝힌다
    assert "투자 권유가 아닙니다" in body
    assert "추천" not in body


def test_the_latest_alias_downloads_the_report_too(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    assert client.get("/api/sessions/latest/report").status_code == 200


def test_a_pipe_in_a_name_does_not_break_the_markdown_table(client, repo):
    session = make(repo, [(1, "26", "건설|토목", 0.4), (2, "26", "건설|토목", 0.2)], STATS)
    organize(client, session.id)
    body = report(client, session.id).text
    assert "건설\\|토목" in body


def test_organizing_again_changes_the_report(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    before = report(client, session.id).text
    repo.mark_done(session.id, "000003", decision="Hold", score=0.9, agents_rated=10, agents_total=10)
    organize(client, session.id)
    assert report(client, session.id).text != before


def test_an_empty_session_still_yields_a_report(client, repo):
    """점수가 있는 종목이 하나도 없어도 정리는 되고, 보고서는 그렇다고 말한다."""
    session = repo.create_session(date(2026, 11, 5))
    repo.add_items(session.id, [Item(seq=1, code="000001", name="가", market="KOSPI")])
    repo.mark_failed(session.id, "000001", "실패")
    organize(client, session.id)
    response = report(client, session.id)
    assert response.status_code == 200
    assert "점수가 있는 후보가 없" in response.text
