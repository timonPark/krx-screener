"""1단계 스크리닝: 유니버스 제외 → 시세 필터.

모델을 쓰지 않고 시세만으로 판정한다. 데이터가 없다는 이유로 종목을 조용히 버리지 않는다 —
시세를 받지 못했거나 이력이 부족한 종목은 "판정 불가"로 사유와 함께 따로 기록한다.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from krx_screener.config import Config, Filters
from krx_screener.flow import FlowStats
from krx_screener.indicators import avg_trading_value_approx, rsi_wilder, sma
from krx_screener.prices import PriceFetchError, PriceStore

# 목록의 종류 정보만으로 판정하는 제외 종류. zero_volume 은 기준일 시세가 필요해서 따로 처리한다.
STATIC_KEYS: tuple[str, ...] = (
    "preferred",
    "spac",
    "managed",
    "new_listing",
    "reit",
    "caution",
    "foreign",
)

EVALUATED = "evaluated"
ZERO_VOLUME = "zero_volume"
UNDETERMINED = "undetermined"


@dataclass(frozen=True)
class Evaluation:
    status: str
    reason: str = ""
    metrics: dict[str, float] = field(default_factory=dict)
    passes: dict[str, bool] = field(default_factory=dict)


def condition_names(filters: Filters) -> list[str]:
    """탈락 사유를 세는 순서. 첫 번째로 만족하지 못한 조건이 그 종목의 탈락 사유가 된다."""
    return ["avg_trading_value", *[f"above_ma{n}" for n in filters.price_above_ma], "rsi"]


def evaluate(
    history: pd.DataFrame, filters: Filters, as_of: date, exclude_zero_volume: bool
) -> Evaluation:
    """기준일 시세로 필터 조건을 판정한다. ``history`` 는 ``Close``/``Volume`` 컬럼의 일봉."""
    hist = history.loc[history.index <= pd.Timestamp(as_of)].dropna(subset=["Close", "Volume"])
    if hist.empty:
        return Evaluation(UNDETERMINED, "시세 없음")

    has_as_of_row = hist.index[-1].date() == as_of
    if not has_as_of_row:
        # 기준일 봉이 없는 종목: 거래정지 의심. 제외하지 않는 설정이면 기준일 값을 못 구해 판정 불가.
        if exclude_zero_volume:
            return Evaluation(ZERO_VOLUME, "기준일 시세 없음")
        return Evaluation(UNDETERMINED, "기준일 시세 없음")
    if exclude_zero_volume and hist["Volume"].iloc[-1] == 0:
        return Evaluation(ZERO_VOLUME, "기준일 거래량 0")

    if len(hist) < filters.required_rows:
        return Evaluation(UNDETERMINED, f"시세 부족 ({len(hist)}거래일 < {filters.required_rows})")

    close, volume = hist["Close"], hist["Volume"]
    metrics: dict[str, float] = {"close": float(close.iloc[-1])}
    passes: dict[str, bool] = {}

    avg_tv = avg_trading_value_approx(close, volume, filters.avg_trading_value_window)
    rsi = rsi_wilder(close, filters.rsi_period)
    mas = {n: sma(close, n) for n in filters.price_above_ma}
    if avg_tv is None or rsi is None or any(v is None for v in mas.values()):
        return Evaluation(UNDETERMINED, "지표 계산 불가")

    metrics["avg_trading_value_approx"] = avg_tv
    passes["avg_trading_value"] = avg_tv >= filters.min_avg_trading_value_krw
    for n, value in mas.items():
        assert value is not None
        metrics[f"sma{n}"] = value
        passes[f"above_ma{n}"] = metrics["close"] > value
    metrics["rsi"] = rsi
    passes["rsi"] = rsi < filters.rsi_max
    return Evaluation(EVALUATED, metrics=metrics, passes=passes)


def apply_static_exclusions(
    classified: pd.DataFrame, exclude: frozenset[str]
) -> tuple[pd.DataFrame, dict[str, int]]:
    """목록의 종류 정보로 유니버스를 줄인다. 종류별 개수는 겹침을 그대로 센다 (합 ≥ 제외된 수)."""
    keys = [k for k in STATIC_KEYS if k in exclude]
    by_kind = {k: int(classified[f"is_{k}"].sum()) for k in keys}
    if not keys:
        return classified.copy(), by_kind
    mask = classified[[f"is_{k}" for k in keys]].any(axis=1)
    return classified[~mask].copy(), by_kind


@dataclass
class ScreenResult:
    as_of: date
    candidates: pd.DataFrame
    listing_total: int
    static_excluded_by_kind: dict[str, int]
    after_static: int
    zero_volume_excluded: int
    undetermined: list[dict[str, str]]
    evaluated: int
    drop_first: dict[str, int]  # 첫 번째로 만족하지 못한 조건별 탈락 수 (합 = 탈락 수)
    fail_each: dict[str, int]  # 조건별로 독립적으로 센 불충족 수 (겹침 있음, 임계값 조정용)
    flow: FlowStats | None = None  # 1.5단계를 돌렸을 때만


def run_screen(
    classified: pd.DataFrame,
    config: Config,
    as_of: date,
    store: PriceStore,
    workers: int = 8,
    progress: Callable[[int, int], None] | None = None,
) -> ScreenResult:
    filters = config.filters
    universe, by_kind = apply_static_exclusions(classified, config.exclude)
    exclude_zero = "zero_volume" in config.exclude

    rows = universe.to_dict("records")

    def work(row: dict) -> Evaluation:
        try:
            history = store.get(str(row["Code"]))
        except PriceFetchError as exc:
            return Evaluation(UNDETERMINED, f"시세 조회 실패: {exc}")
        return evaluate(history, filters, as_of, exclude_zero)

    evaluations: list[Evaluation] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for done, evaluation in enumerate(pool.map(work, rows), start=1):
            evaluations.append(evaluation)
            if progress:
                progress(done, len(rows))

    order = condition_names(filters)
    drop_first = dict.fromkeys(order, 0)
    fail_each = dict.fromkeys(order, 0)
    undetermined: list[dict[str, str]] = []
    zero_volume = 0
    evaluated = 0
    candidate_rows: list[dict] = []

    for row, ev in zip(rows, evaluations, strict=True):
        if ev.status == ZERO_VOLUME:
            zero_volume += 1
            continue
        if ev.status == UNDETERMINED:
            undetermined.append(
                {"code": str(row["Code"]), "name": str(row["Name"]), "reason": ev.reason}
            )
            continue
        evaluated += 1
        failed = [name for name in order if not ev.passes[name]]
        for name in failed:
            fail_each[name] += 1
        if failed:
            drop_first[failed[0]] += 1
            continue
        candidate_rows.append(
            {
                "code": str(row["Code"]),
                "name": str(row["Name"]),
                "market": str(row["Market"]),
                **{k: _round_metric(k, v) for k, v in ev.metrics.items()},
                "is_caution": bool(row["is_caution"]),
                "is_foreign": bool(row["is_foreign"]),
            }
        )

    candidates = pd.DataFrame(candidate_rows)
    if not candidates.empty:
        candidates = candidates.sort_values(
            "avg_trading_value_approx", ascending=False, ignore_index=True
        )

    return ScreenResult(
        as_of=as_of,
        candidates=candidates,
        listing_total=len(classified),
        static_excluded_by_kind=by_kind,
        after_static=len(universe),
        zero_volume_excluded=zero_volume,
        undetermined=undetermined,
        evaluated=evaluated,
        drop_first=drop_first,
        fail_each=fail_each,
    )


def _round_metric(key: str, value: float) -> float:
    if key in {"close", "avg_trading_value_approx"}:
        return round(value)
    return round(value, 1)
