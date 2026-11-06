"""외부 경계: 백엔드가 바깥 세계와 닿는 여섯 곳 (이슈 #11).

백엔드의 나머지 코드는 이 경계를 통해서만 바깥과 닿는다. 그래서 테스트는 이 경계만 가짜로 바꾸고
웹 API 를 통해 동작을 검증한다 (가짜 만드는 법은 ``docs/api-testing.md``).

| 경계 | 하는 일 | 실제 구현 |
|---|---|---|
| ``listing`` | 코스피·코스닥 전종목 목록 | ``universe.fetch_listing`` |
| ``prices`` | 종목별 일봉 시세 | ``prices.fdr_fetcher`` |
| ``trend`` | 투자자별 수급 (네이버) | ``flow.naver_trend`` |
| ``sector`` | 업종 코드·업종명·기업 설명 | 이슈 #16 에서 구현 |
| ``analysis`` | TradingAgents-KR 분석 서버 | ``analysis.AnalysisClient`` |
| ``state_log_root`` | 분석 상태 로그가 쌓이는 폴더 | 이슈 #21 에서 읽기 구현 |
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from krx_screener.flow import TrendFetcher
from krx_screener.prices import Fetcher as PriceFetcher


@dataclass(frozen=True)
class SectorInfo:
    """종목의 업종 정보. 읽지 못한 값은 빈 문자열이다."""

    code: str
    name: str
    description: str


# 전종목 목록: Code, Name, Market, Dept, Volume, Amount, ListingDate 컬럼의 표
ListingSource = Callable[[], pd.DataFrame]
# 종목코드 → 업종 정보. 읽지 못하면 None (오류로 멈추지 않는다)
SectorFetcher = Callable[[str], SectorInfo | None]


class AnalysisServer(Protocol):
    """TradingAgents-KR 서버. ``analysis.AnalysisClient`` 가 이 모양이다."""

    def check_health(self) -> None: ...

    def start(self, ticker: str, trade_date: str, effort: str) -> str: ...

    def snapshot(self, run_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Externals:
    listing: ListingSource
    prices: PriceFetcher
    trend: TrendFetcher
    sector: SectorFetcher
    analysis: AnalysisServer
    state_log_root: Path
