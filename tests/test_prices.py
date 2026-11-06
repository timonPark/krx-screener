from datetime import date

import pandas as pd
import pytest
from helpers import make_history

from krx_screener.prices import PriceFetchError, PriceStore, normalize

AS_OF = date(2026, 11, 6)
TODAY = date(2026, 11, 9)


class FakeFetcher:
    def __init__(self, failures: int = 0):
        self.calls: list[tuple[str, date, date]] = []
        self.failures = failures

    def __call__(self, code, start, end):
        self.calls.append((code, start, end))
        if len(self.calls) <= self.failures:
            raise PriceFetchError("일시 오류")
        frame = make_history([100.0, 101.0, 102.0], 10, AS_OF)
        frame["Open"] = 1.0  # 정규화가 종가·거래량만 남기는지 확인
        return frame


def _store(fetcher, tmp_path, **kwargs):
    return PriceStore(fetcher, AS_OF, 100, tmp_path, today=kwargs.pop("today", TODAY), retry_wait=0, **kwargs)


def test_normalize_keeps_close_and_volume_sorted_and_deduped():
    raw = make_history([1.0, 2.0, 3.0], 5, AS_OF)
    raw = pd.concat([raw.iloc[::-1], raw.iloc[[0]]])  # 뒤집고 첫 행을 중복
    raw["Open"] = 0
    out = normalize(raw)
    assert list(out.columns) == ["Close", "Volume"]
    assert out.index.is_monotonic_increasing
    assert not out.index.has_duplicates
    assert len(out) == 3


def test_normalize_empty_and_missing_columns():
    assert normalize(None).empty
    assert normalize(pd.DataFrame()).empty
    with pytest.raises(PriceFetchError, match="컬럼"):
        normalize(pd.DataFrame({"Close": [1.0]}, index=pd.to_datetime(["2026-11-06"])))


def test_second_call_uses_cache(tmp_path):
    fetcher = FakeFetcher()
    store = _store(fetcher, tmp_path)
    first = store.get("005930")
    second = store.get("005930")
    assert len(fetcher.calls) == 1
    assert (tmp_path / "prices" / "2026-11-06" / "005930.csv").exists()
    pd.testing.assert_frame_equal(first, second, check_freq=False)


def test_cache_is_shared_between_store_instances(tmp_path):
    fetcher = FakeFetcher()
    _store(fetcher, tmp_path).get("0126Z0")
    _store(fetcher, tmp_path).get("0126Z0")
    assert len(fetcher.calls) == 1


def test_refresh_refetches_and_rewrites_cache(tmp_path):
    fetcher = FakeFetcher()
    _store(fetcher, tmp_path).get("005930")
    _store(fetcher, tmp_path, refresh=True).get("005930")
    assert len(fetcher.calls) == 2


def test_today_as_of_is_never_cached(tmp_path):
    fetcher = FakeFetcher()
    store = _store(fetcher, tmp_path, today=AS_OF)
    store.get("005930")
    store.get("005930")
    assert len(fetcher.calls) == 2
    assert not (tmp_path / "prices").exists()


def test_corrupt_cache_is_refetched(tmp_path):
    fetcher = FakeFetcher()
    store = _store(fetcher, tmp_path)
    store.get("005930")
    (tmp_path / "prices" / "2026-11-06" / "005930.csv").write_text("garbage\x00", encoding="utf-8")
    assert not store.get("005930").empty
    assert len(fetcher.calls) == 2


def test_retry_then_success(tmp_path):
    fetcher = FakeFetcher(failures=1)
    assert not _store(fetcher, tmp_path).get("005930").empty
    assert len(fetcher.calls) == 2


def test_gives_up_after_retries(tmp_path):
    fetcher = FakeFetcher(failures=5)
    with pytest.raises(PriceFetchError, match="일시 오류"):
        _store(fetcher, tmp_path).get("005930")
    assert len(fetcher.calls) == 2  # 처음 1번 + 재시도 1번


@pytest.mark.parametrize("code", ["../etc/passwd", "5930", "ABC", "00593O!", ""])
def test_rejects_unsafe_codes_without_fetching(tmp_path, code):
    fetcher = FakeFetcher()
    with pytest.raises(PriceFetchError, match="형식"):
        _store(fetcher, tmp_path).get(code)
    assert fetcher.calls == []
