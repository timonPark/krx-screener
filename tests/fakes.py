"""외부 경계의 가짜들 (이슈 #11). 사용법은 docs/api-testing.md.

웹 API 를 통해 동작을 검증할 때, 바깥 세계와 닿는 여섯 곳(``krx_screener.externals.Externals``)만
이 가짜로 바꿔 끼운다. DB 는 진짜(테스트 전용 PostgreSQL)를 쓴다.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from helpers import make_history, make_listing, passing_closes

from krx_screener.analysis import RATING_LABELS, AnalysisError
from krx_screener.externals import Externals, SectorInfo
from krx_screener.flow import FlowFetchError
from krx_screener.prices import PriceFetchError

AS_OF = date(2026, 11, 5)
CODES = ("028050", "207940", "003490")


class FakePrices:
    """종목코드 → 일봉. ``failing`` 이거나 모르는 종목은 조회 실패로 만든다."""

    def __init__(self, histories: Mapping[str, pd.DataFrame], failing: Iterable[str] = ()) -> None:
        self.histories = dict(histories)
        self.failing = set(failing)
        self.calls: list[str] = []

    def __call__(self, code: str, start: date, end: date) -> pd.DataFrame:
        self.calls.append(code)
        if code in self.failing or code not in self.histories:
            raise PriceFetchError("가짜 시세 조회 실패")
        return self.histories[code]


class FakeTrend:
    """종목코드 → 네이버 수급 행들. ``failing`` 이면 조회 실패, 모르는 종목은 빈 목록(실제 서버와 같다)."""

    def __init__(self, rows: Mapping[str, list[dict[str, Any]]], failing: Iterable[str] = ()) -> None:
        self.rows = dict(rows)
        self.failing = set(failing)
        self.calls: list[str] = []

    def __call__(self, code: str) -> list[dict[str, Any]]:
        self.calls.append(code)
        if code in self.failing:
            raise FlowFetchError("HTTP 503")
        return self.rows.get(code, [])


class FakeSector:
    """종목코드 → 업종 정보. 모르는 종목은 None (읽지 못함)."""

    def __init__(self, info: Mapping[str, SectorInfo]) -> None:
        self.info = dict(info)
        self.calls: list[str] = []

    def __call__(self, code: str) -> SectorInfo | None:
        self.calls.append(code)
        return self.info.get(code)


class FakeAnalysisServer:
    """TradingAgents-KR 서버. 분석은 바로 끝나거나(기본), ``polls_before_done`` 번 처리 중으로 답한 뒤 끝난다."""

    def __init__(
        self,
        decisions: Mapping[str, str] | None = None,
        errors: Iterable[str] = (),
        polls_before_done: int = 0,
        health_error: str | None = None,
    ) -> None:
        self.decisions = dict(decisions or {})
        self.errors = set(errors)
        self.polls_before_done = polls_before_done
        self.health_error = health_error
        self.started: list[tuple[str, str, str]] = []  # (종목코드, 기준일, effort)
        self._polls: dict[str, int] = {}

    def check_health(self) -> None:
        if self.health_error:
            raise AnalysisError(self.health_error)

    def start(self, ticker: str, trade_date: str, effort: str) -> str:
        self.started.append((ticker, trade_date, effort))
        return f"run-{ticker}"

    def snapshot(self, run_id: str) -> dict[str, Any]:
        code = run_id.removeprefix("run-")
        seen = self._polls.get(code, 0)
        self._polls[code] = seen + 1
        if seen < self.polls_before_done:
            return {"status": "running"}
        if code in self.errors:
            return {"status": "error", "error": "가짜 서버 오류"}
        return {"status": "done", "decision": self.decisions.get(code, "Hold")}


# 에이전트 → 상태 로그 안의 위치. 실제 로그(full_states_log_<기준일>.json)에서 확인한 칸이다.
# 감성 분석가(sentiment_report)와 트레이더(trader_investment_plan)에는 `등급` 줄이 없다.
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


def write_state_log(
    root: Path,
    code: str,
    as_of: date,
    ratings: Mapping[str, str | None] | None = None,
    effort: str = "low",
) -> Path:
    """실제 로그 모양의 분석 상태 로그를 ``root`` 아래에 쓴다.

    ``ratings`` 는 에이전트 → 영어 등급(``Buy`` 등). 빠진 에이전트는 ``Hold`` 이고, 값을 ``None`` 으로 주면
    그 칸을 비워서 "읽지 못한 칸"을 흉내 낸다.
    """
    ratings = ratings or {}
    log: dict[str, Any] = {
        "company_of_interest": code,
        "trade_date": as_of.isoformat(),
        "market_report": "",
        "sentiment_report": "감성 분석 보고서",
        "news_report": "",
        "fundamentals_report": "",
        "investment_debate_state": {"bull_history": "", "bear_history": "", "history": "", "current_response": ""},
        "trader_investment_plan": "트레이더 계획",
        "risk_debate_state": {"aggressive_history": "", "conservative_history": "", "neutral_history": "", "history": ""},
        "investment_plan": "",
        "final_trade_decision": "",
        "final_rating": ratings.get("portfolio_manager") or "Hold",
        "run_settings": {"effort": effort, "quick_effort": effort},
    }
    for agent, path in AGENT_FIELDS.items():
        rating = ratings.get(agent, "Hold")
        if rating is None:
            continue
        label = RATING_LABELS[rating]
        text = f"## 요약\n\n**등급**: {label}\n**최종 거래 제안**: {label}\n"
        target = log
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = text
    out = root / code / "TradingAgentsStrategy_logs" / f"full_states_log_{as_of.isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(log, ensure_ascii=False), encoding="utf-8")
    return out


def trend_rows(as_of: date = AS_OF, foreign: int = 100, institution: int = 50, days: int = 10) -> list[dict[str, Any]]:
    """네이버 수급 응답 모양의 행들 (최신이 먼저). 기준일로 끝나는 ``days`` 거래일."""
    index = pd.bdate_range(end=pd.Timestamp(as_of), periods=days)

    def fmt(n: int) -> str:
        return f"{n:+,}" if n else "0"

    return [
        {"bizdate": d.strftime("%Y%m%d"), "foreignerPureBuyQuant": fmt(foreign), "organPureBuyQuant": fmt(institution)}
        for d in reversed(index)
    ]


def make_externals(state_log_root: Path, **overrides: Any) -> Externals:
    """기본 세계(종목 3개, 모두 필터를 통과하고 분석은 바로 끝남)로 외부 경계를 만든다.

    바꾸고 싶은 경계는 이름으로 덮어쓴다: ``make_externals(root, prices=FakePrices({...}))``.
    """
    listing = make_listing(
        [
            {"Code": "028050", "Name": "삼성E&A"},
            {"Code": "207940", "Name": "삼성바이오로직스"},
            {"Code": "003490", "Name": "대한항공"},
        ]
    )
    base = Externals(
        listing=lambda: listing.copy(),
        prices=FakePrices({c: make_history(passing_closes(), 3_000_000, AS_OF) for c in CODES}),
        trend=FakeTrend({c: trend_rows() for c in CODES}),
        sector=FakeSector(
            {
                "028050": SectorInfo("26", "건설", "플랜트 전 분야의 종합 솔루션을 제공하는 EPC 전문기업"),
                "207940": SectorInfo("21", "제약", "바이오의약품 위탁개발생산"),
                "003490": SectorInfo("33", "항공사", "여객·화물 운송"),
            }
        ),
        analysis=FakeAnalysisServer(),
        state_log_root=state_log_root,
    )
    return replace(base, **overrides)
