"""세션·종목·에이전트 등급 저장소 계층 (이슈 #10).

PostgreSQL 에 읽고 쓰기만 한다. 외부 서버 호출이나 분석 규칙은 여기 두지 않는다.
상태·등급·수급 상태는 영어 코드값(``pending``/``Buy``/``strong`` …)으로 다루고, 한국어 표시는 API 계층이 바꾼다.

연결은 호출하는 쪽이 만들어 넘긴다 (``db.connect``). 여러 줄을 한꺼번에 바꾸는 동작은 한 트랜잭션이라
중간에 실패하면 아무것도 바뀌지 않는다.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

STATES = ("pending", "running", "done", "failed")


class NotFoundError(LookupError):
    """세션이나 종목이 없을 때. 메시지는 사용자에게 그대로 보여준다."""


@dataclass(frozen=True)
class Session:
    id: uuid.UUID
    as_of: date
    created_at: datetime
    ranked_at: datetime | None
    total: int  # 세션 안의 종목 수 (보관한 종목은 세지 않는다)
    source: str = "web"  # web = 웹 화면이 만든 세션, cli = 명령줄 분석 대기열
    # 목록을 만드는 중의 상태 (#56). 완성된 세션은 build_state = ready
    build_state: str = "ready"  # building | ready | failed (중단됨은 저장하지 않고 effective_build_state 가 계산한다)
    build_stage: str = ""  # prices | flow | sector | 빈 문자열
    build_done: int = 0
    build_total: int = 0
    build_error: str = ""
    # 목록을 뽑을 때 쓴 조건 (#57). None 이면 조건 기록 없음 (기존 세션, 명령줄 세션)
    criteria: dict | None = None


@dataclass(frozen=True)
class Ranking:
    """정리 결과."""

    ranked: int  # 순위를 매긴 종목 수
    unscored: int  # 끝났지만 점수가 없어 순위에서 뺀 종목 수


@dataclass
class Item:
    """세션 안의 종목 한 줄. 앞의 네 개는 목록을 만들 때 반드시 정해진다."""

    seq: int
    code: str
    name: str
    market: str
    close: int | None = None
    avg_trading_value: int | None = None
    rsi: float | None = None
    flow_status: str = "unconfirmed"
    foreign_net_5d: int | None = None
    institution_net_5d: int | None = None
    sector_code: str = ""
    sector_name: str = ""
    description: str = ""
    is_caution: bool = False
    is_foreign: bool = False
    # 아래는 처리하면서 채워진다
    state: str = "pending"
    analyzed_at: datetime | None = None
    error_message: str = ""
    report_id: str = ""
    decision: str = ""
    score: float | None = None
    agents_rated: int = 0
    agents_total: int = 0
    rank: int | None = None


# 목록을 만들 때 넣는 칼럼 (처리 결과 칼럼은 기본값으로 시작한다)
_LISTED = (
    "seq", "code", "name", "market", "close", "avg_trading_value", "rsi", "flow_status",
    "foreign_net_5d", "institution_net_5d", "sector_code", "sector_name", "description",
    "is_caution", "is_foreign",
)
_ITEM_COLUMNS = ", ".join(f.name for f in fields(Item))
_SESSION_SQL = (
    "SELECT s.id, s.as_of, s.created_at, s.ranked_at, s.source, "
    "s.build_state, s.build_stage, s.build_done, s.build_total, s.build_error, s.criteria, "
    "(SELECT count(*) FROM session_items i WHERE i.session_id = s.id AND NOT i.archived) AS total "
    "FROM sessions s"
)


class SessionRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def _cursor(self) -> psycopg.Cursor:
        return self._conn.cursor(row_factory=dict_row)

    # --- 세션 ---

    def create_session(self, as_of: date) -> Session:
        """새 세션을 만든다. UUID 는 매번 새로 만들어서 같은 날 두 번 만들어도 서로 다르다."""
        session_id = uuid.uuid4()
        self._conn.execute("INSERT INTO sessions (id, as_of) VALUES (%s, %s)", (session_id, as_of))
        session = self.get_session(session_id)
        assert session is not None
        return session

    # --- 목록을 만드는 중인 세션 (#56) ---

    def create_building_session(self, session_id: uuid.UUID, as_of: date, criteria: dict | None = None) -> Session:
        """종목이 없는 채로 만드는 중(building)인 세션을 만든다. 같은 기준일에 이미 만드는 중이면 ``UniqueViolation``.

        ``session_id`` 는 부르는 쪽이 정한다: 세션이 보이기 **전에** 그 id 의 처리 잠금을 먼저 쥐어 두어야 다른 요청이 잠금 없는
        만들기(=중단됨)로 오해해 지우지 않는다. ``criteria`` 는 이 목록을 뽑는 데 쓰는 조건 (``config.criteria_of``).
        """
        self._conn.execute(
            "INSERT INTO sessions (id, as_of, build_state, criteria) VALUES (%s, %s, 'building', %s)",
            (session_id, as_of, None if criteria is None else Jsonb(criteria)),
        )
        session = self.get_session(session_id)
        assert session is not None
        return session

    def set_build_progress(self, session_id: uuid.UUID, stage: str, done: int, total: int) -> None:
        self._conn.execute(
            "UPDATE sessions SET build_stage = %s, build_done = %s, build_total = %s WHERE id = %s",
            (stage, done, total, session_id),
        )

    def finish_build(self, session_id: uuid.UUID, items: Sequence[Item]) -> None:
        """종목을 저장하고 완성(ready)으로 바꾼다. 한 트랜잭션이라 저장이 실패하면 둘 다 반영되지 않는다."""
        with self._conn.transaction():
            self.add_items(session_id, items)
            self._conn.execute(
                "UPDATE sessions SET build_state = 'ready', build_stage = '', build_done = %s, build_total = %s WHERE id = %s",
                (len(items), len(items), session_id),
            )

    def fail_build(self, session_id: uuid.UUID, message: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET build_state = 'failed', build_stage = '', build_error = %s WHERE id = %s",
            (message, session_id),
        )

    def building_session_id(self, as_of: date) -> uuid.UUID | None:
        """그 기준일 목록을 만드는 중인 웹 세션의 id (없으면 None)."""
        row = self._conn.execute(
            "SELECT id FROM sessions WHERE as_of = %s AND source = 'web' AND build_state = 'building'", (as_of,)
        ).fetchone()
        return row[0] if row else None

    def discard_unfinished(self, as_of: date) -> int:
        """그 기준일의 못 끝낸 웹 세션(실패, 중단됨)을 지운다. 지금 만드는 중인 것과 완성된 것은 지우지 않는다."""
        rows = self._conn.execute(
            "SELECT id, build_state FROM sessions WHERE as_of = %s AND source = 'web' AND build_state IN ('building', 'failed')",
            (as_of,),
        ).fetchall()
        removed = 0
        for session_id, state in rows:
            if state == "building" and self.is_locked(session_id):
                continue  # 만드는 연결이 잠금을 쥐고 있다 = 살아 있다
            self._conn.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
            removed += 1
        return removed

    def effective_build_state(self, session: Session) -> str:
        """만들기 상태. 만드는 중(building)인데 만드는 잠금이 없으면 서버가 중간에 죽은 것이라 ``interrupted`` 다."""
        if session.build_state == "building" and not self.is_locked(session.id):
            return "interrupted"
        return session.build_state

    def get_session(self, session_id: uuid.UUID) -> Session | None:
        with self._cursor() as cur:
            row = cur.execute(f"{_SESSION_SQL} WHERE s.id = %s", (session_id,)).fetchone()
        return Session(**row) if row else None

    def latest_session(self) -> Session | None:
        """가장 최근에 만든 세션. 세션을 지정하지 않았을 때 쓴다."""
        with self._cursor() as cur:
            row = cur.execute(f"{_SESSION_SQL} ORDER BY s.created_at DESC, s.id DESC LIMIT 1").fetchone()
        return Session(**row) if row else None

    def list_sessions(self) -> list[Session]:
        """최근 순."""
        with self._cursor() as cur:
            rows = cur.execute(f"{_SESSION_SQL} ORDER BY s.created_at DESC, s.id DESC").fetchall()
        return [Session(**r) for r in rows]

    # --- 종목 ---

    def add_items(self, session_id: uuid.UUID, items: Sequence[Item]) -> None:
        """목록(종목들)을 세션에 넣는다. 하나라도 실패하면 하나도 들어가지 않는다."""
        columns = ", ".join(("session_id", *_LISTED))
        marks = ", ".join(["%s"] * (len(_LISTED) + 1))
        rows = [(session_id, *(getattr(item, c) for c in _LISTED)) for item in items]
        try:
            with self._conn.transaction(), self._conn.cursor() as cur:
                cur.executemany(f"INSERT INTO session_items ({columns}) VALUES ({marks})", rows)
        except psycopg.errors.ForeignKeyViolation:
            raise NotFoundError("세션을 찾지 못했습니다.") from None

    def items(self, session_id: uuid.UUID) -> list[Item]:
        """세션 전체 종목을 순번 순으로 (보관한 종목은 뺀다)."""
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT {_ITEM_COLUMNS} FROM session_items WHERE session_id = %s AND NOT archived ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [Item(**r) for r in rows]

    def get_item(self, session_id: uuid.UUID, code: str) -> Item | None:
        with self._cursor() as cur:
            row = cur.execute(
                f"SELECT {_ITEM_COLUMNS} FROM session_items WHERE session_id = %s AND code = %s",
                (session_id, code),
            ).fetchone()
        return Item(**row) if row else None

    def counts(self, session_id: uuid.UUID) -> dict[str, int]:
        """처리 상태별 개수. 없는 상태도 0 으로 채워서 돌려준다."""
        found = dict.fromkeys(STATES, 0)
        with self._cursor() as cur:
            for row in cur.execute(
                "SELECT state, count(*) AS n FROM session_items WHERE session_id = %s AND NOT archived GROUP BY state",
                (session_id,),
            ):
                found[row["state"]] = row["n"]
        return found

    # --- 세션 단위 처리 잠금 (#18) ---
    #
    # PostgreSQL 의 세션(연결) 단위 advisory lock 이다. 처리하는 연결이 잠금을 쥐고 있다가, 처리가 끝나서 연결을
    # 닫거나 프로세스가 죽어 연결이 끊기면 DB 가 잠금을 푼다. 그래서 잠금이 남아서 영원히 막히는 일이 없다.
    # 잠금을 쥔 연결은 처리가 끝날 때까지 닫지 말아야 한다 (트랜잭션 안에 두지 않도록 자동 커밋 연결로 쓴다).

    @staticmethod
    def _lock_key(session_id: uuid.UUID) -> int:
        """세션 id 의 앞 8바이트로 만든 부호 있는 64비트 정수. 세션마다 다른 잠금이 된다."""
        return int.from_bytes(session_id.bytes[:8], "big", signed=True)

    def try_lock(self, session_id: uuid.UUID) -> bool:
        """이 연결이 세션의 처리 잠금을 쥔다. 이미 다른 연결이 쥐고 있으면 기다리지 않고 False."""
        row = self._conn.execute("SELECT pg_try_advisory_lock(%s)", (self._lock_key(session_id),)).fetchone()
        return bool(row[0])

    def is_locked(self, session_id: uuid.UUID) -> bool:
        """어느 연결이든 세션의 처리 잠금을 쥐고 있는가 (잠금을 건드리지 않고 DB 의 잠금 목록만 본다)."""
        key = self._lock_key(session_id)
        row = self._conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted AND objsubid = 1 "
            "AND classid::bigint = %s AND objid::bigint = %s)",
            ((key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF),  # 64비트 키는 위·아래 32비트로 나뉘어 기록된다
        ).fetchone()
        return bool(row[0])

    # --- 처리 상태와 결과 ---

    def _update_item(self, session_id: uuid.UUID, code: str, assignments: str, params: tuple) -> None:
        cur = self._conn.execute(
            f"UPDATE session_items SET {assignments} WHERE session_id = %s AND code = %s",
            (*params, session_id, code),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"세션에 없는 종목입니다: {code}")

    def mark_running(self, session_id: uuid.UUID, code: str) -> None:
        self._update_item(session_id, code, "state = 'running', error_message = ''", ())

    def mark_done(
        self,
        session_id: uuid.UUID,
        code: str,
        *,
        decision: str,
        score: float | None,
        agents_rated: int,
        agents_total: int,
        report_id: str = "",
        analyzed_at: datetime | None = None,
        elapsed_seconds: float | None = None,
    ) -> None:
        """분석이 끝난 결과를 적는다. 이전 실패 사유는 지운다. 걸린 시간을 주지 않으면 이전 값을 둔다."""
        self._update_item(
            session_id,
            code,
            "state = 'done', error_message = '', decision = %s, score = %s, agents_rated = %s, "
            "agents_total = %s, report_id = %s, analyzed_at = COALESCE(%s, now()), "
            "elapsed_seconds = COALESCE(%s, elapsed_seconds)",
            (decision, score, agents_rated, agents_total, report_id, analyzed_at, elapsed_seconds),
        )

    def mark_failed(
        self,
        session_id: uuid.UUID,
        code: str,
        error_message: str,
        analyzed_at: datetime | None = None,
    ) -> None:
        """실패로 표시하고 사유를 남긴다. 이미 저장된 등급·점수는 건드리지 않는다."""
        self._update_item(
            session_id,
            code,
            "state = 'failed', error_message = %s, analyzed_at = COALESCE(%s, now())",
            (error_message, analyzed_at),
        )

    # --- 정리 (순위) ---

    def organize(self, session_id: uuid.UUID) -> Ranking:
        """끝난 종목에 점수 높은 순으로 순위를 매기고 정리 시각을 적는다. 점수가 같으면 순번이 앞선 종목이 위다.

        순위는 1 부터 빠짐없이 이어진다 (동점 묶기 없음). 실패한 종목과, 끝났지만 점수가 없는 종목(에이전트 등급을 하나도 못 읽음)은
        비교에서 빼고 순위를 비운다 — 점수를 0 으로 지어내지 않는다. 이전 순위는 모두 지우고 새로 매긴다.
        한 트랜잭션이라 중간에 실패하면 이전 순위와 정리 시각이 그대로 남는다.
        """
        with self._conn.transaction():
            if self._conn.execute("SELECT 1 FROM sessions WHERE id = %s", (session_id,)).fetchone() is None:
                raise NotFoundError("세션을 찾지 못했습니다.")
            self._conn.execute("UPDATE session_items SET rank = NULL WHERE session_id = %s", (session_id,))
            ranked = self._conn.execute(
                "UPDATE session_items i SET rank = r.rn FROM ("
                " SELECT seq, row_number() OVER (ORDER BY score DESC, seq ASC) AS rn FROM session_items"
                " WHERE session_id = %s AND NOT archived AND state = 'done' AND score IS NOT NULL) r"
                " WHERE i.session_id = %s AND i.seq = r.seq",
                (session_id, session_id),
            ).rowcount
            unscored = self._conn.execute(
                "SELECT count(*) FROM session_items "
                "WHERE session_id = %s AND NOT archived AND state = 'done' AND score IS NULL",
                (session_id,),
            ).fetchone()[0]
            self._conn.execute("UPDATE sessions SET ranked_at = now() WHERE id = %s", (session_id,))
        return Ranking(ranked=ranked, unscored=unscored)

    def rank_is_stale(self, session_id: uuid.UUID) -> bool:
        """정리한 뒤에 분석이 바뀌어서 지금 다시 정리하면 순위가 달라질 수 있는가. 정리한 적이 없으면 False.

        정리 시각(``ranked_at``) 뒤에 분석한 종목 중에서, 순위에 영향을 줄 수 있는 것만 센다:
        - 정리할 때 순위가 있던 종목 (다시 분석됐으니 점수가 바뀌었거나 실패해서 비교에서 빠져야 한다), 또는
        - 이제 점수와 함께 끝나서 순위에 들어와야 하는 종목.
        정리 때도 비교에서 빠져 있었고 지금도 빠져 있는 종목(실패했던 종목이 또 실패, 점수 없던 종목이 또 점수 없음)은
        다시 정리해도 순위가 같으므로 세지 않는다.
        """
        row = self._conn.execute(
            "SELECT EXISTS ("
            " SELECT 1 FROM session_items i JOIN sessions s ON s.id = i.session_id"
            " WHERE i.session_id = %s AND NOT i.archived AND s.ranked_at IS NOT NULL AND i.analyzed_at > s.ranked_at"
            " AND (i.rank IS NOT NULL OR (i.state = 'done' AND i.score IS NOT NULL)))",
            (session_id,),
        ).fetchone()
        return bool(row[0])

    # --- 에이전트별 등급 ---

    def save_agent_ratings(
        self, session_id: uuid.UUID, code: str, ratings: Mapping[str, str]
    ) -> None:
        """한 종목의 에이전트별 등급을 통째로 바꿔 넣는다. 하나라도 잘못되면 이전 값이 그대로 남는다."""
        try:
            with self._conn.transaction(), self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM item_agent_ratings WHERE session_id = %s AND code = %s",
                    (session_id, code),
                )
                cur.executemany(
                    "INSERT INTO item_agent_ratings (session_id, code, agent, rating) VALUES (%s, %s, %s, %s)",
                    [(session_id, code, agent, rating) for agent, rating in ratings.items()],
                )
        except psycopg.errors.ForeignKeyViolation:
            raise NotFoundError(f"세션에 없는 종목입니다: {code}") from None

    def agent_ratings(self, session_id: uuid.UUID, code: str) -> dict[str, str]:
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT agent, rating FROM item_agent_ratings WHERE session_id = %s AND code = %s "
                "ORDER BY agent",
                (session_id, code),
            ).fetchall()
        return {r["agent"]: r["rating"] for r in rows}
