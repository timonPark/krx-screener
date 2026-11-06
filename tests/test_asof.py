from datetime import date

import pandas as pd
import pytest

from krx_screener.asof import REFERENCE_CODE, AsOfError, resolve_as_of
from krx_screener.prices import PriceFetchError

# 2026-11-02(월) ~ 11-06(금) 거래일
WEEK = pd.bdate_range("2026-11-02", "2026-11-06")


def _fetcher(days=WEEK):
    seen = []

    def fetch(code, start, end):
        seen.append((code, start, end))
        return pd.DataFrame({"Close": 1.0, "Volume": 1.0}, index=days)

    fetch.seen = seen
    return fetch


def test_default_is_last_trading_day_before_today():
    fetch = _fetcher()
    assert resolve_as_of(date(2026, 11, 9), None, fetch) == date(2026, 11, 6)
    assert fetch.seen[0][0] == REFERENCE_CODE


def test_default_skips_today_even_if_today_has_a_row():
    assert resolve_as_of(date(2026, 11, 6), None, _fetcher()) == date(2026, 11, 5)


def test_requested_trading_day_is_accepted():
    assert resolve_as_of(date(2026, 11, 9), date(2026, 11, 4), _fetcher()) == date(2026, 11, 4)


def test_requested_non_trading_day_is_rejected_with_hint():
    with pytest.raises(AsOfError, match="가까운 이전 거래일: 2026-11-06"):
        resolve_as_of(date(2026, 11, 9), date(2026, 11, 7), _fetcher())


def test_future_date_is_rejected_without_fetching():
    fetch = _fetcher()
    with pytest.raises(AsOfError, match="이후"):
        resolve_as_of(date(2026, 11, 9), date(2026, 11, 10), fetch)
    assert fetch.seen == []


def test_no_prior_day_found():
    only_today = pd.DatetimeIndex([pd.Timestamp("2026-11-06")])
    with pytest.raises(AsOfError, match="직전 영업일"):
        resolve_as_of(date(2026, 11, 6), None, _fetcher(only_today))


def test_fetch_failure_becomes_asof_error():
    def broken(code, start, end):
        raise PriceFetchError("연결 실패")

    with pytest.raises(AsOfError, match="연결 실패"):
        resolve_as_of(date(2026, 11, 9), None, broken)
