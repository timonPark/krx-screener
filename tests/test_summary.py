"""정리할 때 만드는 업종(테마)별 해석과 종합 평가 (순수 계산). DB 없이 검증한다.

문장은 수치로만 만든다: 모델도 외부 조회도 쓰지 않으므로 같은 입력이면 같은 결과이고, 문장의 수치는 계산값과 어긋날 수 없다.
"""

from datetime import date

from krx_screener.repository import Item
from krx_screener.summary import summarize

AS_OF = date(2026, 11, 5)


def item(seq, score, decision="Hold", *, sector=("26", "건설"), flow="partial", rsi=60.0, foreign=100, inst=200,
         state="done", name=None):
    return Item(
        seq=seq, code=f"{seq:06d}", name=name or f"종목{seq}", market="KOSPI", rsi=rsi, flow_status=flow,
        foreign_net_5d=foreign, institution_net_5d=inst, sector_code=sector[0], sector_name=sector[1],
        state=state, decision=decision, score=score, agents_rated=10, agents_total=10,
    )


def by_code(themes):
    return {t.code: t for t in themes}


# --- 업종(테마)별 ---


def test_themes_group_the_scored_stocks_by_sector_and_average_their_scores():
    items = [
        item(1, 0.5, "Hold", sector=("26", "건설")),
        item(2, 0.3, "Hold", sector=("26", "건설")),
        item(3, 0.1, "Underweight", sector=("26", "건설")),
        item(4, -0.5, "Underweight", sector=("33", "항공사")),
        item(5, -0.7, "Underweight", sector=("33", "항공사")),
    ]
    themes, _ = summarize(items, AS_OF, None)
    found = by_code(themes)
    assert set(found) == {"26", "33"}
    assert (found["26"].name, found["26"].item_count) == ("건설", 3)
    assert found["26"].avg_score == (0.5 + 0.3 + 0.1) / 3
    assert found["26"].rating_counts == {"Buy": 0, "Overweight": 0, "Hold": 2, "Underweight": 1, "Sell": 0}
    assert found["33"].avg_score == -0.6
    assert found["33"].rating_counts["Underweight"] == 2


def test_stocks_that_do_not_take_part_in_the_ranking_are_left_out():
    """실패한 종목과, 끝났지만 점수가 없는 종목은 순위에서 빠진다 — 요약에서도 뺀다 (점수를 0 으로 지어내지 않는다)."""
    items = [
        item(1, 0.4, "Hold"),
        item(2, None, "REVIEW"),  # 끝났지만 에이전트 등급을 하나도 못 읽음
        item(3, None, "", state="failed"),
        item(4, None, "", state="pending"),
    ]
    themes, market = summarize(items, AS_OF, None)
    assert [t.item_count for t in themes] == [1]
    assert market.scored_count == 1
    assert market.avg_score == 0.4


def test_the_flow_counts_and_net_buying_use_only_the_stocks_whose_flow_is_known():
    items = [
        item(1, 0.1, flow="strong", foreign=1_000, inst=2_000, rsi=50.0),
        item(2, 0.1, flow="weak", foreign=-400, inst=-100, rsi=70.0),
        item(3, 0.1, flow="unconfirmed", foreign=None, inst=None, rsi=None),
    ]
    (theme,), _ = summarize(items, AS_OF, None)
    assert theme.flow_counts == {"strong": 1, "partial": 0, "weak": 1, "unconfirmed": 1}
    assert (theme.foreign_net_5d, theme.institution_net_5d) == (600, 1_900)  # 읽은 두 종목의 합
    assert theme.avg_rsi == 60.0  # RSI 를 모르는 종목은 평균에서 뺀다


def test_net_buying_stays_empty_when_no_stock_has_a_flow_reading():
    (theme,), _ = summarize([item(1, 0.1, flow="unconfirmed", foreign=None, inst=None)], AS_OF, None)
    assert (theme.foreign_net_5d, theme.institution_net_5d) == (None, None)  # 0 이 아니다
    assert "수급을 확인한 종목이 없" in theme.interpretation


def test_a_stock_without_a_sector_is_grouped_as_unconfirmed_sector():
    themes, _ = summarize([item(1, 0.2, sector=("", "")), item(2, 0.2, sector=("26", "건설"))], AS_OF, None)
    assert {t.code: t.name for t in themes} == {"": "업종 미확인", "26": "건설"}


# --- 해석 문장 ---


