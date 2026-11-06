"""기준일(거래일) 결정.

``exchange_calendars`` 의 KRX 캘린더는 pandas 3 과 충돌한 이력이 있어 쓰지 않고, 삼성전자 시세에
실제로 있는 날짜를 거래일로 본다.
"""

from __future__ import annotations

from datetime import date, timedelta

from krx_screener.prices import Fetcher, PriceFetchError, normalize

REFERENCE_CODE = "005930"  # 삼성전자 — 거래일 판정용 기준 종목


class AsOfError(ValueError):
    """기준일을 정하지 못했을 때. 메시지는 사용자에게 그대로 보여준다."""


def resolve_as_of(today: date, requested: date | None, fetcher: Fetcher) -> date:
    """기준일을 돌려준다. 지정하지 않으면 오늘 이전의 마지막 거래일."""
    if requested is not None and requested > today:
        raise AsOfError(f"기준일 {requested} 은 오늘({today}) 이후라 시세가 없습니다.")

    anchor = requested or today
    try:
        frame = normalize(fetcher(REFERENCE_CODE, anchor - timedelta(days=14), anchor))
    except PriceFetchError as exc:
        raise AsOfError(f"거래일을 확인하지 못했습니다 (삼성전자 시세 조회 실패: {exc})") from exc

    days = sorted({ts.date() for ts in frame.index})
    if requested is not None:
        if requested in days:
            return requested
        earlier = [d for d in days if d < requested]
        hint = f" 가까운 이전 거래일: {earlier[-1]}" if earlier else ""
        raise AsOfError(f"기준일 {requested} 은 거래일이 아니거나 시세가 아직 없습니다.{hint}")

    prior = [d for d in days if d < today]
    if not prior:
        raise AsOfError("직전 영업일을 찾지 못했습니다. 기준일을 --date 로 지정해 주세요.")
    return prior[-1]
