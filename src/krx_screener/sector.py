"""업종 코드·업종명·기업 설명 조회 (이슈 #16).

네이버 종목 상세(비공식 주소, 로그인 불필요, 영숫자 종목코드도 조회됨)를 쓴다.

    GET https://stock.naver.com/api/domestic/detail/{종목코드}/detail?codeType=KRX
      upjongCode  업종 코드 (예: 삼성전자 "27")
      upJongName  업종명   (예: "반도체와반도체장비")
      comment1~3  기업 설명 세 줄

이 값은 투자 테마 이름(예: HBM)이 아니라 **업종**이다. 읽지 못하면 ``None`` 을 돌려주고 오류로 멈추지
않는다 (그 종목의 업종은 빈 칸이 된다).
"""

from __future__ import annotations

import re
import time
from typing import Any

from krx_screener.externals import SectorInfo

DETAIL_URL = "https://stock.naver.com/api/domestic/detail/{code}/detail?codeType=KRX"
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://stock.naver.com/",
}
_TIMEOUT = 10
_CODE_RE = re.compile(r"^[0-9A-Z]{6}$")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_detail(payload: Any) -> SectorInfo | None:
    """상세 응답에서 업종 정보를 뽑는다. 업종 코드·업종명·설명이 모두 비면 None (읽을 게 없다)."""
    if not isinstance(payload, dict):
        return None
    code = _text(payload.get("upjongCode"))
    name = _text(payload.get("upJongName"))
    description = " ".join(t for t in (_text(payload.get(f"comment{n}")) for n in (1, 2, 3)) if t)
    if not (code or name or description):
        return None
    return SectorInfo(code=code, name=name, description=description)


def naver_sector(code: str, *, retries: int = 1, retry_wait: float = 0.5) -> SectorInfo | None:
    """종목의 업종 정보. 읽지 못하면 None.

    네트워크 오류는 ``retries`` 번 다시 시도하고, HTTP 오류나 JSON 이 아닌 응답은 다시 시도하지 않는다
    (같은 응답이 또 올 뿐이다).
    """
    import requests

    if not _CODE_RE.fullmatch(code):
        return None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(DETAIL_URL.format(code=code), headers=_HEADERS, timeout=_TIMEOUT)
        except requests.RequestException:
            if attempt < retries:
                time.sleep(retry_wait)
            continue
        if resp.status_code != 200:
            return None
        try:
            return parse_detail(resp.json())
        except ValueError:
            return None
    return None
