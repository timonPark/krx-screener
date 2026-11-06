"""평가점수 계산 (#22): 에이전트별 등급 → 점수(−2 ~ +2 평균)와 등급을 낸 에이전트 수."""

import pytest

from krx_screener.agent_ratings import AGENT_FIELDS, AGENT_TOTAL
from krx_screener.scoring import RATING_POINTS, score_ratings

AGENTS = list(AGENT_FIELDS)


def all_agents(rating):
    return dict.fromkeys(AGENTS, rating)


def test_the_five_ratings_are_worth_plus_two_to_minus_two():
    assert RATING_POINTS == {"Buy": 2, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -2}


def test_all_hold_is_zero_and_all_buy_is_plus_two():
    assert score_ratings(all_agents("Hold")).score == 0
    assert score_ratings(all_agents("Buy")).score == 2
    assert score_ratings(all_agents("Sell")).score == -2


def test_the_score_is_the_plain_average_of_every_agent_that_gave_a_rating():
    ratings = {"market": "Buy", "news": "Overweight", "bull": "Buy", "bear": "Sell"}  # 2 + 1 + 2 − 2 = 3
    assert score_ratings(ratings).score == 3 / 4


def test_every_agent_weighs_the_same_and_bull_and_bear_are_included():
    ratings = {"bull": "Buy", "bear": "Sell"}  # 둘은 종목마다 값이 달라서 평균에 넣는다
    assert score_ratings(ratings).score == 0
    assert score_ratings({"bull": "Buy"}).score == 2


def test_agents_without_a_rating_are_left_out_of_the_average_and_counted():
    ratings = {"market": "Buy", "news": "Hold", "bull": "Overweight"}  # 나머지 7개는 읽지 못했다
    result = score_ratings(ratings)
    assert result.score == 1  # (2 + 0 + 1) / 3 — 빈 칸을 0 으로 치지 않는다
    assert (result.rated, result.total) == (3, AGENT_TOTAL) == (3, 10)


def test_when_no_agent_could_be_read_the_score_is_empty_not_zero():
    result = score_ratings({})
    assert result.score is None  # 점수를 지어내지 않는다
    assert (result.rated, result.total) == (0, 10)


def test_a_full_set_counts_ten_of_ten():
    result = score_ratings(all_agents("Underweight"))
    assert (result.score, result.rated, result.total) == (-1, 10, 10)


def test_a_value_that_is_not_one_of_the_five_ratings_is_not_counted():
    result = score_ratings({"market": "Buy", "news": "REVIEW", "bull": "매수", "bear": ""})
    assert (result.score, result.rated) == (2, 1)  # 알 수 없는 값은 읽지 못한 것과 같다
    assert score_ratings({"news": "REVIEW"}).score is None


def test_the_same_average_gives_exactly_the_same_number_so_ties_can_be_detected():
    # 3개의 합 3 → 1.0 과 6개의 합 6 → 1.0, 2개의 합 1 → 0.5 와 4개의 합 2 → 0.5 가 정확히 같아야 순위의 동점이 성립한다
    assert score_ratings({"market": "Overweight", "news": "Overweight", "bull": "Overweight"}).score == score_ratings(
        dict.fromkeys(AGENTS[:6], "Overweight")
    ).score
    assert score_ratings({"market": "Overweight", "news": "Hold"}).score == score_ratings(
        {"market": "Overweight", "news": "Hold", "bull": "Overweight", "bear": "Hold"}
    ).score
    assert score_ratings({"market": "Buy", "news": "Hold", "bull": "Hold"}).score == pytest.approx(2 / 3)


def test_the_score_is_always_within_minus_two_and_plus_two():
    for rating in RATING_POINTS:
        assert -2 <= score_ratings(all_agents(rating)).score <= 2
