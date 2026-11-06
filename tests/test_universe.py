from datetime import date

import pandas as pd
import pytest

from krx_screener import universe as uni

AS_OF = date(2026, 11, 6)


def _listing(rows: list[dict]) -> pd.DataFrame:
    base = {"Market": "KOSPI", "Dept": None, "Volume": 100, "Amount": 1000, "ListingDate": "2000-01-01"}
    frame = pd.DataFrame([{**base, **row} for row in rows])
    frame["ListingDate"] = pd.to_datetime(frame["ListingDate"])
    return frame


def _kinds(frame: pd.DataFrame, code: str) -> set[str]:
    row = frame.loc[frame["Code"] == code].iloc[0]
    return {key for key in uni.KIND_LABELS if row[key]}


def test_preferred_is_decided_by_code_suffix_not_name():
    frame = uni.classify(
        _listing(
            [
                {"Code": "005930", "Name": "삼성전자"},
                {"Code": "005935", "Name": "삼성전자우"},
                {"Code": "00680K", "Name": "미래에셋증권2우B"},
                # 이름이 우로 끝나지만 끝자리 0 인 보통주는 우선주가 아니다
                {"Code": "123450", "Name": "한국가우"},
                # 영숫자 코드의 보통주
                {"Code": "0126Z0", "Name": "삼성에피스홀딩스"},
            ]
        ),
        AS_OF,
    )
    assert "is_preferred" not in _kinds(frame, "005930")
    assert "is_preferred" in _kinds(frame, "005935")
    assert "is_preferred" in _kinds(frame, "00680K")
    assert "is_preferred" not in _kinds(frame, "123450")
    assert "is_preferred" not in _kinds(frame, "0126Z0")


def test_dept_kinds():
    frame = uni.classify(
        _listing(
            [
                {"Code": "000010", "Name": "A", "Dept": "SPAC(소속부없음)"},
                {"Code": "000020", "Name": "B", "Dept": "관리종목(소속부없음)"},
                {"Code": "000030", "Name": "C", "Dept": "투자주의환기종목(소속부없음)"},
                {"Code": "000040", "Name": "D", "Dept": "외국기업(소속부없음)"},
                {"Code": "000050", "Name": "대신밸류리츠"},
            ]
        ),
        AS_OF,
    )
    assert _kinds(frame, "000010") == {"is_spac"}
    assert _kinds(frame, "000020") == {"is_managed"}
    assert _kinds(frame, "000030") == {"is_caution"}
    assert _kinds(frame, "000040") == {"is_foreign"}
    assert _kinds(frame, "000050") == {"is_reit"}


def test_zero_volume_is_flagged_but_missing_volume_too():
    frame = uni.classify(
        _listing(
            [
                {"Code": "000010", "Name": "A", "Volume": 0},
                {"Code": "000020", "Name": "B", "Volume": None},
                {"Code": "000030", "Name": "C", "Volume": 5},
            ]
        ),
        AS_OF,
    )
    assert "is_zero_volume" in _kinds(frame, "000010")
    assert "is_zero_volume" in _kinds(frame, "000020")
    assert "is_zero_volume" not in _kinds(frame, "000030")


def test_new_listing_boundary_uses_weekdays():
    # 2026-11-06(금) 기준 평일 60일 전은 2026-08-14(금). 그날 상장이면 정확히 60 → 신규 아님.
    frame = uni.classify(
        _listing(
            [
                {"Code": "000010", "Name": "A", "ListingDate": "2026-08-14"},
                {"Code": "000020", "Name": "B", "ListingDate": "2026-08-17"},
                {"Code": "000030", "Name": "C", "ListingDate": "2026-11-05"},
            ]
        ),
        AS_OF,
    )
    assert "is_new_listing" not in _kinds(frame, "000010")
    assert "is_new_listing" in _kinds(frame, "000020")
    assert "is_new_listing" in _kinds(frame, "000030")


def test_missing_listing_date_is_counted_separately_not_as_new():
    frame = uni.classify(_listing([{"Code": "000010", "Name": "A", "ListingDate": None}]), AS_OF)
    assert _kinds(frame, "000010") == {"listing_date_missing"}


def test_remaining_after_counts_overlaps_once():
    frame = uni.classify(
        _listing(
            [
                {"Code": "000010", "Name": "A"},
                # 관리종목이면서 거래량 0: 두 종류에 속하지만 한 번만 제외된다
                {"Code": "000020", "Name": "B", "Dept": "관리종목(소속부없음)", "Volume": 0},
                {"Code": "000035", "Name": "C우"},
            ]
        ),
        AS_OF,
    )
    assert uni.remaining_after(frame, []) == 3
    assert uni.remaining_after(frame, ["is_managed", "is_zero_volume"]) == 2
    assert uni.remaining_after(frame, ["is_managed", "is_zero_volume", "is_preferred"]) == 1


def test_remaining_after_rejects_unknown_kind():
    frame = uni.classify(_listing([{"Code": "000010", "Name": "A"}]), AS_OF)
    with pytest.raises(ValueError, match="알 수 없는 종류"):
        uni.remaining_after(frame, ["is_typo"])


def test_count_kinds_lists_every_kind():
    frame = uni.classify(_listing([{"Code": "000010", "Name": "A"}]), AS_OF)
    assert [item.key for item in uni.count_kinds(frame)] == list(uni.KIND_LABELS)
