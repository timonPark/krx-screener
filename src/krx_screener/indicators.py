"""시세 기반 지표 계산. 모두 순수 함수이며 계산할 수 없으면 None 을 돌려준다."""

from __future__ import annotations

import pandas as pd


def sma(close: pd.Series, period: int) -> float | None:
    """마지막 ``period`` 개 종가의 단순 이동평균."""
    if len(close) < period:
        return None
    return float(close.iloc[-period:].mean())


def rsi_wilder(close: pd.Series, period: int) -> float | None:
    """Wilder 방식 RSI. 종가가 ``period + 1`` 개 미만이면 None.

    처음 ``period`` 개 변화의 단순 평균으로 시작해 이후 ``(평균 × (period-1) + 새 값) / period`` 로
    평활한다. 하락이 전혀 없으면 100, 상승·하락이 모두 없으면(가격 불변) 50 을 돌려준다.
    """
    if len(close) < period + 1:
        return None
    delta = close.diff().dropna().to_numpy()
    gains = [max(d, 0.0) for d in delta]
    losses = [max(-d, 0.0) for d in delta]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def avg_trading_value_approx(close: pd.Series, volume: pd.Series, window: int) -> float | None:
    """최근 ``window`` 거래일 평균 거래대금의 **근사값** (종가 × 거래량의 평균).

    실제 거래대금은 체결마다 가격이 달라서 이 값과 약간 다르다. FinanceDataReader 종목별 시세에는
    거래대금 컬럼이 없어서 근사한다.
    """
    if len(close) < window or len(volume) < window:
        return None
    return float((close.iloc[-window:] * volume.iloc[-window:]).mean())
