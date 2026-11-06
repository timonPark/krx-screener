"""세션 주소 (이슈 #13): 세션 만들기와 목록.

- ``POST /api/sessions``  ``{"as_of": "2026-11-05"}`` → 새 세션 (201)
- ``GET  /api/sessions``  → 최근 순 세션 목록

기준일은 거래일이어야 한다. 입력이 잘못되면 422, 거래일이나 전종목 목록을 받을 수 없는 외부 장애면 503 이고,
둘 다 ``{"detail": "한국어 메시지"}`` 다.

세션을 만들 때 목록(분석 대상 종목)을 같이 채운다 (``listing.build_items``). 목록 계산은 시세를 종목마다
받아서 오래 걸릴 수 있으므로 DB 연결을 열기 전에 끝내고, 세션과 종목은 한 번에 저장한다 (하나라도 실패하면
세션도 남지 않는다). 수급 상태(#15)와 업종 정보(#16)는 목록을 만들 때 같이 채운다.
"""

from __future__ import annotations

from datetime import UTC, date

from fastapi import APIRouter, Request
from pydantic import BaseModel

from krx_screener.asof import resolve_as_of
from krx_screener.listing import build_items
from krx_screener.repository import Session, SessionRepository

router = APIRouter(prefix="/api/sessions")


class SessionCreate(BaseModel):
    as_of: date


class SessionOut(BaseModel):
    id: str
    as_of: date
    created_at: str  # UTC, 예: 2026-11-06T00:12:00Z
    total: int

    @classmethod
    def of(cls, session: Session) -> SessionOut:
        created = session.created_at.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        return cls(id=str(session.id), as_of=session.as_of, created_at=created, total=session.total)


@router.post("", status_code=201, response_model=SessionOut)
def create_session(body: SessionCreate, request: Request) -> SessionOut:
    deps = request.app.state.deps
    # 거래일이 아니거나 미래면 resolve_as_of 가 AsOfError(한국어)를 낸다 → api.py 가 응답으로 바꾼다
    resolve_as_of(deps.today(), body.as_of, deps.externals.prices)
    items = build_items(
        deps.externals,
        deps.config,
        body.as_of,
        deps.today(),
        workers=deps.workers,
        cache_dir=deps.cache_dir,
        retry_wait=deps.retry_wait,
    )
    with deps.connect() as conn:  # 한 트랜잭션: 종목 저장이 실패하면 세션도 남지 않는다
        repo = SessionRepository(conn)
        session = repo.create_session(body.as_of)
        repo.add_items(session.id, items)
        saved = repo.get_session(session.id)
    assert saved is not None
    return SessionOut.of(saved)


@router.get("", response_model=list[SessionOut])
def list_sessions(request: Request) -> list[SessionOut]:
    with request.app.state.deps.connect() as conn:
        return [SessionOut.of(s) for s in SessionRepository(conn).list_sessions()]

