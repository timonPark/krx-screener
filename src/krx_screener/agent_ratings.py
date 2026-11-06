"""에이전트별 등급 판독기 (이슈 #21): 분석 상태 로그의 에이전트별 칸에서 등급을 읽는다.

TradingAgents-KR 서버는 분석을 마치면 상태를 JSON 로그로 저장한다. 서버와 **같은 컴퓨터**의 파일이고 서버 API 로는 받을 수 없다.

    {로그 폴더}/{종목코드}/TradingAgentsStrategy_logs/full_states_log_{기준일}.json

- 에이전트마다 정해진 이름의 칸(``AGENT_FIELDS``)이 있다. 칸이 에이전트를 정하므로 글의 줄 순서로 짐작하지 않는다.
  감성 분석가와 트레이더의 칸에는 5단계 등급이 없어 읽지 않는다 (그래서 에이전트는 10개다).
- 같은 종목·같은 기준일을 다시 분석하면 파일이 덮어써진다. **분석이 끝난 직후에** 읽는다.
- 마크다운 보고서는 등급 줄 수가 제각각이라 읽지 않는다.
- 읽지 못한 칸은 오류가 아니라 그 에이전트만 빈다. 등급을 지어내지 않는다.

한 칸의 글에서 등급을 읽는 규칙 (실제 로그 13개에서 확인한 표기들이다):

- 라벨은 ``등급`` (리서치 매니저는 ``추천`` 도 쓴다), 영어 ``Rating``. ``**등급**: 매수``, ``**등급: 보유.**``,
  ``Aggressive Analyst: **등급**: 보유``, 표 ``| **등급** | **보유** |`` 를 읽는다.
- 라벨은 줄 머리이거나 ``접두 라벨: `` 바로 뒤에 올 때만 라벨이다. 문장 속의 "컨센서스 등급: 매수", "현재 등급: 보유" 는 다른 사람의
  의견이거나 설명이라서 읽지 않는다.
- 값은 5단계 등급의 한국어·영어 표기뿐이다 (대소문자 무시). ``REDUCE`` 같은 다른 값은 읽지 않는다.
- ``최종 거래 제안`` 은 읽지 않는다. 등급을 3단계(BUY/HOLD/SELL)로 뭉갠 값이라서 (비중 축소 → 매도), 5단계 등급 대신 쓰면
  점수가 한쪽으로 치우친다.
- 한 칸에 서로 다른 값이 나오면 어느 쪽이 그 에이전트의 등급인지 알 수 없으므로 비워 둔다 (같은 값이 여러 번이면 그 값이다).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

from krx_screener.analysis import RATING_LABELS

logger = logging.getLogger(__name__)

# 에이전트 → 상태 로그 안의 칸 (중첩이면 순서대로). 실제 로그에서 확인한 이름이다.
AGENT_FIELDS: dict[str, tuple[str, ...]] = {
    "market": ("market_report",),
    "news": ("news_report",),
    "fundamentals": ("fundamentals_report",),
    "bull": ("investment_debate_state", "bull_history"),
    "bear": ("investment_debate_state", "bear_history"),
    "research_manager": ("investment_plan",),
    "aggressive": ("risk_debate_state", "aggressive_history"),
    "conservative": ("risk_debate_state", "conservative_history"),
    "neutral": ("risk_debate_state", "neutral_history"),
    "portfolio_manager": ("final_trade_decision",),
}
AGENT_TOTAL = len(AGENT_FIELDS)

_DEFAULT_LABELS = ("등급", "Rating")
_AGENT_LABELS: dict[str, tuple[str, ...]] = {"research_manager": ("등급", "추천", "Rating")}

# 5단계 등급의 표기 → 내부 값. 한국어는 공백을 없앤 꼴로 비교한다 ("비중 확대" = "비중확대").
_BY_NAME = {label.replace(" ", ""): rating for rating, label in RATING_LABELS.items()}
_BY_NAME.update({rating.lower(): rating for rating in RATING_LABELS})

_NAMES = "|".join(
    sorted(
        {label.replace(" ", r"[ \t]*") for label in RATING_LABELS.values()} | set(RATING_LABELS),
        key=len,
        reverse=True,
    )
)
# 값 바로 뒤에 글자가 붙으면 다른 낱말이다 (보유분, Buyback)
_VALUE = rf"({_NAMES})(?![가-힣A-Za-z])"
_SPACE = r"[ \t]*"  # 줄을 넘지 않는다: 다음 줄의 값은 이 줄의 값이 아니다


def _patterns(labels: tuple[str, ...]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    label = "(?:" + "|".join(re.escape(x) for x in labels) + ")"
    # 문장 속 낱말이 아니라 라벨이어야 한다: 줄 머리(목록·제목 기호 허용)이거나 `접두 라벨: ` 바로 뒤.
    # `**등급**: 매수`, `- **등급**: 매수`, `Aggressive Analyst: **등급**: 보유`, `**투자 논리**: **등급: 보유.**`
    head = rf"(?:^|[:：]){_SPACE}(?:(?:[-•>]|#+|\d+\.){_SPACE})*\**{_SPACE}"
    inline = re.compile(
        rf"{head}{label}{_SPACE}\**{_SPACE}[:：]{_SPACE}\**{_SPACE}{_VALUE}", re.IGNORECASE | re.MULTILINE
    )
    # 표의 한 줄: `| **등급** | **보유** | … |`
    table = re.compile(rf"\|{_SPACE}\**{_SPACE}{label}{_SPACE}\**{_SPACE}\|{_SPACE}\**{_SPACE}{_VALUE}", re.IGNORECASE)
    return inline, table


_PATTERNS = {labels: _patterns(labels) for labels in {_DEFAULT_LABELS, *_AGENT_LABELS.values()}}


def parse_rating(text: str, labels: tuple[str, ...] = _DEFAULT_LABELS) -> str | None:
    """한 칸의 글에서 내부 등급 값(``Buy`` 등)을 읽는다. 읽지 못하거나 값이 서로 엇갈리면 None."""
    found: set[str] = set()
    for pattern in _PATTERNS.get(labels) or _patterns(labels):
        for match in pattern.finditer(text):
            found.add(_BY_NAME[re.sub(r"\s+", "", match.group(1)).lower()])
    return found.pop() if len(found) == 1 else None


def log_path(root: Path, code: str, as_of: date) -> Path:
    return root / code / "TradingAgentsStrategy_logs" / f"full_states_log_{as_of.isoformat()}.json"


def _cell(log: dict[str, Any], path: tuple[str, ...]) -> str | None:
    node: Any = log
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, str) else None


def read_agent_ratings(root: Path, code: str, as_of: date) -> dict[str, str]:
    """끝난 분석의 에이전트별 등급 (에이전트 → 내부 등급 값). 읽은 에이전트만 담긴다.

    파일이 없거나 깨졌거나 다른 종목·기준일의 로그면 빈 딕셔너리다. 오류로 멈추지 않는다.
    """
    path = log_path(root, code, as_of)
    try:
        log = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("분석 상태 로그가 없습니다 (%s): %s", code, path)
        return {}
    except (OSError, ValueError) as exc:  # 읽을 수 없음, 깨진 JSON, UTF-8 이 아님
        logger.warning("분석 상태 로그를 읽지 못했습니다 (%s): %s", code, type(exc).__name__)
        return {}
    if not isinstance(log, dict):
        logger.warning("분석 상태 로그의 구조가 예상과 다릅니다 (%s).", code)
        return {}
    # 안에 적힌 종목·기준일이 다르면 우리 분석의 로그가 아니다. 적혀 있지 않으면 확인할 수 없으니 그대로 읽는다
    for key, expected in (("company_of_interest", code), ("trade_date", as_of.isoformat())):
        if key in log and log[key] != expected:
            logger.warning("분석 상태 로그가 다른 분석의 것입니다 (%s): %s=%r", code, key, log[key])
            return {}

    ratings: dict[str, str] = {}
    for agent, field in AGENT_FIELDS.items():
        text = _cell(log, field)
        rating = parse_rating(text, _AGENT_LABELS.get(agent, _DEFAULT_LABELS)) if text else None
        if rating is not None:
            ratings[agent] = rating
    return ratings
