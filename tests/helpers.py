"""테스트용 시세 생성 도우미."""

from __future__ import annotations

from datetime import date

import pandas as pd


def make_history(closes: list[float], volumes: list[float] | float, as_of: date) -> pd.DataFrame:
    """``as_of`` 로 끝나는 평일 일봉 (Close, Volume)."""
    index = pd.bdate_range(end=pd.Timestamp(as_of), periods=len(closes), name="Date")
    if not isinstance(volumes, list):
        volumes = [volumes] * len(closes)
    return pd.DataFrame({"Close": closes, "Volume": volumes}, index=index, dtype=float)


def make_listing(rows: list[dict]) -> pd.DataFrame:
    """전종목 목록 한 장. 기본값은 오래전에 상장한 평범한 코스피 보통주."""
    base = {"Market": "KOSPI", "Dept": None, "Volume": 100, "Amount": 1000, "ListingDate": "2000-01-01"}
    frame = pd.DataFrame([{**base, **row} for row in rows])
    frame["ListingDate"] = pd.to_datetime(frame["ListingDate"])
    return frame


def passing_closes(n: int = 80) -> list[float]:
    """완만한 상승 추세에 ±5 진동. 마지막 종가는 20·60일선 위, RSI 는 60 안팎."""
    return [1000 + 2 * i + (5 if i % 2 == 0 else -5) for i in range(n)]


def falling_closes(n: int = 80) -> list[float]:
    """완만한 하락 추세에 ±5 진동. 마지막 종가는 20·60일선 아래."""
    return [2000 - 2 * i + (5 if i % 2 == 0 else -5) for i in range(n)]
