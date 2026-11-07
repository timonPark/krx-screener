"""브라우저 테스트용 서버 (#58): 실제 백엔드(FastAPI + 테스트 PostgreSQL + 실제 ``frontend/``)에 외부 경계만 가짜로 끼운다.

테스트가 서버 안의 가짜를 실행 중에 조작할 수 있게, 테스트 전용 주소 ``POST /__ctl`` 을 더한다 (제품 코드에는 없다).

    {"errors": ["030530"], "polls": 2, "health_error": "…", "db_down": true, "build_gate": 3}

- ``errors``: 이 종목의 분석은 오류로 끝난다. ``polls``: 분석이 끝나기 전에 처리 중으로 답하는 횟수.
- ``health_error``: 분석 서버가 꺼진 것처럼 한국어 오류를 낸다. ``db_down``: DB 에 연결하지 못한다.
- ``build_gate``: 목록을 만들 때 시세를 이 개수까지만 받고 멈춰 둔다 (올리면 이어서 받는다). 만드는 중의 진행을 안정적으로 보려는 것이다.

화면의 갱신 간격(`config.js`)은 테스트에서 짧게 바꿔서 내려준다: 실제 갱신 코드를 그대로 쓰되 10초씩 기다리지 않게 한다.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

import uvicorn
from fakes import (
    AS_OF,
    FakeAnalysisServer,
    FakePrices,
    FakeSector,
    FakeTrend,
    make_externals,
    repo_config,
    trend_rows,
)
from helpers import make_history, make_listing, passing_closes
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from krx_screener import db
from krx_screener.api import Dependencies, create_app
from krx_screener.externals import SectorInfo

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"

# (종목코드, 이름, 업종). 거래대금이 큰 순이라 순번이 이 순서다. 코드 끝자리는 모두 0 (우선주가 아님)
STOCKS = [
    ("028050", "삼성E&A", "건설"),
    ("207940", "삼성바이오로직스", "제약"),
    ("003490", "대한항공", "항공사"),
    ("030530", "원익홀딩스", "반도체와반도체장비"),
    ("064400", "LG씨엔에스", "IT서비스"),
    ("000100", "유한양행", "제약"),
    ("263860", "지니언스", "소프트웨어"),
    ("090430", "아모레퍼시픽", "화장품"),
]
RATINGS = {  # 종목마다 점수가 달라지게 (에이전트별 등급)
    "028050": dict.fromkeys(["market", "news", "bull"], "Buy"),
    "207940": dict.fromkeys(["market", "news", "bear", "neutral"], "Sell"),
    "003490": {"market": "Overweight", "bull": "Overweight", "bear": "Hold"},
    "030530": dict.fromkeys(["market", "fundamentals"], "Underweight"),
    "064400": dict.fromkeys(["market", "news", "fundamentals", "bull"], "Overweight"),
    "000100": {},
    "263860": dict.fromkeys(["market", "bull", "aggressive"], "Buy"),
    "090430": dict.fromkeys(["market", "bear"], "Sell"),
}


@dataclass
class Control:
    """테스트가 실행 중에 바꾸는 값. 가짜들이 부를 때마다 읽는다."""

    errors: set[str] = field(default_factory=set)
    polls: int = 2
    health_error: str | None = None
    db_down: bool = False
    build_gate: int | None = None  # None 이면 막지 않는다
    price_calls: int = 0

    def update(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            if key == "errors":
                value = set(value)
            elif key not in ("polls", "health_error", "db_down", "build_gate"):
                raise KeyError(key)
            setattr(self, key, value)


class ControlledAnalysis(FakeAnalysisServer):
    """제어값을 부를 때마다 읽는 가짜 분석 서버."""

    def __init__(self, control: Control, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.control = control

    def _sync(self) -> None:
        self.errors = set(self.control.errors)
        self.health_error = self.control.health_error
        self.polls_before_done = self.control.polls

    def check_health(self) -> None:
        self._sync()
        super().check_health()

    def start(self, ticker: str, trade_date: str, effort: str) -> str:
        self._sync()
        self._polls.pop(ticker, None)  # 실행마다 처음부터 (실제 서버의 새 run 처럼)
        return super().start(ticker, trade_date, effort)

    def snapshot(self, run_id: str) -> dict[str, Any]:
        self._sync()
        return super().snapshot(run_id)


class GatedPrices(FakePrices):
    """``build_gate`` 개까지만 시세를 주고, 그 뒤는 게이트가 올라갈 때까지 기다린다 (기준일 확인용 삼성전자는 늘 바로 준다)."""

    def __init__(self, histories, control: Control) -> None:
        super().__init__(histories)
        self.control = control

    def __call__(self, code, start, end):
        if code != "005930":
            deadline = time.monotonic() + 60
            while self.control.build_gate is not None and self.control.price_calls >= self.control.build_gate:
                if time.monotonic() > deadline:
                    raise AssertionError("시세 게이트가 60초 동안 열리지 않았다")
                time.sleep(0.02)
            self.control.price_calls += 1
        return super().__call__(code, start, end)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BrowserServer:
    """백그라운드 스레드에서 도는 서버. ``url`` 로 화면을 연다."""

    def __init__(self, database_url: str, state_root: Path) -> None:
        self.control = Control()
        listing = make_listing([{"Code": c, "Name": n} for c, n, _ in STOCKS])
        histories = {c: make_history(passing_closes(), 3_000_000 - 250_000 * i, AS_OF) for i, (c, _, _) in enumerate(STOCKS)}
        histories["005930"] = make_history(passing_closes(), 3_000_000, AS_OF)
        externals = replace(
            make_externals(state_root),
            listing=lambda: listing.copy(),
            prices=GatedPrices(histories, self.control),
            trend=FakeTrend({c: trend_rows(foreign=(-100 if i % 3 == 2 else 100)) for i, (c, _, _) in enumerate(STOCKS)}),
            sector=FakeSector({c: SectorInfo(str(20 + i), s, f"{n} 의 업종 설명 예시") for i, (c, n, s) in enumerate(STOCKS)}),
            analysis=ControlledAnalysis(self.control, state_log_root=state_root, agent_ratings=RATINGS),
        )
        config = repo_config()
        config = replace(config, analysis=replace(config.analysis, poll_interval_seconds=0.1))

        def connect():
            if self.control.db_down:
                raise db.DatabaseError("PostgreSQL 에 연결하지 못했습니다 (테스트 DB 를 끈 상황).")
            return db.connect(database_url)

        deps = Dependencies(
            connect=connect, externals=externals, config=config, today=lambda: date(2026, 11, 9), retry_wait=0,
            progress_interval=0, frontend_dir=FRONTEND,
        )
        app = create_app(deps)
        app.router.routes.insert(0, Route("/__ctl", self._ctl, methods=["POST"]))
        app.router.routes.insert(0, Route("/config.js", self._config_js))  # 정적 화면보다 먼저 맞게 맨 앞에 둔다
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    async def _ctl(self, request: Request) -> JSONResponse:
        self.control.update(await request.json())
        return JSONResponse({"ok": True})

    async def _config_js(self, request: Request) -> PlainTextResponse:
        text = (FRONTEND / "config.js").read_text(encoding="utf-8")
        for old, new in (("pollMs: 10_000", "pollMs: 400"), ("buildPollMs: 2_000", "buildPollMs: 250")):
            assert old in text, f"config.js 에 {old} 가 없다 — 테스트의 갱신 간격 바꾸기를 고친다"
            text = text.replace(old, new)
        return PlainTextResponse(text, media_type="text/javascript")

    def start(self) -> None:
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("브라우저 테스트 서버가 시작되지 않았다")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
