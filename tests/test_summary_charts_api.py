"""정리 요약의 차트를 내려주는 API 와 보고서 속 차트 (``GET /api/sessions/{id}/charts/{이름}.svg``, ``/report?format=html``).

요약은 정리할 때 DB 에 저장된다. 여기서는 그것으로 그린 SVG 를 API 로 내려주고 보고서(마크다운·HTML)에 싣는 쪽을 웹 API 경계에서 검증한다.
"""

import uuid
import xml.etree.ElementTree as ET

import pytest
from test_organize_summary_api import ROWS, STATS, make, organize

ALL = ["rating", "flow", "breadth", "theme-score", "theme-ratings"]


def chart(client, session_id, name):
    return client.get(f"/api/sessions/{session_id}/charts/{name}.svg")


def report(client, session_id, **params):
    return client.get(f"/api/sessions/{session_id}/report", params=params)


# --- 차트 ---


@pytest.mark.parametrize("name", ALL)
def test_each_chart_is_served_as_svg_that_is_not_cached(client, repo, name):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    response = chart(client, session.id, name)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers["cache-control"] == "no-cache"  # 다시 정리하면 그림이 바뀐다
    root = ET.fromstring(response.text)  # 올바른 XML
    assert root.tag == "{http://www.w3.org/2000/svg}svg"


def test_a_chart_needs_the_session_to_be_organized_first(client, repo):
    session = make(repo, ROWS, STATS)
    response = chart(client, session.id, "rating")
    assert response.status_code == 409
    assert "정리를 먼저 실행" in response.json()["detail"]


def test_an_unknown_chart_or_session_is_a_404(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    assert chart(client, session.id, "pie").status_code == 404
    assert chart(client, uuid.uuid4(), "rating").status_code == 404


def test_the_whole_market_chart_is_a_404_when_those_numbers_were_not_saved(client, repo):
    session = make(repo, ROWS)  # 전종목 수치 없음
    organize(client, session.id)
    response = chart(client, session.id, "breadth")
    assert response.status_code == 404
    assert "전종목 수치" in response.json()["detail"]
    assert chart(client, session.id, "rating").status_code == 200  # 나머지는 그려진다


def test_the_latest_alias_serves_charts_too(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    assert client.get("/api/sessions/latest/charts/rating.svg").status_code == 200


def test_organizing_again_redraws_the_charts(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    before = chart(client, session.id, "rating").text
    repo.mark_done(session.id, "000003", decision="Hold", score=0.9, agents_rated=10, agents_total=10)
    organize(client, session.id)
    assert chart(client, session.id, "rating").text != before


# --- HTML 보고서 ---


def test_the_html_report_is_a_self_contained_download_with_every_chart_inline(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    response = report(client, session.id, format="html")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "charset=utf-8" in response.headers["content-type"].lower()
    assert response.headers["content-disposition"] == 'attachment; filename="krx-screener-summary-2026-11-05.html"'
    body = response.text
    assert body.startswith("<!doctype html>") and '<html lang="ko">' in body
    assert body.count("<svg") == 5  # 다섯 차트가 그대로 들어 있다 (외부 파일 없이 열린다)
    assert "http://" not in body.replace("http://www.w3.org/2000/svg", "")  # 외부 주소를 부르지 않는다
    assert "<script" not in body


def test_the_html_report_holds_the_same_sentences_as_the_markdown_one(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    body = report(client, session.id, format="html").text
    stored = client.get(f"/api/sessions/{session.id}").json()["summary"]
    assert stored["market"]["assessment"] in body
    for theme in stored["themes"]:
        assert theme["interpretation"] in body
    assert "정리 요약 — 2026-11-05" in body


def test_the_html_report_has_four_charts_when_the_whole_market_numbers_are_missing(client, repo):
    session = make(repo, ROWS)
    organize(client, session.id)
    assert report(client, session.id, format="html").text.count("<svg") == 4


def test_a_name_with_markup_cannot_inject_html_into_the_report(client, repo):
    session = make(repo, [(1, "1", "<script>alert(1)</script>", 0.4), (2, "1", "<script>alert(1)</script>", 0.2)], STATS)
    organize(client, session.id)
    body = report(client, session.id, format="html").text
    assert "<script" not in body
    assert "&lt;script&gt;" in body


def test_the_html_report_warns_when_the_ranking_is_stale(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    assert "낡았" not in report(client, session.id, format="html").text
    repo.mark_done(session.id, "000003", decision="Hold", score=0.9, agents_rated=10, agents_total=10)
    assert "정리 이후 다시 분석된 종목이 있어 낡았을 수 있습니다" in report(client, session.id, format="html").text


def test_the_html_report_needs_the_session_to_be_organized_first(client, repo):
    session = make(repo, ROWS, STATS)
    assert report(client, session.id, format="html").status_code == 409


def test_the_default_format_is_still_markdown_and_other_formats_are_rejected(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    assert report(client, session.id).headers["content-type"].startswith("text/markdown")
    assert report(client, session.id, format="md").headers["content-type"].startswith("text/markdown")
    assert report(client, session.id, format="pdf").status_code == 422


# --- 마크다운 보고서의 텍스트 차트 ---


def test_the_markdown_report_carries_text_charts_in_a_code_block(client, repo):
    session = make(repo, ROWS, STATS)
    organize(client, session.id)
    body = report(client, session.id).text
    assert "## 차트" in body and "```text" in body
    for title in ("등급 분포", "수급 상태 분포", "전종목 시장 폭", "업종별 평균 평가점수"):
        assert title in body
    assert "█" in body