def test_the_interpretation_states_the_numbers_and_how_the_theme_compares_with_all_candidates():
    items = [item(i, 0.6, "Overweight", sector=("A", "높은업종")) for i in (1, 2, 3)]
    items += [item(i, -0.6, "Underweight", sector=("B", "낮은업종")) for i in (4, 5, 6)]
    items += [item(i, 0.0, "Hold", sector=("C", "비슷한업종")) for i in (7, 8, 9)]  # 전체 평균 0.0
    found = by_code(summarize(items, AS_OF, None)[0])
    assert "평균 평가점수 +0.60" in found["A"].interpretation
    assert "후보 전체 평균(+0.00)보다 높습니다" in found["A"].interpretation
    assert "후보 전체 평균(+0.00)보다 낮습니다" in found["B"].interpretation
    assert "후보 전체 평균(+0.00)과 비슷합니다" in found["C"].interpretation
    # 등급 분포와 수급, 순매수 합계(주)가 그대로 들어간다
    assert "비중 확대 3개" in found["A"].interpretation
    assert "양호 0·부분 3·약함 0·미확인 0" in found["A"].interpretation
    assert "외국인 +300주" in found["A"].interpretation and "기관 +600주" in found["A"].interpretation


def test_a_small_theme_is_marked_as_reference_only():
    items = [item(1, 0.2, sector=("A", "하나뿐")), *[item(i, 0.2, sector=("B", "셋")) for i in (2, 3, 4)]]
    found = by_code(summarize(items, AS_OF, None)[0])
    assert "참고만" in found["A"].interpretation
    assert "참고만" not in found["B"].interpretation


def test_no_sentence_recommends_or_forecasts():
    """후보는 추천 종목이 아니고 순위는 투자 판단이 아니다 (GLOSSARY). 문장도 그렇다."""
    items = [item(i, 0.5, "Overweight") for i in (1, 2, 3)]
    themes, market = summarize(items, AS_OF, {"evaluated": 100, "candidates": 3, "fail_each": {"above_ma20": 40}})
    text = " ".join([*(t.interpretation for t in themes), market.assessment])
    for word in ("추천", "매수하세요", "오를", "내릴", "전망"):
        assert word not in text


# --- 종합 평가 ---


def test_the_market_assessment_counts_candidates_and_ratings():
    items = [item(1, 0.4, "Hold"), item(2, -0.2, "Underweight"), item(3, -0.4, "Underweight"), item(4, -0.6, "Underweight")]
    _, market = summarize(items, AS_OF, None)
    assert market.scored_count == 4
    assert market.avg_score == (0.4 - 0.2 - 0.4 - 0.6) / 4
    assert market.rating_counts == {"Buy": 0, "Overweight": 0, "Hold": 1, "Underweight": 3, "Sell": 0}
    assert "2026-11-05 기준" in market.assessment
    assert "점수가 있는 후보 4개" in market.assessment
    assert "평균 평가점수는 -0.20" in market.assessment
    assert "보유 1개" in market.assessment and "비중 축소 3개" in market.assessment


def test_the_assessment_adds_whole_market_numbers_when_they_were_saved():
    stats = {
        "listing_total": 2800, "after_static": 2600, "evaluated": 2000, "candidates": 116, "undetermined": 50,
        "zero_volume_excluded": 40,
        "fail_each": {"avg_trading_value": 1500, "above_ma20": 800, "above_ma60": 1000},
    }
    _, market = summarize([item(1, 0.1)], AS_OF, stats)
    assert market.universe_stats == stats
    assert "시세를 확인한 2,000개 중 20일선 위 60.0%, 60일선 위 50.0%" in market.assessment
    assert "후보 조건을 모두 통과한 종목은 116개(5.8%)" in market.assessment


def test_the_assessment_says_so_when_whole_market_numbers_were_not_saved():
    """전종목 수치를 저장하기 전에 만든 목록. 지어내지 않고 없다고 말한다."""
    _, market = summarize([item(1, 0.1)], AS_OF, None)
    assert market.universe_stats is None
    assert "전종목 수치가 저장되어 있지 않" in market.assessment


def test_the_assessment_always_says_candidates_do_not_represent_the_whole_market():
    for stats in (None, {"evaluated": 10, "candidates": 1, "fail_each": {}}):
        _, market = summarize([item(1, 0.1)], AS_OF, stats)
        assert "시장 전체를 대표하지 않습니다" in market.assessment


def test_with_no_scored_stock_there_are_no_themes_and_no_average():
    themes, market = summarize([item(1, None, "REVIEW"), item(2, None, "", state="failed")], AS_OF, None)
    assert themes == []
    assert (market.scored_count, market.avg_score) == (0, None)
    assert "점수가 있는 후보가 없" in market.assessment
