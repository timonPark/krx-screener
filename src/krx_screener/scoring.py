"""평가점수 계산 (이슈 #22): 에이전트별 등급을 숫자로 바꿔 평균을 낸다.

- 등급 → 점수: 매수 +2, 비중 확대 +1, 보유 0, 비중 축소 −1, 매도 −2.
- **점수 = 등급을 낸 에이전트 전부의 평균** (−2 ~ +2). 비중은 모두 같고 강세·약세도 포함한다 (둘의 값은 종목마다 달라서 평균에 넣는다).
- 등급을 읽지 못한 에이전트는 평균에서 뺀다. 빈 칸을 0(보유)으로 치지 않는다. 하나도 못 읽었으면 점수는 None 이다 — 점수를 지어내지 않는다.
- 종목마다 등급을 낸 에이전트 수와 전체 수를 같이 돌려준다 (적은 수로 계산한 점수를 화면에서 구별할 수 있게).
- 에이전트의 등급만으로 계산한다. 보고서 본문은 읽지 않고, 모델을 다시 부르지 않으니 쿼터도 들지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from krx_screener.agent_ratings import AGENT_TOTAL

RATING_POINTS: dict[str, int] = {"Buy": 2, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -2}


@dataclass(frozen=True)
class Score:
    score: float | None  # 읽은 에이전트가 없으면 None
    rated: int  # 등급을 낸 에이전트 수
    total: int  # 전체 에이전트 수


def score_ratings(ratings: Mapping[str, str]) -> Score:
    """``에이전트 → 내부 등급 값`` 에서 점수를 낸다. 5단계에 없는 값은 읽지 못한 것으로 본다."""
    points = [RATING_POINTS[rating] for rating in ratings.values() if rating in RATING_POINTS]
    # 정수 합을 정수 개수로 한 번만 나눈다: 같은 평균이면 정확히 같은 수가 되어 순위의 동점을 가릴 수 있다
    average = sum(points) / len(points) if points else None
    return Score(score=average, rated=len(points), total=AGENT_TOTAL)
