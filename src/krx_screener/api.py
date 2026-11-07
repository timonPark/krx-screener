"""웹 API 의 틀 (이슈 #11).

화면이 부르는 주소를 제공한다. 이 틀은 앱을 만드는 방법(``create_app``)과 오류 형식, 상태 확인만 갖고,
세션·처리·조회·정리 주소는 이후 이슈(#13 이후)가 더한다.

- DB 연결과 외부 경계는 ``Dependencies`` 로 주입한다. 테스트는 이 둘만 바꿔 끼운다.
- 모든 오류 응답은 ``{"detail": "한국어 메시지"}`` 모양이다.
- ``frontend_dir`` 이 있으면 그 폴더(화면)를 ``/`` 에서 정적 파일로 같이 서빙한다 (#26). 화면의 ``apiBase`` 는 빈 값이라
  같은 주소의 ``/api/...`` 를 부르므로 CORS 가 필요 없다. 화면은 API 라우터 **뒤에** 마운트해서 ``/api`` 를 가리지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from krx_screener.analysis import AnalysisError
from krx_screener.asof import AsOfError, AsOfUnavailableError
from krx_screener.config import Config
from krx_screener.db import MIGRATIONS, DatabaseError
from krx_screener.externals import Externals
from krx_screener.repository import NotFoundError
from krx_screener.routes_sessions import router as sessions_router
from krx_screener.universe import UniverseFetchError

STALE_SCHEMA = "DB 스키마가 최신이 아닙니다. `python -m krx_screener.db init` 을 실행하세요."


def today_kst() -> date:
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


@dataclass(frozen=True)
class Dependencies:
    connect: Callable[[], psycopg.Connection]  # 요청마다 새 연결을 돌려준다
    externals: Externals
    config: Config  # 목록 조건(제외·필터)과 분석 설정
    today: Callable[[], date] = field(default=today_kst)  # 테스트가 오늘 날짜를 고정할 수 있게 주입한다
    cache_dir: Path | None = None  # 시세 캐시 폴더. None 이면 캐시하지 않는다
    retry_wait: float = 0.5  # 외부 시세 조회를 재시도하기 전에 기다리는 초
    workers: int = 8  # 종목 시세를 동시에 조회하는 수
    frontend_dir: Path | None = None  # 화면 폴더. None 이면 API 만 서빙한다
    progress_interval: float = 2.0  # 목록을 만드는 진행을 DB 에 적는 최소 간격(초). 테스트는 0 으로 두어 매번 적는다


def create_app(deps: Dependencies) -> FastAPI:
    app = FastAPI(title="krx-screener")
    app.state.deps = deps

    @app.exception_handler(DatabaseError)
    def database_error(_: Request, exc: DatabaseError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(psycopg.errors.UndefinedColumn)
    @app.exception_handler(psycopg.errors.UndefinedTable)
    def schema_is_old(_: Request, exc: Exception) -> JSONResponse:
        # 코드는 새 스키마를 쓰는데 DB 에 `db init` 을 하지 않았다 → 알 수 없는 서버 오류 대신 할 일을 알려준다
        return JSONResponse(status_code=503, content={"detail": STALE_SCHEMA})

    @app.exception_handler(AnalysisError)
    def analysis_unavailable(_: Request, exc: AnalysisError) -> JSONResponse:
        # TradingAgents-KR 서버에 연결하지 못했거나 요청을 거절했다 = 외부 장애
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(NotFoundError)
    def not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(UniverseFetchError)
    def universe_unavailable(_: Request, exc: UniverseFetchError) -> JSONResponse:
        # 전종목 목록을 받지 못함 = 외부 장애
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(AsOfUnavailableError)
    def as_of_unavailable(_: Request, exc: AsOfUnavailableError) -> JSONResponse:
        # 입력 잘못이 아니라 거래일을 확인할 수 없는 외부 장애
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(AsOfError)
    def as_of_error(_: Request, exc: AsOfError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 모든 오류 응답을 {"detail": "한국어 메시지"} 로 맞춘다 (FastAPI 기본은 영어 목록이다)
        first = exc.errors()[0] if exc.errors() else {}
        if "as_of" in first.get("loc", ()):
            if first.get("type") == "missing":
                message = "기준일(as_of)을 입력하세요 (예: 2026-11-05)."
            else:
                message = "기준일 형식이 올바르지 않습니다 (예: 2026-11-05)."
        elif {"offset", "count"} & set(first.get("loc", ())):
            message = "오프셋은 0 이상, 개수는 1 이상의 정수로 입력하세요."
        else:
            message = "요청 형식이 올바르지 않습니다."
        return JSONResponse(status_code=422, content={"detail": message})

    @app.get("/api/health")
    def health(request: Request) -> dict:
        """서버와 DB 상태. 스키마가 적용된 버전도 같이 알려준다."""
        with request.app.state.deps.connect() as conn:
            try:
                version = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
            except psycopg.errors.UndefinedTable:
                raise DatabaseError(
                    "DB 스키마가 없습니다. `python -m krx_screener.db init` 을 실행하세요."
                ) from None
        if version is None or version < MIGRATIONS[-1][0]:
            raise DatabaseError(STALE_SCHEMA)
        return {"status": "ok", "db": "ok", "schema_version": version}

    app.include_router(sessions_router)
    if deps.frontend_dir is not None:
        # 마지막에 마운트한다: 위의 /api 주소가 먼저 맞고, 나머지 주소는 화면 파일로 간다
        app.mount("/", StaticFiles(directory=deps.frontend_dir, html=True), name="frontend")
    return app
