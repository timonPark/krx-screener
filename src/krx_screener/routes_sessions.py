"""세션 주소 (이슈 #13): 세션 만들기와 목록.

- ``POST /api/sessions``  ``{"as_of": "2026-11-05"}`` → 새 세션 (201)
- ``GET  /api/sessions``  → 최근 순 세션 목록
- ``GET  /api/sessions/{id}``  (``id`` 에 ``latest`` 가능) → 세션 전체 조회 (#23)
- ``POST /api/sessions/{id}/process``  ``{"offset": 0, "count": 10}`` → 구간 처리 시작 (#17)

기준일은 거래일이어야 한다. 입력이 잘못되면 422, 거래일이나 전종목 목록을 받을 수 없는 외부 장애면 503 이고,
둘 다 ``{"detail": "한국어 메시지"}`` 다.

세션을 만들 때 목록(분석 대상 종목)을 같이 채운다 (``listing.build_items``). 목록 계산은 시세를 종목마다
받아서 오래 걸릴 수 있으므로 DB 연결을 열기 전에 끝내고, 세션과 종목은 한 번에 저장한다 (하나라도 실패하면
세션도 남지 않는다). 수급 상태(#15)와 업종 정보(#16)는 목록을 만들 때 같이 채운다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from krx_screener.analysis import RATING_LABELS, REVIEW
from krx_screener.asof import resolve_as_of
from krx_screener.flow import STATUS_LABELS
from krx_screener.listing import build_items
from krx_screener.processing import process_codes
from krx_screener.repository import Item, NotFoundError, Session, SessionRepository

router = APIRouter(prefix="/api/sessions")


class SessionCreate(BaseModel):
    as_of: date


def _utc(moment: datetime | None) -> str | None:
    """UTC 문자열 (예: 2026-11-06T00:12:00Z). 화면이 한국 시각으로 바꿔 보여준다."""
    if moment is None:
        return None
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class SessionOut(BaseModel):
    id: str
    as_of: date
    created_at: str  # UTC, 예: 2026-11-06T00:12:00Z
    total: int

    @classmethod
    def of(cls, session: Session) -> SessionOut:
        return cls(id=str(session.id), as_of=session.as_of, created_at=_utc(session.created_at), total=session.total)


class ItemOut(BaseModel):
    """종목 한 줄. 키는 영어, 사용자에게 보이는 값(수급 상태, 등급)은 한국어다 (저장은 영어 코드값)."""

    seq: int
    code: str
    name: str
    market: str
    sector_code: str
    sector_name: str
    description: str
    is_caution: bool
    is_foreign: bool
    close: int | None
    avg_trading_value: int | None
    rsi: float | None
    flow_status: str  # 양호 | 부분 | 약함 | 미확인
    foreign_net_5d: int | None
    institution_net_5d: int | None
    state: str  # pending | running | done | failed
    analyzed_at: str | None
    error_message: str
    report_id: str
    decision_label: str  # 매수 | 비중 확대 | 보유 | 비중 축소 | 매도 | REVIEW | 빈 문자열
    score: float | None
    agents_rated: int
    agents_total: int
    rank: int | None

    @classmethod
    def of(cls, item: Item) -> ItemOut:
        return cls(
            seq=item.seq, code=item.code, name=item.name, market=item.market,
            sector_code=item.sector_code, sector_name=item.sector_name, description=item.description,
            is_caution=item.is_caution, is_foreign=item.is_foreign,
            close=item.close, avg_trading_value=item.avg_trading_value, rsi=item.rsi,
            flow_status=STATUS_LABELS.get(item.flow_status, STATUS_LABELS["unconfirmed"]),
            foreign_net_5d=item.foreign_net_5d, institution_net_5d=item.institution_net_5d,
            state=item.state, analyzed_at=_utc(item.analyzed_at), error_message=item.error_message,
            report_id=item.report_id,
            decision_label=item.decision if item.decision == REVIEW else RATING_LABELS.get(item.decision, ""),
            score=item.score, agents_rated=item.agents_rated, agents_total=item.agents_total, rank=item.rank,
        )


class CountsOut(BaseModel):
    pending: int
    running: int
    done: int
    failed: int


class SessionDetailOut(BaseModel):
    session: SessionOut
    processing: bool  # 이 세션을 처리 중인지. true 면 화면이 주기적으로 다시 부른다
    ranked_at: str | None  # 정리를 실행한 시각, 안 했으면 null
    rank_stale: bool  # 정리 이후 재분석된 종목이 있으면 true
    counts: CountsOut
    items: list[ItemOut]  # 세션 전체, 순번 순


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



def _find_session(repo: SessionRepository, session_id: str) -> Session:
    """``latest`` 는 가장 최근 세션. 없거나 UUID 형식이 아니면 한국어 404 (NotFoundError)."""
    if session_id == "latest":
        session = repo.latest_session()
        if session is None:
            raise NotFoundError("세션이 없습니다. 먼저 새 목록을 뽑으세요.")
        return session
    try:
        session = repo.get_session(uuid.UUID(session_id))
    except ValueError:
        session = None  # 형식이 UUID 가 아니면 없는 세션과 같게 다룬다
    if session is None:
        raise NotFoundError("세션을 찾지 못했습니다.")
    return session


@router.get("/{session_id}", response_model=SessionDetailOut)
def get_session(session_id: str, request: Request) -> SessionDetailOut:
    """세션 전체를 처리 여부와 함께 조회한다. ``latest`` 는 가장 최근 세션이다."""
    with request.app.state.deps.connect() as conn:
        repo = SessionRepository(conn)
        session = _find_session(repo, session_id)
        counts = repo.counts(session.id)
        items = repo.items(session.id)
    return SessionDetailOut(
        session=SessionOut.of(session),
        # 처리 중 표시는 지금은 실행 중인 종목이 있는지로 정한다. 세션 단위 잠금(#18)이 생기면 잠금 상태를 따른다.
        processing=counts["running"] > 0,
        ranked_at=_utc(session.ranked_at),
        rank_stale=False,  # 정리(#24) 이후 재분석 여부는 #25 가 계산한다
        counts=CountsOut(**counts),
        items=[ItemOut.of(i) for i in items],
    )


class ProcessRequest(BaseModel):
    offset: int = Field(ge=0)  # 목록 앞에서 건너뛸 개수
    count: int = Field(ge=1)  # 거기서부터 처리할 개수


@router.post("/{session_id}/process", response_model=None)
def process(session_id: str, body: ProcessRequest, request: Request, background: BackgroundTasks) -> JSONResponse:
    """목록의 ``offset`` 번째 뒤로 ``count`` 개를 처리한다 (순번이 offset+1 ~ offset+count).

    바로 ``202 {"started": true, "queued": N}`` 으로 응답하고 분석은 뒤에서 한 종목씩 돌린다. 구간 안의 종목이
    모두 끝나 있으면 아무것도 보내지 않고 ``200 {"started": false, "queued": 0}`` 이다.
    """
    deps = request.app.state.deps
    with deps.connect() as conn:
        repo = SessionRepository(conn)
        session = _find_session(repo, session_id)
        items = repo.items(session.id)
    if body.offset >= len(items):
        raise HTTPException(422, f"오프셋이 종목 수({len(items)})보다 크거나 같습니다.")
    window = [i for i in items if body.offset < i.seq <= body.offset + body.count]
    todo = [i.code for i in window if i.state != "done"]  # 끝난 종목은 다시 보내지 않는다
    if not todo:
        return JSONResponse(status_code=200, content={"started": False, "queued": 0})
    deps.externals.analysis.check_health()  # 서버가 꺼져 있으면 아무것도 시작하지 않고 한국어 503
    background.add_task(process_codes, deps, session.id, session.as_of, todo)
    return JSONResponse(status_code=202, content={"started": True, "queued": len(todo)})
