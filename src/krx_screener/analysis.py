"""3단계: 후보를 TradingAgents-KR 로 보내 전체 분석을 돌리고 최종 등급을 읽는다.

TradingAgents-KR 는 별도 프로세스(로컬 서버)이고 HTTP API 로만 연결한다. 그 코드에 의존하지 않는다.

- ``GET  /api/health``        서버 확인
- ``POST /api/runs``          분석 시작 → 201 + ``run_id`` (종목코드는 ``^\\d{6}$`` 만 허용)
- ``GET  /api/runs/{run_id}`` 상태 스냅샷 (``pending``/``running``/``done``/``error``, ``decision``)

주의:
- 종목 하나의 분석이 몇 분 걸리고 Max 플랜 쿼터를 쓴다. 호출하는 쪽(CLI)이 개수를 제한하고 확인한다.
- 서버에는 취소 API 가 없다. 시간 초과로 기다리기를 멈춰도 서버에서는 계속 돌 수 있다.
- ``run_id`` 상태는 서버 메모리에만 있다. 서버가 재시작되면 404 가 된다.
- 영숫자 종목코드(예: ``0126Z0``)는 서버가 거절하므로 보내지 않고 "건너뜀" 으로 기록한다.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# 서버가 돌려주는 등급은 영어 내부값이고, 한국어 표기도 읽는다 (등급 표기 이중성).
RATING_LABELS = {
    "Buy": "매수",
    "Overweight": "비중 확대",
    "Hold": "보유",
    "Underweight": "비중 축소",
    "Sell": "매도",
}
_KOREAN_TO_RATING = {label: rating for rating, label in RATING_LABELS.items()}
TARGET_RATINGS = frozenset({"Buy", "Overweight"})  # 우리가 모아 보여줄 등급
REVIEW = "REVIEW"  # 서버가 등급을 읽지 못한 경우 (사람이 확인)

DONE, FAILED, TIMEOUT, SKIPPED = "done", "error", "timeout", "skipped"

_SERVER_CODE_RE = re.compile(r"^\d{6}$")


class AnalysisError(RuntimeError):
    """분석 서버와 통신하지 못했거나 서버가 요청을 거절했을 때. 메시지는 그대로 보여준다."""


def normalize_rating(raw: str | None) -> str | None:
    """영어·한국어 표기를 내부 등급 값으로. 알 수 없으면 None, ``REVIEW`` 는 그대로."""
    if not raw:
        return None
    text = raw.strip()
    if text == REVIEW:
        return REVIEW
    for rating in RATING_LABELS:
        if text.lower() == rating.lower():
            return rating
    return _KOREAN_TO_RATING.get(text)


def rating_label(rating: str | None) -> str:
    if rating is None:
        return ""
    return RATING_LABELS.get(rating, rating)


def server_accepts(code: str) -> bool:
    """서버가 받아 주는 종목코드인가 (숫자 6자리만)."""
    return bool(_SERVER_CODE_RE.fullmatch(code))


@dataclass(frozen=True)
class AnalysisOutcome:
    code: str
    name: str
    status: str  # done | error | timeout | skipped
    decision: str | None = None  # 내부 등급 값 (Buy 등) 또는 REVIEW
    run_id: str = ""
    elapsed_seconds: float = 0.0
    message: str = ""

    @property
    def is_target(self) -> bool:
        return self.status == DONE and self.decision in TARGET_RATINGS


def skipped_outcome(code: str, name: str) -> AnalysisOutcome:
    """서버가 거절할 종목코드라 보내지 않은 종목의 결과."""
    return AnalysisOutcome(
        code, name, SKIPPED, message="서버가 숫자 6자리 종목코드만 받아 보내지 않았습니다."
    )


class AnalysisClient:
    """TradingAgents-KR API 의 얇은 HTTP 클라이언트."""

    def __init__(self, base_url: str, request_timeout: float = 30.0, session: Any = None) -> None:
        import requests

        self._base = base_url.rstrip("/")
        self._timeout = request_timeout
        self._session = session or requests.Session()
        self._requests = requests

    def _call(self, method: str, path: str, **kwargs: Any):
        try:
            return self._session.request(method, self._base + path, timeout=self._timeout, **kwargs)
        except self._requests.RequestException as exc:
            raise AnalysisError(
                f"TradingAgents-KR 서버에 연결하지 못했습니다 ({self._base}): {type(exc).__name__}"
            ) from None

    def check_health(self) -> None:
        resp = self._call("GET", "/api/health")
        if resp.status_code != 200:
            raise AnalysisError(f"TradingAgents-KR 서버 상태 확인 실패 (HTTP {resp.status_code})")

    def start(self, ticker: str, trade_date: str, effort: str) -> str:
        resp = self._call(
            "POST",
            "/api/runs",
            json={"ticker": ticker, "trade_date": trade_date, "effort": effort},
        )
        if resp.status_code != 201:
            raise AnalysisError(f"분석 시작을 거절당했습니다 (HTTP {resp.status_code}): {_detail(resp)}")
        run_id = _json(resp).get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise AnalysisError("분석 시작 응답에 run_id 가 없습니다.")
        return run_id

    def snapshot(self, run_id: str) -> dict[str, Any]:
        resp = self._call("GET", f"/api/runs/{run_id}")
        if resp.status_code == 404:
            raise AnalysisError("서버가 이 분석을 찾지 못합니다 (서버가 재시작됐을 수 있음).")
        if resp.status_code != 200:
            raise AnalysisError(f"분석 상태 조회 실패 (HTTP {resp.status_code})")
        return _json(resp)


def _json(resp: Any) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        raise AnalysisError("서버 응답이 JSON 이 아닙니다.") from None
    if not isinstance(body, dict):
        raise AnalysisError("서버 응답 구조가 예상과 다릅니다.")
    return body


def _detail(resp: Any) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    return str(body.get("detail", ""))[:200] if isinstance(body, dict) else ""


def run_one(
    client: AnalysisClient,
    code: str,
    name: str,
    trade_date: str,
    effort: str,
    timeout_seconds: float,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> AnalysisOutcome:
    """종목 하나를 분석하고 끝날 때까지 기다린다. 실패는 예외 대신 결과로 돌려준다."""
    if not server_accepts(code):
        return skipped_outcome(code, name)

    started = clock()
    try:
        run_id = client.start(code, trade_date, effort)
    except AnalysisError as exc:
        return AnalysisOutcome(code, name, FAILED, elapsed_seconds=clock() - started, message=str(exc))

    while True:
        try:
            snap = client.snapshot(run_id)
        except AnalysisError as exc:
            return AnalysisOutcome(
                code, name, FAILED, run_id=run_id, elapsed_seconds=clock() - started, message=str(exc)
            )
        elapsed = clock() - started

        status = snap.get("status")
        if status == "done":
            decision = normalize_rating(snap.get("decision"))
            message = "" if decision else f"등급을 읽지 못했습니다 (서버 값: {snap.get('decision')!r})"
            return AnalysisOutcome(
                code, name, DONE, decision=decision or REVIEW, run_id=run_id,
                elapsed_seconds=elapsed, message=message,
            )
        if status == "error":
            return AnalysisOutcome(
                code, name, FAILED, run_id=run_id, elapsed_seconds=elapsed,
                message=str(snap.get("error") or "서버가 오류로 끝냈습니다.")[:300],
            )
        if elapsed >= timeout_seconds:
            return AnalysisOutcome(
                code, name, TIMEOUT, run_id=run_id, elapsed_seconds=elapsed,
                message=f"{int(timeout_seconds)}초 안에 끝나지 않아 기다리기를 멈췄습니다 (서버에서는 계속 실행 중일 수 있음).",
            )
        sleep(poll_interval)
