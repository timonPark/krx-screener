"""웹 API 의 틀 (이슈 #11).

화면이 부르는 주소를 제공한다. 이 틀은 앱을 만드는 방법(``create_app``)과 오류 형식, 상태 확인만 갖고,
세션·처리·조회·정리 주소는 이후 이슈(#13 이후)가 더한다.

- DB 연결과 외부 경계는 ``Dependencies`` 로 주입한다. 테스트는 이 둘만 바꿔 끼운다.
- 모든 오류 응답은 ``{"detail": "한국어 메시지"}`` 모양이다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from krx_screener.db import DatabaseError
from krx_screener.externals import Externals


@dataclass(frozen=True)
class Dependencies:
    connect: Callable[[], psycopg.Connection]  # 요청마다 새 연결을 돌려준다
    externals: Externals


def create_app(deps: Dependencies) -> FastAPI:
    app = FastAPI(title="krx-screener")
    app.state.deps = deps

    @app.exception_handler(DatabaseError)
    def database_error(_: Request, exc: DatabaseError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

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
        return {"status": "ok", "db": "ok", "schema_version": version}

    return app
