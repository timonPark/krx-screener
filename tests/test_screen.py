from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from helpers import falling_closes, make_history, make_listing, passing_closes

from krx_screener import universe as uni
from krx_screener.config import Filters, load_config
from krx_screener.prices import PriceFetchError
from krx_screener.screen import (
    EVALUATED,
    UNDETERMINED,
    ZERO_VOLUME,
    apply_static_exclusions,
    evaluate,
    run_screen,
)

AS_OF = date(2026, 11, 6)
FILTERS = Filters(
    min_avg_trading_value_krw=1_000_000_000,
    avg_trading_value_window=20,
    price_above_ma=(20, 60),
    rsi_period=14,
    rsi_max=70.0,
)
CONFIG = load_config(Path(__file__).resolve().parents[1] / "config.yaml")


def _ev(closes, volume=3_000_000, exclude_zero=True, as_of=AS_OF):
    return evaluate(make_history(closes, volume, as_of), FILTERS, as_of, exclude_zero)


def test_passing_stock_meets_every_condition():
    ev = _ev(passing_closes())
    assert ev.status == EVALUATED
    assert all(ev.passes.values()), ev.passes
    assert ev.metrics["close"] < ev.metrics["avg_trading_value_approx"]
    assert ev.metrics["rsi"] < 70


def test_falling_stock_fails_both_moving_averages():
    ev = _ev(falling_closes())
    assert ev.status == EVALUATED
    assert ev.passes["above_ma20"] is False
    assert ev.passes["above_ma60"] is False
    assert ev.passes["avg_trading_value"] is True


def test_low_volume_fails_trading_value_only():
    ev = _ev(passing_closes(), volume=10)
    assert [k for k, ok in ev.passes.items() if not ok] == ["avg_trading_value"]


def test_overheated_stock_fails_rsi():
    ev = _ev([1000.0 + 10 * i for i in range(80)])  # 매일 상승 → RSI 100
    assert ev.passes["rsi"] is False
    assert ev.metrics["rsi"] == 100.0


def test_zero_volume_on_as_of_is_excluded_or_evaluated_by_setting():
    volumes = [3_000_000.0] * 79 + [0.0]
    hist = make_history(passing_closes(), volumes, AS_OF)
    assert evaluate(hist, FILTERS, AS_OF, True).status == ZERO_VOLUME
    assert evaluate(hist, FILTERS, AS_OF, False).status == EVALUATED


def test_missing_as_of_row_is_suspected_halt_or_undetermined():
    hist = make_history(passing_closes(), 3_000_000, date(2026, 11, 5))  # 기준일 전날까지만 있음
    assert evaluate(hist, FILTERS, AS_OF, True).status == ZERO_VOLUME
    ev = evaluate(hist, FILTERS, AS_OF, False)
    assert ev.status == UNDETERMINED
    assert "기준일 시세 없음" in ev.reason


def test_too_short_or_empty_history_is_undetermined_not_dropped():
    short = _ev(passing_closes(30))
    assert short.status == UNDETERMINED
    assert "시세 부족" in short.reason
    empty = evaluate(pd.DataFrame(columns=["Close", "Volume"], index=pd.DatetimeIndex([])), FILTERS, AS_OF, True)
    assert empty.status == UNDETERMINED


def test_rows_after_as_of_are_ignored():
    hist = make_history(passing_closes(80), 3_000_000, date(2026, 11, 13))  # 기준일 이후 5거래일 포함
    ev = evaluate(hist, FILTERS, AS_OF, True)
    assert ev.status == EVALUATED
    assert ev.metrics["close"] == passing_closes(80)[-6]  # 11-13 이 80번째 → 11-06 은 75번째


class FakeStore:
    def __init__(self, histories):
        self.histories = histories
        self.calls: list[str] = []

    def get(self, code):
        self.calls.append(code)
        item = self.histories[code]
        if isinstance(item, Exception):
            raise item
        return item


