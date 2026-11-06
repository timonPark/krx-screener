"""종목별 일봉 시세 조회와 날짜별 캐시.

데이터 소스는 FinanceDataReader 종목별 시세 (keyless, 영숫자 코드도 조회됨). 컬럼은 시가·고가·저가·
종가·거래량이며 **거래대금은 없다.** pykrx 의 날짜별 전종목 일괄 조회는 KRX 로그인 요구로 막혀 쓰지
않는다.

캐시: ``<cache_dir>/prices/<기준일>/<종목코드>.csv``. 같은 기준일로 다시 돌리면 네트워크를 쓰지 않는다.
기준일이 오늘이면 종가가 확정되지 않았을 수 있어 캐시를 읽지도 쓰지도 않는다.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

Fetcher = Callable[[str, date, date], pd.DataFrame]

_COLUMNS = ["Close", "Volume"]
_CODE_RE = re.compile(r"^[0-9A-Z]{6}$")


class PriceFetchError(RuntimeError):
    """시세를 받지 못했을 때."""


def fdr_fetcher(code: str, start: date, end: date) -> pd.DataFrame:
    """FinanceDataReader 로 [start, end] 일봉을 받는다 (end 포함)."""
    import FinanceDataReader as fdr

    try:
        return fdr.DataReader(code, start.isoformat(), end.isoformat())
    except Exception as exc:  # noqa: BLE001 — 네트워크·파싱 오류를 한 가지로 다룸
        raise PriceFetchError(f"{type(exc).__name__}: {exc}") from exc


def normalize(frame: pd.DataFrame | None) -> pd.DataFrame:
    """종가·거래량만 남기고 날짜 오름차순으로 정리한다. 빈 응답은 빈 표로 돌려준다."""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=_COLUMNS, index=pd.DatetimeIndex([], name="Date"), dtype=float)
    missing = [c for c in _COLUMNS if c not in frame.columns]
    if missing:
        raise PriceFetchError(f"시세 응답에 컬럼이 없습니다: {missing}")
    out = frame[_COLUMNS].copy()
    out.index = pd.DatetimeIndex(pd.to_datetime(out.index)).normalize()
    out.index.name = "Date"
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out.astype(float)


class PriceStore:
    """기준일까지의 시세를 캐시와 함께 돌려준다. 스레드에서 동시에 불러도 된다."""

    def __init__(
        self,
        fetcher: Fetcher,
        as_of: date,
        lookback_days: int,
        cache_dir: Path | None,
        today: date,
        retries: int = 1,
        retry_wait: float = 0.5,
        refresh: bool = False,
    ) -> None:
        self._fetcher = fetcher
        self._refresh = refresh  # True 면 캐시를 읽지 않고 새로 받아 덮어쓴다
        self._as_of = as_of
        self._start = as_of - timedelta(days=lookback_days)
        self._retries = retries
        self._retry_wait = retry_wait
        # 오늘 기준일이면 확정되지 않은 값을 캐시하지 않는다.
        use_cache = cache_dir is not None and as_of < today
        self._dir = cache_dir / "prices" / as_of.isoformat() if use_cache and cache_dir else None

    def get(self, code: str) -> pd.DataFrame:
        if not _CODE_RE.fullmatch(code):
            raise PriceFetchError(f"종목코드 형식이 올바르지 않습니다: {code!r}")
        cached = self._read_cache(code)
        if cached is not None:
            return cached
        frame = self._fetch(code)
        self._write_cache(code, frame)
        return frame

    def _fetch(self, code: str) -> pd.DataFrame:
        last: PriceFetchError | None = None
        for attempt in range(self._retries + 1):
            try:
                return normalize(self._fetcher(code, self._start, self._as_of))
            except PriceFetchError as exc:
                last = exc
                if attempt < self._retries:
                    time.sleep(self._retry_wait)
        assert last is not None
        raise last

    def _read_cache(self, code: str) -> pd.DataFrame | None:
        if self._dir is None or self._refresh:
            return None
        path = self._dir / f"{code}.csv"
        try:
            frame = pd.read_csv(path, index_col="Date", parse_dates=["Date"])
            return normalize(frame)
        except (OSError, ValueError, KeyError, PriceFetchError):
            return None  # 없거나 손상된 캐시는 다시 받는다

    def _write_cache(self, code: str, frame: pd.DataFrame) -> None:
        if self._dir is None:
            return
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
            os.close(fd)
            frame.to_csv(tmp)
            os.replace(tmp, self._dir / f"{code}.csv")
        except OSError as exc:
            logger.warning("시세 캐시 저장 실패 (%s): %s", code, exc)
