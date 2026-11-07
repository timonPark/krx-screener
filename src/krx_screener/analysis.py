"""3단계: 후보를 TradingAgents-KR 로 보내 전체 분석을 돌리고 최종 등급을 읽는다.

TradingAgents-KR 는 별도 프로세스(로컬 서버)이고 HTTP API 로만 연결한다. 그 코드에 의존하지 않는다.

- ``GET  /api/health``        서버 확인
- ``POST /api/runs``          분석 시작 → 201 + ``run_id`` (종목코드는 ``^\\d{6}$`` 만 허용)
- ``GET  /api/runs/{run_id}`` 상태 스냅샷 (``pending``/``running``/``done``/``error``, ``decision``)
- ``GET  /api/runs?limit=N``  최근 분석 목록 (종목·기준일·상태·생성 시각). 같은 종목·기준일의 분석이 아직 도는지 볼 때 쓴다
- ``GET  /api/runs/{run_id}/events`` 진행 이벤트 (SSE). 보고서 번호는 여기의 ``report_saved`` 이벤트에만 있다

주의:
- 종목 하나의 분석이 몇 분 걸리고 Max 플랜 쿼터를 쓴다. 호출하는 쪽(CLI)이 개수를 제한하고 확인한다.
- 서버에는 취소 API 가 없다. 시간 초과로 기다리기를 멈춰도 서버에서는 계속 돌 수 있다.
  같은 종목을 다시 보내면 서버는 거절하지 않고 새 분석을 하나 더 시작해서 쿼터를 한 번 더 쓴다 (2026-11-07 실제 서버로 확인, #70).
  그래서 보내기 전에 같은 종목·기준일의 분석이 아직 도는지 보고, 돌고 있으면 새로 보내지 않고 그 분석을 이어서 기다린다 (``run_one``).
- ``run_id`` 상태는 서버 메모리에만 있다. 서버가 재시작되면 404 가 된다.
- 보고서 번호(``report_id``, 예: ``028050_20261106_172435``)는 상태 스냅샷에 없다. 서버는 보고서를 저장한 직후 ``report_saved``
  이벤트를 내고 그 다음에 ``done`` 을 내므로, 분석이 끝난 뒤 이벤트를 다시 읽으면(서버가 처음부터 되풀이해 준다) 항상 있다.
  보고서 목록(``GET /api/reports``)에서 종목코드로 찾는 방법은 쓰지 않는다: 같은 종목의 보고서가 여러 개일 수 있고 보고서 번호의
  시각은 저장한 시각이라 어느 것이 이번 분석인지 가릴 수 없다.
- 영숫자 종목코드(예: ``0126Z0``)는 서버가 거절하므로 보내지 않고 "건너뜀" 으로 기록한다.
"""

from __future__ import annotations

import json
import logging
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

RUN_LIST_LIMIT = 20  # 서버가 기본으로 주는 개수. 도는 분석은 늘 최근이라 이만큼이면 된다 (응답에 보고서 본문이 들어 있어 무겁다)
RUNNING_STATES = frozenset({"pending", "running"})  # 서버가 아직 끝내지 않은 분석

_SERVER_CODE_RE = re.compile(r"^\d{6}$")
_REPORT_ID_RE = re.compile(r"^\d{6}_\d{8}_\d{6}$")  # 서버의 보고서 폴더 이름: 종목코드_저장일_저장시각

