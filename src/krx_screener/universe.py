"""전종목 목록을 받아 종류별로 분류한다.

유니버스(스크리닝 대상)의 제외 규칙을 정하기 위한 재료를 만드는 모듈이다. 여기서는 종류를
**판별해 표시만** 하고, 무엇을 제외할지는 정하지 않는다 (제외 규칙은 개수를 확인한 뒤 확정).

데이터 소스는 FinanceDataReader 다 (keyless).
- ``StockListing('KRX')``: 종목코드·종목명·시장·소속부·당일 시세(거래량·거래대금)
- ``StockListing('KRX-DESC')``: 상장일

종목코드는 숫자 6자리가 아닐 수 있다. 신규 종목은 ``0126Z0`` 처럼 영숫자 코드를 받고, 우선주는
끝자리가 ``0`` 이 아니다 (예: ``005935``, ``00680K``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

# KONEX 는 제외한다. FDR 은 코스닥 우량기업부를 ``KOSDAQ GLOBAL`` 로 따로 내려주지만 같은
# 코스닥 시장이므로 합친다.
MARKET_ALIASES: dict[str, str] = {
    "KOSPI": "KOSPI",
    "KOSDAQ": "KOSDAQ",
    "KOSDAQ GLOBAL": "KOSDAQ",
}

# 신규상장 판정 기준 (거래일 수). 여기서는 공휴일을 반영하지 않는 평일 수로 근사한다.
NEW_LISTING_TRADING_DAYS = 60

# 소속부(Dept) 값으로 판별하는 종류. FDR 이 내려주는 문자열 그대로다.
_DEPT_SPAC = "SPAC(소속부없음)"
_DEPT_MANAGED = "관리종목(소속부없음)"
_DEPT_CAUTION = "투자주의환기종목(소속부없음)"
_DEPT_FOREIGN = "외국기업(소속부없음)"

# 종류 플래그 컬럼과 화면 표기. 한 종목이 여러 종류에 동시에 속할 수 있다.
KIND_LABELS: dict[str, str] = {
    "is_preferred": "우선주",
    "is_spac": "스팩",
    "is_managed": "관리종목",
    "is_caution": "투자주의환기종목",
    "is_foreign": "외국기업",
    "is_reit": "리츠",
    "is_zero_volume": "당일 거래량 0",
    "is_new_listing": f"신규상장 {NEW_LISTING_TRADING_DAYS}거래일 미만(근사)",
    "listing_date_missing": "상장일 미확인",
}


class UniverseFetchError(RuntimeError):
    """종목 목록을 받지 못했을 때."""


def fetch_listing() -> pd.DataFrame:
    """코스피·코스닥 전종목을 한 표로 받는다 (KONEX 제외).

    반환 컬럼: ``Code``, ``Name``, ``Market``(KOSPI/KOSDAQ), ``Dept``, ``Volume``, ``Amount``,
    ``ListingDate``(없으면 NaT).
    """
    import FinanceDataReader as fdr

    try:
        listing = fdr.StockListing("KRX")
        desc = fdr.StockListing("KRX-DESC")
    except Exception as exc:  # noqa: BLE001 — 네트워크·외부 패키지 오류를 한 가지로 안내
        raise UniverseFetchError(f"종목 목록을 받지 못했습니다 ({type(exc).__name__}: {exc})") from exc

    if listing is None or listing.empty:
        raise UniverseFetchError("종목 목록이 비어 있습니다.")
    required = {"Code", "Name", "Market", "Dept", "Volume", "Amount"}
    if missing := required - set(listing.columns):
        raise UniverseFetchError(f"종목 목록에 필요한 컬럼이 없습니다: {sorted(missing)}")

    frame = listing[list(required)].copy()
    frame["Market"] = frame["Market"].map(MARKET_ALIASES)
    frame = frame.dropna(subset=["Market"]).reset_index(drop=True)

    # 상장일은 없어도 목록 자체는 쓸 수 있다. 못 받으면 "상장일 미확인" 으로 센다.
    if desc is not None and {"Code", "ListingDate"} <= set(desc.columns):
        dates = desc[["Code", "ListingDate"]].drop_duplicates("Code")
        frame = frame.merge(dates, on="Code", how="left")
        frame["ListingDate"] = pd.to_datetime(frame["ListingDate"], errors="coerce")
    else:
        frame["ListingDate"] = pd.NaT
    return frame


def classify(listing: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """종류 플래그(bool) 컬럼을 덧붙인 사본을 돌려준다. 제외 여부는 정하지 않는다."""
    out = listing.copy()
    code = out["Code"].astype(str)
    name = out["Name"].astype(str)
    dept = out["Dept"].fillna("")

    # 우선주: 보통주는 끝자리가 0 이고 우선주는 5·7·9·K·L 등이다. 이름의 "우" 로 찾으면
    # 이름이 우로 끝나는 보통주를 잘못 잡는다.
    out["is_preferred"] = ~code.str.endswith("0")
    out["is_spac"] = dept.eq(_DEPT_SPAC)
    out["is_managed"] = dept.eq(_DEPT_MANAGED)
    out["is_caution"] = dept.eq(_DEPT_CAUTION)
    out["is_foreign"] = dept.eq(_DEPT_FOREIGN)
    out["is_reit"] = name.str.contains("리츠", regex=False)
    # 거래량 0 은 거래정지일 수도, 그날 체결이 없었던 것일 수도 있다. 거래정지 여부를 확정하는
    # 값이 아니라 "의심" 표시다.
    out["is_zero_volume"] = out["Volume"].fillna(0).eq(0)

    listed = out["ListingDate"]
    out["listing_date_missing"] = listed.isna()
    elapsed = np.full(len(out), np.nan)
    known = listed.notna().to_numpy()
    if known.any():
        elapsed[known] = np.busday_count(
            listed[known].to_numpy().astype("datetime64[D]"), np.datetime64(as_of)
        )
    out["is_new_listing"] = pd.Series(elapsed, index=out.index).lt(NEW_LISTING_TRADING_DAYS)
    return out


@dataclass(frozen=True)
class KindCount:
    key: str
    label: str
    count: int


def count_kinds(classified: pd.DataFrame) -> list[KindCount]:
    """종류별 개수. 한 종목이 여러 종류에 속하면 각각에 센다 (합이 전체보다 클 수 있음)."""
    return [
        KindCount(key, label, int(classified[key].sum())) for key, label in KIND_LABELS.items()
    ]


def count_by_market(classified: pd.DataFrame) -> dict[str, int]:
    return {str(k): int(v) for k, v in classified["Market"].value_counts().items()}


def remaining_after(classified: pd.DataFrame, exclude_keys: list[str]) -> int:
    """주어진 종류 중 하나라도 해당하는 종목을 뺀 뒤 남는 종목 수 (겹침 반영)."""
    unknown = set(exclude_keys) - set(KIND_LABELS)
    if unknown:
        raise ValueError(f"알 수 없는 종류: {sorted(unknown)}")
    if not exclude_keys:
        return len(classified)
    excluded = classified[exclude_keys].any(axis=1)
    return int((~excluded).sum())
