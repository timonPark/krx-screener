"""1.5단계: 1단계를 통과한 후보에만 투자자별 수급을 조회해 표시한다.

데이터 소스는 네이버 증권 ``GET https://m.stock.naver.com/api/stock/{종목코드}/trend`` 다
(JSON, 비공식 경로, 로그인 불필요, 영숫자 종목코드도 조회됨). 최근 10거래일의 외국인·기관·개인
순매수 **수량(주)** 을 준다. 금액이 아니고 기관 세부 구분도 없다. KRX 로그인은 쓰지 않는다.

원칙: 수급을 읽지 못한 종목을 "수급 0" 으로 읽거나 버리지 않는다. 응답이 비었거나 오류거나 기준일을
포함하지 않으면 항상 ``미확인`` 으로 표시하고 사유를 센다. (없는 종목코드는 오류가 아니라 HTTP 200
에 빈 목록을 돌려준다.)
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

TREND_URL = "https://m.stock.naver.com/api/stock/{code}/trend"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "ko-KR,ko;q=0.9"}
_TIMEOUT = 10
MAX_WINDOW_DAYS = 10  # 네이버가 주는 이력 길이
_CODE_RE = re.compile(r"^[0-9A-Z]{6}$")

# 내부 코드값은 영어, 화면·CSV 표기는 한국어 (i18n 경계).
STRONG, PARTIAL, WEAK, UNCONFIRMED = "strong", "partial", "weak", "unconfirmed"
STATUS_LABELS = {STRONG: "양호", PARTIAL: "부분", WEAK: "약함", UNCONFIRMED: "미확인"}

# 수급 미확인 사유 (내부 코드값)
FETCH_FAILED = "fetch_failed"
NO_DATA = "no_data"
NO_AS_OF_ROW = "no_as_of_row"
SHORT_HISTORY = "short_history"
UNREADABLE = "unreadable"

REASON_LABELS = {
    FETCH_FAILED: "조회 실패",
    NO_DATA: "데이터 없음",
    NO_AS_OF_ROW: "기준일 데이터 없음",
    SHORT_HISTORY: "이력 부족",
    UNREADABLE: "값 읽기 실패",
}

TrendFetcher = Callable[[str], list[dict[str, Any]]]


class FlowFetchError(RuntimeError):
    """수급을 받지 못했을 때. 메시지는 사용자에게 그대로 보여준다."""


def naver_trend(code: str) -> list[dict[str, Any]]:
    """네이버 증권에서 최근 10거래일 투자자별 순매수 행을 받는다."""
    import requests

    if not _CODE_RE.fullmatch(code):
        raise FlowFetchError(f"종목코드 형식이 올바르지 않습니다: {code!r}")
    try:
        resp = requests.get(TREND_URL.format(code=code), headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise FlowFetchError(f"요청 실패 ({type(exc).__name__})") from None
    if resp.status_code != 200:
        raise FlowFetchError(f"HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        raise FlowFetchError("응답이 JSON 이 아닙니다") from None
    if not isinstance(payload, list):
        raise FlowFetchError("응답 구조가 예상과 다릅니다")
    return [row for row in payload if isinstance(row, dict)]


def _to_int(text: Any) -> int | None:
    """``'+9,822'`` / ``'-21,196'`` / ``'0'`` → int. 해석 불가면 None."""
    try:
        return int(str(text).replace(",", "").replace("+", "").strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class FlowReading:
    status: str
    foreign: int | None = None
    institution: int | None = None
    reason_code: str = ""
    reason: str = ""


def classify_status(foreign: int, institution: int) -> str:
    """외국인·기관 순매수 합계의 부호로 상태를 정한다 (수량 합계의 부호만 사용)."""
    positives = (foreign > 0) + (institution > 0)
    return {2: STRONG, 1: PARTIAL, 0: WEAK}[positives]


def _unconfirmed(code: str, reason: str) -> FlowReading:
    return FlowReading(UNCONFIRMED, reason_code=code, reason=reason)


def read_flow(rows: list[dict[str, Any]], as_of: date, window: int) -> FlowReading:
    """기준일로 끝나는 최근 ``window`` 거래일의 외국인·기관 순매수 합계를 읽는다.

    기준일 봉이 없거나, 이력이 ``window`` 일보다 짧거나, 값을 읽지 못하면 ``미확인`` 이다.
    기준일 이후 행은 쓰지 않는다 (룩어헤드 방지).
    """
    cutoff = as_of.strftime("%Y%m%d")
    usable = sorted(
        (r for r in rows if str(r.get("bizdate", "")) and str(r["bizdate"]) <= cutoff),
        key=lambda r: str(r["bizdate"]),
        reverse=True,
    )
    if not usable:
        return _unconfirmed(
            NO_DATA, f"기준일 이전 수급 데이터 없음 (최근 {MAX_WINDOW_DAYS}거래일만 제공)"
        )
    if str(usable[0]["bizdate"]) != cutoff:
        return _unconfirmed(NO_AS_OF_ROW, "기준일 수급 데이터 없음")
    taken = usable[:window]
    if len(taken) < window:
        return _unconfirmed(SHORT_HISTORY, f"수급 이력 부족 ({len(taken)}일 < {window}일)")

    foreign = [_to_int(r.get("foreignerPureBuyQuant")) for r in taken]
    institution = [_to_int(r.get("organPureBuyQuant")) for r in taken]
    if None in foreign or None in institution:
        return _unconfirmed(UNREADABLE, "수급 값을 읽지 못함")
    total_f, total_i = sum(foreign), sum(institution)  # type: ignore[arg-type]
    return FlowReading(classify_status(total_f, total_i), total_f, total_i)


def fetch_flow(
    code: str,
    as_of: date,
    window: int,
    fetcher: TrendFetcher,
    retries: int = 1,
    retry_wait: float = 0.5,
) -> FlowReading:
    last: FlowFetchError | None = None
    for attempt in range(retries + 1):
        try:
            return read_flow(fetcher(code), as_of, window)
        except FlowFetchError as exc:
            last = exc
            if attempt < retries:
                time.sleep(retry_wait)
    return _unconfirmed(FETCH_FAILED, f"수급 조회 실패: {last}")


@dataclass
class FlowStats:
    mode: str
    window_days: int
    stage1_candidates: int
    status_counts: dict[str, int]
    unconfirmed_by_reason: dict[str, int]
    unconfirmed: list[dict[str, str]] = field(default_factory=list)
    dropped_by_filter: int = 0


def apply_flow(
    candidates: pd.DataFrame,
    mode: str,
    window: int,
    as_of: date,
    fetcher: TrendFetcher,
    workers: int = 8,
    progress: Callable[[int, int], None] | None = None,
    retry_wait: float = 0.5,
) -> tuple[pd.DataFrame, FlowStats]:
    """후보에 수급 컬럼 3개를 붙인다. ``mode == "filter"`` 면 ``약함`` 만 탈락시킨다."""
    foreign_col, institution_col = f"foreign_net_{window}d", f"institution_net_{window}d"
    stats = FlowStats(
        mode=mode,
        window_days=window,
        stage1_candidates=len(candidates),
        status_counts=dict.fromkeys(STATUS_LABELS, 0),
        unconfirmed_by_reason={},
    )

    codes = candidates["code"].tolist() if not candidates.empty else []
    readings: list[FlowReading] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = pool.map(
            lambda c: fetch_flow(c, as_of, window, fetcher, retry_wait=retry_wait), codes
        )
        for done, reading in enumerate(futures, start=1):
            readings.append(reading)
            if progress:
                progress(done, len(codes))

    out = candidates.copy()
    out[foreign_col] = pd.array([r.foreign for r in readings], dtype="Int64")
    out[institution_col] = pd.array([r.institution for r in readings], dtype="Int64")
    out["flow_status"] = [STATUS_LABELS[r.status] for r in readings]

    for code, reading in zip(codes, readings, strict=True):
        stats.status_counts[reading.status] += 1
        if reading.status == UNCONFIRMED:
            stats.unconfirmed_by_reason[reading.reason_code] = (
                stats.unconfirmed_by_reason.get(reading.reason_code, 0) + 1
            )
            name = out.loc[out["code"] == code, "name"].iloc[0]
            stats.unconfirmed.append({"code": code, "name": str(name), "reason": reading.reason})

    if mode == "filter":
        weak = out["flow_status"] == STATUS_LABELS[WEAK]
        stats.dropped_by_filter = int(weak.sum())
        out = out[~weak].reset_index(drop=True)
    return out, stats