logger = logging.getLogger(__name__)


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
    report_id: str = ""  # 서버가 저장한 보고서 번호. 읽지 못했으면 빈 문자열 (분석 결과는 그대로 유효하다)

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

    def find_running(self, ticker: str, trade_date: str) -> str | None:
        """서버에 같은 종목·기준일의 아직 도는 분석(``pending``/``running``)이 있으면 그 ``run_id``, 없으면 None.

        여럿이면 가장 먼저 시작한 것(끝에 가장 가깝다). 목록을 읽지 못하면(목록 주소가 없는 서버 버전, 오류 응답, 연결 실패 등)
        확인할 수 없는 것이므로 None 으로 돌려주고 경고만 남긴다: 이 확인 때문에 분석 자체가 막히지는 않는다.
        """
        try:
            resp = self._call("GET", f"/api/runs?limit={RUN_LIST_LIMIT}")
            if resp.status_code != 200:
                raise AnalysisError(f"분석 목록 조회 실패 (HTTP {resp.status_code})")
            runs = resp.json()
        except (AnalysisError, ValueError) as exc:
            logger.warning("서버의 분석 목록을 읽지 못해 이미 도는 분석이 있는지 확인하지 못했습니다 (%s): %s", ticker, exc)
            return None
        if not isinstance(runs, list):
            logger.warning("서버의 분석 목록 모양이 예상과 다릅니다 (%s)", ticker)
            return None
        running = [
            run
            for run in runs
            if isinstance(run, dict)
            and run.get("ticker") == ticker
            and run.get("trade_date") == trade_date
            and run.get("status") in RUNNING_STATES
            and isinstance(run.get("run_id"), str)
            and run["run_id"]
        ]
        if not running:
            return None
        return min(running, key=lambda run: str(run.get("created_at", "")))["run_id"]

    def snapshot(self, run_id: str) -> dict[str, Any]:
        resp = self._call("GET", f"/api/runs/{run_id}")
        if resp.status_code == 404:
            raise AnalysisError("서버가 이 분석을 찾지 못합니다 (서버가 재시작됐을 수 있음).")
        if resp.status_code != 200:
            raise AnalysisError(f"분석 상태 조회 실패 (HTTP {resp.status_code})")
        return _json(resp)

    def report_id(self, run_id: str) -> str | None:
        """끝난 분석의 보고서 번호. 서버가 보고서를 저장하지 않았거나 모양이 이상하면 None.

        진행 이벤트(SSE)를 읽는다. 분석이 끝난 뒤에는 서버가 이벤트를 처음부터 되풀이하고 ``done`` 에서 끝내므로 오래 걸리지 않는다.
        """
        resp = self._call("GET", f"/api/runs/{run_id}/events", stream=True)
        try:
            if resp.status_code != 200:
                raise AnalysisError(f"보고서 번호 조회 실패 (HTTP {resp.status_code})")
            for raw in resp.iter_lines():
                # text/event-stream 은 charset 이 없으면 requests 가 ISO-8859-1 로 읽어 한글이 깨진다. bytes 로 받아 UTF-8 로 직접 읽는다
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[len("data:"):])
                except ValueError:
                    continue  # 깨진 줄은 건너뛴다
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "report_saved":
                    found = event.get("report_id")
                    return found if isinstance(found, str) and _REPORT_ID_RE.fullmatch(found) else None
                if event.get("type") in {"done", "error"}:
                    return None  # 보고서를 저장하지 않고 끝났다
            return None
        except self._requests.RequestException as exc:
            raise AnalysisError(f"보고서 번호를 읽지 못했습니다: {type(exc).__name__}") from None
        finally:
            resp.close()


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
    # 서버에서 같은 종목·기준일의 분석이 아직 돌고 있으면(앞서 시간 초과로 기다리기를 멈춘 분석 등) 새로 보내지 않고 이어서 기다린다.
    # 새로 보내면 서버가 거절하지 않고 분석을 하나 더 시작해서 쿼터를 한 번 더 쓰고, 먼저 끝난 분석의 결과는 버려진다 (#70).
    run_id = client.find_running(code, trade_date)
    if run_id:
        logger.info("서버에서 이미 도는 분석을 이어서 기다립니다 (%s, %s)", code, trade_date)
    else:
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
                elapsed_seconds=elapsed, message=message, report_id=_read_report_id(client, run_id, code),
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


def _read_report_id(client: AnalysisClient, run_id: str, code: str) -> str:
    """보고서 번호를 읽는다. 못 읽어도 분석 결과(등급)는 유효하므로 빈 문자열로 두고 실패로 만들지 않는다."""
    try:
        return client.report_id(run_id) or ""
    except AnalysisError as exc:
        logger.warning("보고서 번호를 읽지 못했습니다 (%s): %s", code, exc)
        return ""
