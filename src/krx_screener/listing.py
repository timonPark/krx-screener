"""목록 생성 (이슈 #14): 기준일의 시세만으로 분석 대상 목록을 만든다. 모델도 계정도 쓰지 않는다.

기존 1단계 동작을 세션이 쓰는 모양으로 감싼다.

1. 전종목 목록을 받아 종류(우선주·스팩·관리종목·리츠·신규상장 …)를 표시한다.
2. 설정에서 제외하기로 한 종류와 거래정지 의심 종목을 뺀다. 투자주의환기·외국기업은 빼지 않고 표시만 한다.
3. 시세 필터를 적용한다: 20거래일 평균 거래대금(종가×거래량 근사), 종가 > 20일선, 종가 > 60일선.
   RSI 는 값만 저장하고 조건으로 쓰지 않는다 (설정에 ``rsi_max`` 가 없을 때).
4. 평균 거래대금이 큰 순서로 순번을 매긴다 (1부터).
5. 목록에 남은 종목에만 수급을 조회해 상태와 순매수 합계를 채운다 (#15). **표시만 하고 종목을 거르지 않는다.**
6. 목록에 남은 종목에만 업종 코드·업종명·기업 설명을 조회해 채운다 (#16). 읽지 못한 종목은 빈 칸이다.

시세를 받지 못했거나 이력이 부족한 종목은 목록에 넣지 않는다 (지어내지 않는다).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from krx_screener import universe
from krx_screener.config import Config
from krx_screener.externals import Externals
from krx_screener.flow import fetch_flow
from krx_screener.prices import PriceStore
from krx_screener.repository import Item
from krx_screener.screen import ScreenResult, lookback_days, map_with_progress, run_screen

logger = logging.getLogger(__name__)

Progress = Callable[[str, int, int], None]  # (단계, 한 개수, 전체 개수)


def _no_progress(stage: str, done: int, total: int) -> None:
    pass


def _to_item(seq: int, row: dict[str, Any]) -> Item:
    return Item(
        seq=seq,
        code=str(row["code"]),
        name=str(row["name"]),
        market=str(row["market"]),
        close=int(round(row["close"])),
        avg_trading_value=int(round(row["avg_trading_value_approx"])),
        rsi=float(row["rsi"]),
        is_caution=bool(row["is_caution"]),
        is_foreign=bool(row["is_foreign"]),
    )


def _apply_flow(
    items: list[Item], externals: Externals, config: Config, as_of: date, workers: int, retry_wait: float,
    progress: Progress | None = None,
) -> None:
    """종목마다 수급 상태와 순매수 합계를 채운다. 읽지 못하면 ``unconfirmed`` 이고 합계는 빈 값이다.

    설정의 ``flow.mode`` 와 상관없이 종목을 거르지 않는다 (스펙: 수급은 표시만 한다).
    """
    window = config.flow.window_days
    report = progress or _no_progress
    report("flow", 0, len(items))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        readings = map_with_progress(
            pool,
            lambda it: fetch_flow(it.code, as_of, window, externals.trend, retry_wait=retry_wait),
            items,
            lambda done, total: report("flow", done, total),
        )
    for item, reading in zip(items, readings, strict=True):
        item.flow_status = reading.status
        item.foreign_net_5d = reading.foreign
        item.institution_net_5d = reading.institution


def _read_sector(externals: Externals, code: str):
    """업종 조회는 어떤 이유로든 목록 만들기를 멈추게 하지 않는다 (읽지 못하면 빈 칸)."""
    try:
        return externals.sector(code)
    except Exception:  # noqa: BLE001 — 외부 경계의 예측하지 못한 실패가 세션 생성 전체를 막으면 안 된다
        logger.warning("업종 조회 실패 (%s) — 빈 칸으로 둔다", code, exc_info=True)
        return None


def _apply_sector(items: list[Item], externals: Externals, workers: int, progress: Progress | None = None) -> None:
    """종목마다 업종 코드·업종명·기업 설명을 채운다. 읽지 못한 종목은 세 값 모두 빈 칸이다."""
    report = progress or _no_progress
    report("sector", 0, len(items))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        infos = map_with_progress(
            pool, lambda it: _read_sector(externals, it.code), items, lambda done, total: report("sector", done, total)
        )
    for item, info in zip(items, infos, strict=True):
        if info is not None:
            item.sector_code = info.code
            item.sector_name = info.name
            item.description = info.description


def universe_stats(result: ScreenResult) -> dict:
    """1단계 결과에서 전종목 수치만 뽑는다 (종목별 내용은 넣지 않는다)."""
    return {
        "listing_total": result.listing_total,
        "after_static": result.after_static,
        "evaluated": result.evaluated,
        "candidates": len(result.candidates),
        "undetermined": len(result.undetermined),
        "zero_volume_excluded": result.zero_volume_excluded,
        "fail_each": dict(result.fail_each),
    }


def build_items(
    externals: Externals,
    config: Config,
    as_of: date,
    today: date,
    *,
    workers: int = 8,
    cache_dir: Path | None = None,
    retry_wait: float = 0.5,
    progress: Progress | None = None,
) -> tuple[list[Item], dict]:
    """기준일의 목록을 순번 순서의 종목들과, 그 목록을 만들며 센 전종목 수치로 돌려준다 (저장은 호출하는 쪽이 한다).

    전종목 수치(``universe_stats``)는 시세를 확인한 종목 수와 조건별 불충족 수 같은 **개수**다. 이 값은 시세를 다 읽는 이 순간에만 알 수 있어서
    세션에 같이 저장하고, 정리 때 종합 평가에 쓴다 (``summary``).

    ``progress(단계, 한 개수, 전체 개수)`` 는 단계(``prices`` → ``flow`` → ``sector``)가 시작될 때와 종목 하나가 끝날 때마다 불린다.
    언제나 이 함수를 부른 스레드에서 불리므로 받는 쪽이 잠금을 걸 필요가 없다.
    """
    report = progress or _no_progress
    report("prices", 0, 0)  # 전체 개수는 제외 규칙을 적용한 뒤에야 안다
    classified = universe.classify(externals.listing(), as_of)
    store = PriceStore(
        externals.prices,
        as_of,
        lookback_days(config.filters),
        cache_dir,
        today=today,
        retry_wait=retry_wait,
    )
    result = run_screen(
        classified, config, as_of, store, workers=workers, progress=lambda done, total: report("prices", done, total)
    )
    rows = result.candidates.to_dict("records") if not result.candidates.empty else []
    items = [_to_item(seq, row) for seq, row in enumerate(rows, start=1)]
    _apply_flow(items, externals, config, as_of, workers, retry_wait, report)
    _apply_sector(items, externals, workers, report)
    return items, universe_stats(result)
