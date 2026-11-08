"""정리할 때 만드는 업종(테마)별 해석과 종합 평가.

- **수치로만 만든다.** 모델도 외부 조회도 쓰지 않는다 (쿼터가 들지 않고, 같은 입력이면 같은 결과이며, 문장의 수치가 계산값과 어긋날 수 없다).
  문장은 정해진 틀에 계산한 수치를 채운 것이라 단조롭다.
- **테마 = 네이버 업종**이다 (``Item.sector_code``/``sector_name``). 투자 테마 이름(예: HBM)이 아니다. 업종을 읽지 못한 종목은 ``업종 미확인`` 으로 묶는다.
- **순위에 들어가는 종목만 센다**: 끝났고(``done``) 점수가 있는 종목. 실패한 종목과 점수가 없는 종목은 정리가 순위에서 빼므로 여기서도 뺀다.
- 문장은 추천·전망을 하지 않는다 (후보는 추천 종목이 아니고 순위는 투자 판단이 아니다, GLOSSARY).
- 종합 평가는 후보의 수치에, 목록을 만들 때 저장해 둔 **전종목 수치**(``universe_stats``)를 더한다. 그 수치가 없는 목록(이 기능 전에 만든 것)은
  없다고 말한다 — 지어내지 않는다.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from krx_screener.analysis import RATING_LABELS, REVIEW
from krx_screener.flow import STATUS_LABELS

if TYPE_CHECKING:
    from krx_screener.repository import Item

RATING_ORDER = tuple(RATING_LABELS)  # Buy, Overweight, Hold, Underweight, Sell
FLOW_ORDER = tuple(STATUS_LABELS)  # strong, partial, weak, unconfirmed

# 임계값은 백테스트로 정한 값이 아니라 문장을 읽기 쉽게 하려는 기준이다.
# 평가점수 평균은 에이전트 10개의 평균이라 0.1 단위로 움직인다. 전체 평균과 0.2(= 두 단계) 이상 다르면 높다·낮다고 쓴다.
COMPARE_GAP = 0.2
# 이보다 적은 종목으로 이룬 업종은 "참고만" 이라고 적는다.
MIN_THEME_SIZE = 3

NO_SECTOR_CODE = ""
NO_SECTOR_NAME = "업종 미확인"


@dataclass(frozen=True)
class ThemeSummary:
    code: str  # 네이버 업종 코드. 읽지 못한 종목들은 빈 문자열
    name: str
    item_count: int
    avg_score: float
    rating_counts: dict[str, int]  # 등급(영어 내부값) → 종목 수
    flow_counts: dict[str, int]  # 수급 상태(영어 내부값) → 종목 수
    avg_rsi: float | None  # RSI 를 아는 종목이 없으면 None
    foreign_net_5d: int | None  # 수급을 읽은 종목들의 합. 읽은 종목이 없으면 None (0 이 아니다)
    institution_net_5d: int | None
    interpretation: str


@dataclass(frozen=True)
class MarketSummary:
    scored_count: int
    avg_score: float | None  # 점수가 있는 종목이 없으면 None
    rating_counts: dict[str, int]
    flow_counts: dict[str, int]
    universe_stats: dict | None  # 목록을 만들 때 저장한 전종목 수치. 없으면 None
    assessment: str


def _signed(value: float) -> str:
    """부호를 붙여 소수 둘째 자리까지. 0 에 가까운 값이 ``-0.00`` 으로 보이지 않게 한다."""
    return f"{round(value, 2) + 0.0:+.2f}"


def _rating_counts(items: Sequence[Item]) -> dict[str, int]:
    found = Counter(i.decision for i in items)
    counts = {rating: found.get(rating, 0) for rating in RATING_ORDER}
    if found.get(REVIEW):
        counts[REVIEW] = found[REVIEW]
    return counts


def _flow_counts(items: Sequence[Item]) -> dict[str, int]:
    found = Counter(i.flow_status for i in items)
    return {status: found.get(status, 0) for status in FLOW_ORDER}


def _rating_text(counts: Mapping[str, int]) -> str:
    parts = [f"{RATING_LABELS.get(code, code)} {n}개" for code, n in counts.items() if n]
    return "·".join(parts)


def _flow_text(counts: Mapping[str, int]) -> str:
    return "·".join(f"{STATUS_LABELS[status]} {counts[status]}" for status in FLOW_ORDER)


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _compare_text(avg: float, overall: float) -> str:
    gap = avg - overall
    whole = f"후보 전체 평균({_signed(overall)})"
    if gap >= COMPARE_GAP:
        return f"{whole}보다 높습니다"
    if gap <= -COMPARE_GAP:
        return f"{whole}보다 낮습니다"
    return f"{whole}과 비슷합니다"


def _theme_text(name: str, count: int, avg: float, overall: float, ratings: Mapping[str, int], flows: Mapping[str, int],
                avg_rsi: float | None, foreign: int | None, institution: int | None) -> str:
    sentences = [
        f"{name}: 점수가 있는 후보 {count}개의 평균 평가점수 {_signed(avg)}로 {_compare_text(avg, overall)}.",
        f"등급은 {_rating_text(ratings)}입니다.",
    ]
    if avg_rsi is not None:
        sentences.append(f"평균 RSI는 {avg_rsi:.0f}입니다.")
    sentences.append(f"수급 상태는 {_flow_text(flows)}개이고,")
    if foreign is None or institution is None:
        sentences.append("수급을 확인한 종목이 없어 5일 순매수 합계는 없습니다.")
    else:
        sentences.append(f"5일 순매수 합계는 외국인 {foreign:+,}주, 기관 {institution:+,}주입니다.")
    if count < MIN_THEME_SIZE:
        sentences.append(f"(종목이 {count}개뿐이라 참고만 하세요.)")
    return " ".join(sentences)


def _theme(code: str, name: str, items: Sequence[Item], overall: float) -> ThemeSummary:
    avg = sum(i.score for i in items) / len(items)
    ratings, flows = _rating_counts(items), _flow_counts(items)
    avg_rsi = _mean([i.rsi for i in items if i.rsi is not None])
    # 수급을 읽은 종목(외국인·기관 합계가 있는 종목)만 더한다. 읽지 못한 종목을 0 으로 치지 않는다.
    flowing = [i for i in items if i.foreign_net_5d is not None and i.institution_net_5d is not None]
    foreign = sum(i.foreign_net_5d for i in flowing) if flowing else None
    institution = sum(i.institution_net_5d for i in flowing) if flowing else None
    text = _theme_text(name, len(items), avg, overall, ratings, flows, avg_rsi, foreign, institution)
    return ThemeSummary(code, name, len(items), avg, ratings, flows, avg_rsi, foreign, institution, text)


def _universe_text(stats: Mapping | None) -> str:
    if not stats or not stats.get("evaluated"):
        return "목록을 만들 때 전종목 수치가 저장되어 있지 않아 전종목 수치는 없습니다."
    evaluated = int(stats["evaluated"])
    fail_each = stats.get("fail_each") or {}
    above = [
        f"{key.removeprefix('above_ma')}일선 위 {100 * (evaluated - fail_each[key]) / evaluated:.1f}%"
        for key in sorted(fail_each, key=lambda k: (len(k), k))
        if key.startswith("above_ma")
    ]
    head = f"전종목 가운데 시세를 확인한 {evaluated:,}개 중"
    text = f"{head} {', '.join(above)}이고, " if above else f"{head} "
    candidates = int(stats.get("candidates", 0))
    return f"{text}후보 조건을 모두 통과한 종목은 {candidates:,}개({100 * candidates / evaluated:.1f}%)입니다."


def _assessment(as_of: date, scored: int, avg: float | None, ratings: Mapping[str, int], flows: Mapping[str, int],
                stats: Mapping | None) -> str:
    if scored == 0:
        head = f"{as_of.isoformat()} 기준 점수가 있는 후보가 없습니다."
    else:
        head = (
            f"{as_of.isoformat()} 기준 점수가 있는 후보 {scored}개의 평균 평가점수는 {_signed(avg)}입니다. "
            f"등급은 {_rating_text(ratings)}이고, 수급 상태는 {_flow_text(flows)}개입니다."
        )
    caveat = (
        "후보는 거래대금과 이동평균선 조건을 통과한 종목이라 시장 전체를 대표하지 않습니다. "
        "등급은 분석 결과이고 투자 권유가 아닙니다."
    )
    return " ".join([head, _universe_text(stats), caveat])


def summarize(items: Sequence[Item], as_of: date, universe_stats: Mapping | None) -> tuple[list[ThemeSummary], MarketSummary]:
    """순위에 들어가는 종목으로 업종별 요약과 종합 평가를 만든다. 업종은 종목 수가 많은 순(같으면 코드 순)이다."""
    ranked = [i for i in items if i.state == "done" and i.score is not None]
    overall = _mean([i.score for i in ranked])
    groups: dict[str, list[Item]] = defaultdict(list)
    for item in ranked:
        groups[item.sector_code or NO_SECTOR_CODE].append(item)

    themes = [
        _theme(code, (members[0].sector_name or NO_SECTOR_NAME) if code else NO_SECTOR_NAME, members, overall)
        for code, members in groups.items()
    ]
    themes.sort(key=lambda t: (-t.item_count, t.code))

    ratings, flows = _rating_counts(ranked), _flow_counts(ranked)
    stats = dict(universe_stats) if universe_stats else None
    market = MarketSummary(
        scored_count=len(ranked),
        avg_score=overall,
        rating_counts=ratings,
        flow_counts=flows,
        universe_stats=stats,
        assessment=_assessment(as_of, len(ranked), overall, ratings, flows, stats),
    )
    return themes, market
