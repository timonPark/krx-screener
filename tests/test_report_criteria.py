"""보고서의 "후보와 점수의 기준" 절: 후보를 어떤 조건으로 뽑았는지, "점수가 있는 후보"가 무엇인지, 평가점수를 어떻게 계산하는지.

조건은 세션이 목록을 뽑을 때 저장한 값(``sessions.criteria``)으로 쓴다. 그 값이 없는 세션(조건을 기록하기 전에 만든 것)은 현재 설정으로 채우지 않고
기록이 없다고 말한다 — 옛 세션을 지금 설정으로 채우면 틀린 값일 수 있다.
"""

import json
import re

import pytest
from test_organize_summary_api import ROWS, STATS, make, organize
from test_summary_charts_api import report

from krx_screener.config import criteria_of
from krx_screener.repository import Item

RECORDED = {
    "filters": {"min_avg_trading_value_krw": 1_000_000_000, "avg_trading_value_window": 20, "price_above_ma": [20, 60], "rsi_max": None},
    "exclude": ["preferred", "spac", "managed", "zero_volume", "new_listing", "reit"],
    "flow": {"mode": "mark", "window_days": 5},
}


def record(repo, session, criteria):
    repo._conn.execute("UPDATE sessions SET criteria = %s::jsonb WHERE id = %s", (json.dumps(criteria), session.id))


def section(text: str) -> str:
    """`## 후보와 점수의 기준` 절 (다음 `## ` 제목 앞까지)."""
    return text.split("## 후보와 점수의 기준")[1].split("\n## ")[0]


def organized(client, repo, rows=ROWS, criteria=RECORDED):
    session = make(repo, rows, STATS)
    if criteria is not None:
        record(repo, session, criteria)
    organize(client, session.id)
    return session


# --- 후보를 뽑는 기준 ---


def test_the_report_states_the_conditions_the_list_was_built_with(client, repo):
    body = report(client, organized(client, repo).id).text
    text = section(body)
    assert "20거래일 평균 거래대금(종가×거래량 근사) 10억 이상" in text
    assert "종가가 20일선·60일선 위" in text
    assert "RSI 조건 없음" in text
    assert "제외한 종류: 우선주·스팩·관리종목·거래정지 의심·신규상장·리츠" in text
    assert "수급: 표시만 (후보를 거르지 않음), 최근 5거래일" in text
    assert "평균 거래대금이 큰 순서로 순번" in text


def test_the_section_comes_before_the_assessment_that_uses_the_terms(client, repo):
    body = report(client, organized(client, repo).id).text
    assert body.index("## 후보와 점수의 기준") < body.index("## 종합 평가")


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"filters": {**RECORDED["filters"], "rsi_max": 70}}, "RSI 70 미만"),
        ({"filters": {**RECORDED["filters"], "min_avg_trading_value_krw": 150_000_000}}, "1.5억 이상"),
        ({"filters": {**RECORDED["filters"], "min_avg_trading_value_krw": 50_000_000}}, "5,000만 이상"),
        ({"filters": {**RECORDED["filters"], "price_above_ma": [5]}}, "종가가 5일선 위"),
        ({"exclude": []}, "제외한 종류: 없음"),
        ({"flow": {"mode": "filter", "window_days": 3}}, "수급: 약함 후보는 탈락, 최근 3거래일"),
    ],
)
def test_each_recorded_condition_is_read_from_the_session(client, repo, change, expected):
    criteria = {**RECORDED, **change}
    assert expected in section(report(client, organized(client, repo, criteria=criteria).id).text)


def test_the_conditions_the_code_writes_are_the_ones_the_report_reads(client, repo):
    """`criteria_of(config)` 가 저장하는 모양 그대로 읽는다 (보고서가 다른 모양을 가정하지 않는다)."""
    from fakes import repo_config

    stored = criteria_of(repo_config())
    text = section(report(client, organized(client, repo, criteria=stored).id).text)
    assert "거래일 평균 거래대금" in text and "일선 위" in text and "제외한 종류:" in text and "수급:" in text


def test_a_session_without_recorded_conditions_says_so_and_invents_none(client, repo):
    body = report(client, organized(client, repo, criteria=None).id).text
    text = section(body)
    assert "목록을 뽑을 때 쓴 조건이 기록되어 있지 않습니다" in text
    assert not re.search(r"\d+억|거래일 평균|일선 위|RSI \d", text)  # 현재 설정으로 채워 넣지 않는다


# --- 점수가 있는 후보 ---


def test_the_report_counts_the_candidates_with_and_without_a_score(client, repo):
    text = section(report(client, organized(client, repo).id).text)  # 4개 점수 있음, 1개 실패
    assert "후보 5개 중 점수가 있는 종목은 4개입니다" in text
    assert "실패 1개" in text
    assert "점수를 0으로 지어내지 않습니다" in text


def test_a_finished_stock_without_a_score_is_counted_separately_from_a_failed_one(client, repo):
    session = make(repo, [(1, "26", "건설", 0.4), (2, "26", "건설", None)], STATS)  # 2번은 실패
    repo.add_items(session.id, [Item(seq=8, code="000008", name="점수없음", market="KOSPI")])
    repo.mark_done(session.id, "000008", decision="REVIEW", score=None, agents_rated=0, agents_total=10)  # 끝났지만 등급을 하나도 못 읽음
    record(repo, session, RECORDED)
    organize(client, session.id)
    text = section(report(client, session.id).text)
    assert "후보 3개 중 점수가 있는 종목은 1개입니다" in text
    assert "실패 1개" in text and "완료했지만 점수 없음 1개" in text


def test_stocks_added_after_organizing_are_counted_as_not_processed_yet(client, repo):
    session = organized(client, repo)  # 4개 점수 있음, 1개 실패
    repo.add_items(session.id, [Item(seq=9, code="000009", name="대기", market="KOSPI")])
    text = section(report(client, session.id).text)
    assert "후보 6개 중 점수가 있는 종목은 4개입니다" in text
    assert "아직 처리하지 않은 종목 1개" in text


def test_the_definition_says_what_a_candidate_with_a_score_is(client, repo):
    text = section(report(client, organized(client, repo).id).text)
    assert "분석이 끝났고" in text and "한 개 이상" in text


# --- 평가점수와 순위 ---


def test_the_report_explains_how_the_score_and_the_rank_are_made(client, repo):
    text = section(report(client, organized(client, repo).id).text)
    assert "매수 +2 · 비중 확대 +1 · 보유 0 · 비중 축소 −1 · 매도 −2" in text
    assert "에이전트 10개" in text
    assert "0 으로 치지 않습니다" in text
    assert "최종 등급" in text and "다른 값" in text
    assert "평가점수가 높은 순" in text


# --- HTML ---


def test_the_html_report_has_the_same_section_before_the_assessment(client, repo):
    body = report(client, organized(client, repo).id, format="html").text
    assert body.index("후보와 점수의 기준") < body.index("종합 평가")
    text = body.split("후보와 점수의 기준")[1].split("<h2>")[0]
    assert "10억 이상" in text and "<li>" in text and "점수가 있는 종목은 4개" in text


def test_an_unknown_excluded_kind_is_shown_by_its_code_and_escaped_in_html(client, repo):
    criteria = {**RECORDED, "exclude": ["preferred", "<b>x</b>"]}
    body = report(client, organized(client, repo, criteria=criteria).id, format="html").text
    assert "<b>x</b>" not in body and "&lt;b&gt;x&lt;/b&gt;" in body
