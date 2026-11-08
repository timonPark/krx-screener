"""세션 주소 (이슈 #13): 세션 만들기와 목록.

- ``POST /api/sessions``  ``{"as_of": "2026-11-05"}`` → 새 세션을 바로 만들고 **202** 로 응답한다. 종목은 뒤에서 채운다 (#56).
  같은 기준일을 이미 만드는 중이면 ``409 {"detail", "session_id"}``.
- ``GET  /api/sessions``  → 최근 순 세션 목록
- ``GET  /api/sessions/{id}``  (``id`` 에 ``latest`` 가능) → 세션 전체 조회 (#23)
- ``POST /api/sessions/{id}/process``  ``{"offset": 0, "count": 10}`` → 구간 처리 시작 (#17). 실패한 종목은 보내지 않는다 (#77)
- ``POST /api/sessions/{id}/process-failed``  → 세션의 실패한 종목을 다시 처리 (#77)
- ``POST /api/sessions/{id}/organize``  → 정리: 끝난 종목에 점수 순위를 매긴다 (#24)

기준일은 거래일이어야 한다. 입력이 잘못되면 422, 거래일이나 전종목 목록을 받을 수 없는 외부 장애면 503 이고,
둘 다 ``{"detail": "한국어 메시지"}`` 다.

세션을 만들 때 목록(분석 대상 종목)은 뒤에서 채운다 (``session_build.build_session``). 시세를 처음 받을 때는 전종목이라 약 20분이
걸려서 요청이 끝나길 기다리지 않는다. 진행은 세션의 ``build`` (상태·단계·한 개수·전체 개수)로 본다: ``building`` → ``ready`` /
``failed`` / ``interrupted``(만들던 서버가 죽음). 종목은 다 만든 뒤에 한 트랜잭션으로 저장한다. 만드는 중에는 처리·정리를 못 한다 (409).
수급 상태(#15)와 업종 정보(#16)는 목록을 만들 때 같이 채운다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import psycopg
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from krx_screener.analysis import RATING_LABELS, REVIEW, server_accepts
from krx_screener.asof import resolve_as_of
from krx_screener.config import criteria_of
from krx_screener.flow import STATUS_LABELS
from krx_screener.processing import process_codes
from krx_screener.repository import Item, NotFoundError, Session, SessionRepository
from krx_screener.session_build import build_session

router = APIRouter(prefix="/api/sessions")


class SessionCreate(BaseModel):
    as_of: date


def _utc(moment: datetime | None) -> str | None:
    """UTC 문자열 (예: 2026-11-06T00:12:00Z). 화면이 한국 시각으로 바꿔 보여준다."""
    if moment is None:
        return None
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class BuildOut(BaseModel):
    """목록을 만드는 중의 상태. 완성된 세션은 ``state = ready`` 이고 나머지 값은 의미가 없다."""

    state: str  # building | ready | failed | interrupted(만들던 서버가 죽음)
    stage: str  # 만드는 중일 때: prices(시세) | flow(수급) | sector(업종). 아니면 빈 문자열
    done: int  # 그 단계에서 끝난 개수
    total: int  # 그 단계의 전체 개수 (시세 단계 처음에는 아직 모르면 0)
    error: str  # failed 일 때 한국어 사유


class FiltersOut(BaseModel):
    min_avg_trading_value_krw: int
    avg_trading_value_window: int
    price_above_ma: list[int]
    rsi_max: float | None  # None 이면 RSI 조건 없음


class FlowCriteriaOut(BaseModel):
    mode: str  # mark(표시만) | filter(약함 탈락)
    window_days: int


class CriteriaOut(BaseModel):
    """세션이 목록을 뽑을 때 쓴 조건 (#57). 값 그대로이고 문구는 화면이 만든다."""

    filters: FiltersOut
    exclude: list[str]  # 제외한 종류 (preferred, spac, managed, zero_volume, new_listing, reit, caution, foreign)
    flow: FlowCriteriaOut


class SessionOut(BaseModel):
    id: str
    as_of: date
    created_at: str  # UTC, 예: 2026-11-06T00:12:00Z
    total: int
    build: BuildOut
    criteria: CriteriaOut | None  # None 이면 조건 기록 없음 (기존 세션, 명령줄 세션)

    @classmethod
    def of(cls, session: Session, build_state: str | None = None) -> SessionOut:
        """``build_state`` 는 ``repo.effective_build_state`` 가 계산한 값 (중단됨 포함). 주지 않으면 저장된 값을 쓴다."""
        build = BuildOut(
            state=build_state or session.build_state,
            stage=session.build_stage,
            done=session.build_done,
            total=session.build_total,
            error=session.build_error,
        )
        return cls(
            id=str(session.id),
            as_of=session.as_of,
            created_at=_utc(session.created_at),
            total=session.total,
            build=build,
            criteria=session.criteria,
        )


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


@router.post("", status_code=202, response_model=SessionOut)
def create_session(body: SessionCreate, request: Request, background: BackgroundTasks) -> JSONResponse | SessionOut:
    deps = request.app.state.deps
    # 거래일이 아니거나 미래면 resolve_as_of 가 AsOfError(한국어)를 낸다 → api.py 가 응답으로 바꾼다 (세션은 만들지 않는다)
    resolve_as_of(deps.today(), body.as_of, deps.externals.prices)
    conn = deps.connect()
    conn.autocommit = True
    handed_over = False  # 연결(과 잠금)을 뒤에서 만드는 일에 넘겼는가. 넘기지 않았으면 여기서 닫아 잠금을 푼다
    try:
        repo = SessionRepository(conn)
        repo.discard_unfinished(body.as_of)  # 같은 기준일로 다시 뽑으면 못 끝낸(실패·중단된) 목록은 지운다
        session_id = uuid.uuid4()
        # 세션이 보이기 전에 잠금을 먼저 쥔다: 잠금 없는 만드는 중 세션은 '중단됨'으로 보여 다른 요청이 지울 수 있다
        repo.try_lock(session_id)
        try:
            session = repo.create_building_session(session_id, body.as_of, criteria_of(deps.config))
        except psycopg.errors.UniqueViolation:  # 같은 기준일을 이미 만드는 중이다 (DB 의 유일 인덱스가 막는다)
            running = repo.building_session_id(body.as_of)
            return JSONResponse(
                status_code=409,
                content={
                    "detail": f"이미 {body.as_of.isoformat()} 목록을 뽑는 중입니다.",
                    "session_id": str(running) if running else None,
                },
            )
        background.add_task(build_session, deps, conn, session_id, body.as_of)
        handed_over = True
        return SessionOut.of(session)
    finally:
        if not handed_over:
            conn.close()


@router.get("", response_model=list[SessionOut])
def list_sessions(request: Request) -> list[SessionOut]:
    with request.app.state.deps.connect() as conn:
        repo = SessionRepository(conn)
        return [SessionOut.of(s, repo.effective_build_state(s)) for s in repo.list_sessions()]


def _unfinished_message(repo: SessionRepository, session: Session, action: str) -> str | None:
    """목록이 아직 완성되지 않았으면(만드는 중·실패·중단됨) ``action``(처리·정리)을 못 하는 이유. 완성됐으면 None."""
    state = repo.effective_build_state(session)
    if state == "building":
        return f"목록을 만드는 중입니다. 끝난 뒤에 {action}할 수 있습니다."
    if state == "interrupted":
        return "목록을 만들다 서버가 멈춰 완성되지 않았습니다. 같은 기준일로 다시 뽑으세요."
    if state == "failed":
        return f"목록을 만들지 못했습니다: {session.build_error}"
    return None


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
        # 처리 중인지는 세션의 처리 잠금으로 안다. 종목 상태(running)는 처리하던 프로세스가 죽으면 남을 수 있다.
        # 목록을 만드는 동안에도 같은 잠금을 쥐지만 그것은 처리가 아니다.
        # 읽는 도중에 처리가 끝나거나 시작할 수 있어서 잠금을 종목을 읽기 전·후에 모두 보고 하나라도 쥐고 있으면 처리 중으로 답한다.
        # (종목만 먼저 읽고 잠금을 나중에 보면 running 종목이 있는데 processing=false 가 되어 화면이 갱신을 멈추고 '처리 중' 으로 남는다.)
        locked_before = repo.is_locked(session.id)
        counts = repo.counts(session.id)
        items = repo.items(session.id)
        build_state = repo.effective_build_state(session)
        processing = build_state == "ready" and (locked_before or repo.is_locked(session.id))
        rank_stale = repo.rank_is_stale(session.id)
    return SessionDetailOut(
        session=SessionOut.of(session, build_state),
        processing=processing,
        ranked_at=_utc(session.ranked_at),
        rank_stale=rank_stale,  # 정리 뒤에 분석이 바뀌어 다시 정리하면 순위가 달라질 수 있으면 true (#25)
        counts=CountsOut(**counts),
        items=[ItemOut.of(i) for i in items],
    )


class ProcessRequest(BaseModel):
    offset: int = Field(ge=0)  # 목록 앞에서 건너뛸 개수
    count: int = Field(ge=1)  # 거기서부터 처리할 개수


def _start_processing(
    session_id: str, request: Request, background: BackgroundTasks, choose, recover: bool = False
) -> JSONResponse:
    """구간 처리와 실패 재처리가 같이 쓰는 시작 절차. ``choose(items)`` 가 이번에 보낼 종목코드를 순서대로 돌려준다.

    잠금(409)·서버 상태 확인(503)·응답 모양이 두 경로에서 같도록 한 곳에 둔다. ``recover`` 는 서버에 끝난 분석이 있으면 새로 보내지 않고 가져온다 (#75).
    """
    deps = request.app.state.deps
    conn = deps.connect()
    conn.autocommit = True
    handed_over = False  # 연결(과 잠금)을 뒤의 처리에 넘겼는가. 넘기지 않았으면 여기서 닫아 잠금을 푼다
    try:
        repo = SessionRepository(conn)
        session = _find_session(repo, session_id)
        unfinished = _unfinished_message(repo, session, "처리")
        if unfinished:  # 만드는 중에는 같은 잠금을 만드는 쪽이 쥐고 있어서, 잠그기 전에 거른다
            raise HTTPException(409, unfinished)
        # 종목을 읽기 전에 잠근다: 동시에 온 두 요청이 같은 종목을 둘 다 대기열에 올리지 못하게
        if not repo.try_lock(session.id):
            raise HTTPException(409, "이미 처리 중입니다.")
        todo = choose(repo.items(session.id))
        if not todo:
            return JSONResponse(status_code=200, content={"started": False, "queued": 0})
        deps.externals.analysis.check_health()  # 서버가 꺼져 있으면 아무것도 시작하지 않고 한국어 503
        background.add_task(process_codes, deps, conn, session.id, session.as_of, todo, recover)
        handed_over = True
        return JSONResponse(status_code=202, content={"started": True, "queued": len(todo)})
    finally:
        if not handed_over:
            conn.close()


@router.post("/{session_id}/process", response_model=None)
def process(session_id: str, body: ProcessRequest, request: Request, background: BackgroundTasks) -> JSONResponse:
    """목록의 ``offset`` 번째 뒤로 ``count`` 개를 처리한다 (순번이 offset+1 ~ offset+count).

    **끝나지 않은 종목(대기, 그리고 처리하던 서버가 죽어 처리 중으로 남은 종목)만** 보낸다. 끝난 종목은 다시 보내지 않고, 실패한
    종목도 보내지 않는다: 순번이 앞선 실패 종목이 아직 안 돌린 대기 종목보다 먼저 돌아 쿼터와 시간을 쓰는 것을 막는다 (#77).
    실패한 종목은 ``POST /{id}/process-failed`` 로 다시 처리한다.

    바로 ``202 {"started": true, "queued": N}`` 으로 응답하고 분석은 뒤에서 한 종목씩 돌린다. 구간 안에 보낼 종목이 없으면
    아무것도 보내지 않고 ``200 {"started": false, "queued": 0}`` 이다.
    같은 세션이 이미 처리 중이면 ``409`` 다 (세션 단위 잠금, #18). 다른 세션은 막히지 않는다.
    """

    def choose(items: list) -> list[str]:
        if body.offset >= len(items):
            raise HTTPException(422, f"오프셋이 종목 수({len(items)})보다 크거나 같습니다.")
        window = [i for i in items if body.offset < i.seq <= body.offset + body.count]
        return [i.code for i in window if i.state not in ("done", "failed")]

    return _start_processing(session_id, request, background, choose)


@router.post("/{session_id}/process-failed", response_model=None)
def process_failed(session_id: str, request: Request, background: BackgroundTasks) -> JSONResponse:
    """세션 전체에서 **실패한 종목을** 순번 순서대로 다시 처리한다 (#77). 구간(오프셋·개수)은 받지 않는다.

    서버가 숫자 6자리 종목코드만 받아서, 다시 보내도 계속 실패하는 영숫자 코드는 뺀다 (쿼터는 쓰지 않지만 같은 사유로 즉시 실패할 뿐이다).
    다시 보낼 종목이 없으면 ``200 {"started": false, "queued": 0}``. 응답 모양·잠금(409)·서버 확인(503)은 ``/process`` 와 같다.
    **종목마다 먼저 서버에 같은 종목·기준일의 끝난 분석(보고서)이 있는지 본다** (#75). 있으면 가장 최근 보고서의 결과를 저장하고(쿼터 0),
    없을 때만 새로 보낸다. 연결이 끊기거나 시간 초과로 실패로 기록됐지만 서버에서는 끝난 종목이 여기에 해당한다.
    서버가 보고서 목록의 필터·기준일(TradingAgents-KR #174)을 모르는 옛 버전이면 끝난 분석을 찾지 못해 예전처럼 새로 보낸다.
    """

    def choose(items: list) -> list[str]:
        return [i.code for i in items if i.state == "failed" and server_accepts(i.code)]

    return _start_processing(session_id, request, background, choose, recover=True)


@router.post("/{session_id}/organize", response_model=None)
def organize(session_id: str, request: Request) -> JSONResponse:
    """세션의 끝난 종목에 점수 높은 순으로 순위를 매긴다 (점수가 같으면 순번이 앞선 종목이 위).

    ``200 {"ranked": N, "unscored": M}`` — ``unscored`` 는 끝났지만 점수가 없어 순위에서 뺀 종목 수다.
    대기·처리 중인 종목이 하나라도 있으면 ``409 {"detail": "…", "remaining": N}`` 이고 아무것도 바꾸지 않는다.
    실패한 종목은 남은 개수에 넣지 않고 순위에서 뺀다. 처리 중인 세션은 정리하지 않는다 (같은 세션 잠금을 쓴다).
    """
    with request.app.state.deps.connect() as conn:
        conn.autocommit = True
        repo = SessionRepository(conn)
        session = _find_session(repo, session_id)
        unfinished = _unfinished_message(repo, session, "정리")
        if unfinished:
            return JSONResponse(status_code=409, content={"detail": unfinished, "remaining": 0})
        # 처리와 같은 잠금: 정리하는 동안 같은 세션의 처리가 시작되어 점수가 바뀌지 않게 한다. 연결을 닫으면 풀린다
        locked = repo.try_lock(session.id)
        counts = repo.counts(session.id)
        remaining = counts["pending"] + counts["running"]  # 실패는 남은 개수가 아니다 (비교에서 빠진다)
        if not locked:
            return JSONResponse(status_code=409, content={"detail": "이미 처리 중입니다.", "remaining": remaining})
        if remaining:
            return JSONResponse(
                status_code=409, content={"detail": f"남은 {remaining}개가 끝나야 정리할 수 있습니다.", "remaining": remaining}
            )
        ranking = repo.organize(session.id)
    return JSONResponse(status_code=200, content={"ranked": ranking.ranked, "unscored": ranking.unscored})
