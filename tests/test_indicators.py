import pandas as pd
import pytest

from krx_screener.indicators import avg_trading_value_approx, rsi_wilder, sma


def test_sma_uses_last_period_values():
    assert sma(pd.Series([1, 2, 3, 4, 5]), 3) == 4.0


def test_sma_needs_enough_values():
    assert sma(pd.Series([1, 2]), 3) is None


def test_rsi_matches_hand_computed_wilder_value():
    # 변화 +1, -1, +2, period 2: 평균 이득/손실 0.5/0.5 → 새 값 반영 후 1.25/0.25 → RS 5 → RSI 83.33
    assert rsi_wilder(pd.Series([10, 11, 10, 12]), 2) == pytest.approx(83.3333, abs=1e-3)


def test_rsi_extremes():
    assert rsi_wilder(pd.Series(range(1, 20)), 14) == 100.0
    assert rsi_wilder(pd.Series(range(20, 1, -1)), 14) == 0.0
    assert rsi_wilder(pd.Series([5.0] * 20), 14) == 50.0


def test_rsi_needs_period_plus_one_closes():
    assert rsi_wilder(pd.Series(range(14)), 14) is None
    assert rsi_wilder(pd.Series(range(15)), 14) is not None


def test_avg_trading_value_is_close_times_volume_mean():
    close, volume = pd.Series([10.0, 20.0]), pd.Series([3.0, 4.0])
    assert avg_trading_value_approx(close, volume, 2) == 55.0
    assert avg_trading_value_approx(close, volume, 3) is None