def _screen_fixture():
    listing = make_listing(
        [
            {"Code": "000010", "Name": "통과전자"},
            {"Code": "000025", "Name": "통과전자우"},  # 우선주 (끝자리 5)
            {"Code": "000030", "Name": "조회실패"},
            {"Code": "000040", "Name": "거래정지"},
            {"Code": "000050", "Name": "하락중"},
            {"Code": "000060", "Name": "환기종목", "Dept": "투자주의환기종목(소속부없음)"},
            {"Code": "000070", "Name": "외국기업", "Dept": "외국기업(소속부없음)", "Market": "KOSDAQ"},
            {"Code": "000080", "Name": "거래부진"},
            {"Code": "000090", "Name": "대신밸류리츠"},
        ]
    )
    up = passing_closes()
    histories = {
        "000010": make_history(up, 3_000_000, AS_OF),
        "000030": PriceFetchError("연결 실패"),
        "000040": make_history(up, [3_000_000.0] * 79 + [0.0], AS_OF),
        "000050": make_history(falling_closes(), 3_000_000, AS_OF),
        "000060": make_history(up, 2_000_000, AS_OF),
        "000070": make_history(up, 1_000_000, AS_OF),
        "000080": make_history(up, 10, AS_OF),
    }
    return uni.classify(listing, AS_OF), histories


def test_run_screen_end_to_end():
    classified, histories = _screen_fixture()
    store = FakeStore(histories)
    result = run_screen(classified, CONFIG, AS_OF, store, workers=3)

    assert result.listing_total == 9
    assert result.static_excluded_by_kind == {"preferred": 1, "reit": 1, "spac": 0, "managed": 0, "new_listing": 0}
    assert result.after_static == 7
    assert result.zero_volume_excluded == 1
    assert [u["code"] for u in result.undetermined] == ["000030"]
    assert "연결 실패" in result.undetermined[0]["reason"]
    assert result.evaluated == 5

    # 제외된 종류는 시세를 조회하지도 않는다
    assert "000025" not in store.calls
    assert "000090" not in store.calls

    # 통과: 평균 거래대금 큰 순서. 투자주의환기·외국기업은 제외되지 않고 표시만 붙는다
    assert result.candidates["code"].tolist() == ["000010", "000060", "000070"]
    flags = result.candidates.set_index("code")
    assert bool(flags.loc["000060", "is_caution"]) is True
    assert bool(flags.loc["000070", "is_foreign"]) is True
    assert bool(flags.loc["000010", "is_caution"]) is False
    assert flags.loc["000070", "market"] == "KOSDAQ"

    assert result.drop_first == {"avg_trading_value": 1, "above_ma20": 1, "above_ma60": 0, "rsi": 0}
    assert result.fail_each == {"avg_trading_value": 1, "above_ma20": 1, "above_ma60": 1, "rsi": 0}
    # 판정 대상 = 후보 + 탈락
    assert result.evaluated == len(result.candidates) + sum(result.drop_first.values())


def test_run_screen_reports_progress():
    classified, histories = _screen_fixture()
    seen = []
    run_screen(classified, CONFIG, AS_OF, FakeStore(histories), workers=2, progress=lambda d, t: seen.append((d, t)))
    assert seen[-1] == (7, 7)
    assert [d for d, _ in seen] == list(range(1, 8))


def test_run_screen_with_no_candidates_returns_empty_frame():
    classified, histories = _screen_fixture()
    only_falling = classified[classified["Code"] == "000050"].reset_index(drop=True)
    result = run_screen(only_falling, CONFIG, AS_OF, FakeStore(histories))
    assert result.candidates.empty
    assert result.drop_first["above_ma20"] == 1


def test_static_exclusions_count_overlaps_but_remove_once():
    listing = make_listing(
        [
            {"Code": "000010", "Name": "A"},
            {"Code": "000025", "Name": "B우", "Dept": "관리종목(소속부없음)"},  # 우선주이자 관리종목
        ]
    )
    kept, by_kind = apply_static_exclusions(uni.classify(listing, AS_OF), frozenset({"preferred", "managed"}))
    assert by_kind == {"preferred": 1, "managed": 1}
    assert kept["Code"].tolist() == ["000010"]


@pytest.mark.parametrize("keys", [frozenset(), frozenset({"zero_volume"})])
def test_no_static_exclusion_keeps_everyone(keys):
    listing = make_listing([{"Code": "000010", "Name": "A"}, {"Code": "000025", "Name": "B우"}])
    kept, by_kind = apply_static_exclusions(uni.classify(listing, AS_OF), keys)
    assert len(kept) == 2
    assert by_kind == {}
