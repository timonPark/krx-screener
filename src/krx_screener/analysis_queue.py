"""분석 대기열 (이슈 #12): 명령줄(``run``·``analyze``·``list``)이 쓰는 대기열을 PostgreSQL 세션 저장소 위에 둔다.

``run`` 이 후보 목록을 넣고, ``analyze`` 가 아직 끝나지 않은 종목부터 보내며 끝나는 즉시 상태를 적는다. 중간에 멈춰도 다음 실행이 이어서 한다.
서버(TradingAgents-KR)가 진행 중인 작업을 되찾는 기능은 두지 않는다 — 죽은 시점에 보내던 종목 하나를 다시 보내는 정도의 손실만 감수한다.
이전에는 SQLite 파일(``krx-screener.db``)이었다 (ADR 0001).

- **기준일마다 ``source = 'cli'`` 세션 하나**가 대기열이다. 웹 화면이 만든 세션(``web``)과 섞이지 않고, 화면의 세션 목록에는 같이 보인다.
  그래서 명령줄로 분석한 결과도 점수·순위·보고서 번호를 갖는다 (웹 처리와 같은 완료 처리 ``processing.finish_item`` 을 쓴다).
- 상태는 ``pending`` / ``running`` / ``done`` / ``failed`` (영어 내부값). 이전의 ``error``·``timeout``·``skipped`` 는 ``failed`` 와 사유(메시지)로 합쳤다.
  서버가 받지 않는 형식의 종목코드(대문자 영숫자 6자가 아님, 소문자 등)는 처음부터 ``failed`` 와 "보내지 않았습니다" 사유로 넣고, 결과로 바꿀 때 ``skipped`` 로 보인다.
  영숫자 코드(``0126Z0`` 등)는 서버가 받게 되어(TradingAgents-KR #181) 다른 종목처럼 ``pending`` 이다 (#82).
- 후보 목록에서 빠진 종목: 끝난 것은 지우지 않고 **보관(archived)** 한다 (쿼터를 쓴 결과라서). 보관한 종목은 목록·개수·순위에 나오지 않고,
  다시 후보가 되면 순번과 등급이 그대로 돌아온다. 끝나지 않은 것은 지운다.
- 같은 세션을 웹 화면과 ``analyze`` 가 동시에 처리하지 않도록 세션 잠금을 같이 쓴다 (``lock``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from krx_screener import db
from krx_screener.analysis import (
    DONE,
    FAILED,
    SKIPPED,
    AnalysisOutcome,
    server_accepts,
    skipped_outcome,
)
from krx_screener.flow import STATUS_LABELS
from krx_screener.processing import finish_item
from krx_screener.repository import NotFoundError, SessionRepository

PENDING = "pending"
DONE_STATE = "done"

_CODE_BY_LABEL = {label: code for code, label in STATUS_LABELS.items()}  # 수급 상태: 한국어 표기 → 저장값
_PARK = 10_000_000  # 순번을 다시 매기는 동안 잠깐 옮겨 두는 크기 (순번은 세션 안에서 겹치면 안 된다)
_ARCHIVE_BASE = 1_000_000  # 보관한 종목의 순번 시작값. 어떤 후보 목록의 길이보다 크다


@dataclass(frozen=True)
class QueueEntry:
    """후보 한 줄. 앞의 세 개만 필수이고, 있는 값은 세션에 저장되어 웹 화면에도 보인다."""

    code: str
    name: str
    rank: int  # 1 = 거래대금이 가장 큰 후보
    flow_status: str = ""  # 수급 상태 한국어 표기 (양호·부분·약함·미확인). 비어 있으면 미확인
    market: str = ""
    close: int | None = None
    avg_trading_value: int | None = None
    rsi: float | None = None
    foreign_net_5d: int | None = None
    institution_net_5d: int | None = None
    is_caution: bool = False
    is_foreign: bool = False


@dataclass(frozen=True)
class QueueRow:
    as_of: str
    code: str
    name: str
    rank: int
    flow_status: str  # 한국어 표기
    state: str  # pending | running | done | failed
    decision: str
    report_id: str
    elapsed_seconds: float
    message: str

    def to_outcome(self) -> AnalysisOutcome:
        if self.state == DONE_STATE:
            status = DONE
        elif self.state == "failed":
            status = SKIPPED if not server_accepts(self.code) else FAILED  # 보내지 않은 종목은 건너뜀
        else:
            status = PENDING
        return AnalysisOutcome(
            code=self.code,
            name=self.name,
            status=status,
            decision=self.decision or None,
            report_id=self.report_id,
            elapsed_seconds=self.elapsed_seconds,
            message=self.message,
        )


_ROW_COLUMNS = "code, name, seq, flow_status, state, decision, report_id, elapsed_seconds, error_message"


class AnalysisQueue:
    """분석 대기열. ``conn`` 은 자동 커밋 연결이어야 한다 (종목이 끝날 때마다 바로 보이도록)."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        self._repo = SessionRepository(conn)

    @classmethod
    def connect(cls) -> AnalysisQueue:
        """환경변수의 접속 정보로 연결한다. 연결하지 못하면 한국어 ``DatabaseError``."""
        conn = db.connect()
        conn.autocommit = True
        return cls(conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> AnalysisQueue:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()  # 연결을 닫으면 쥐고 있던 세션 잠금도 풀린다

    # --- 세션 ---

    def _session_id(self, as_of: str) -> uuid.UUID | None:
        row = self._conn.execute(
            "SELECT id FROM sessions WHERE source = 'cli' AND as_of = %s", (date.fromisoformat(as_of),)
        ).fetchone()
        return row[0] if row else None

    def _require_session(self, as_of: str) -> uuid.UUID:
        session_id = self._session_id(as_of)
        if session_id is None:
            raise NotFoundError(f"분석 대기열이 없습니다: {as_of}. 먼저 `krx-screener run` 을 실행하세요.")
        return session_id

    def _ensure_session(self, as_of: str) -> uuid.UUID:
        self._conn.execute(
            "INSERT INTO sessions (id, as_of, source) VALUES (%s, %s, 'cli') ON CONFLICT (as_of) WHERE source = 'cli' DO NOTHING",
            (uuid.uuid4(), date.fromisoformat(as_of)),
        )
        return self._require_session(as_of)

    def latest_date(self) -> str | None:
        """대기열에 있는 가장 최근 기준일 (비어 있으면 None)."""
        row = self._conn.execute("SELECT max(as_of) FROM sessions WHERE source = 'cli'").fetchone()
        return row[0].isoformat() if row and row[0] else None

    def lock(self, as_of: str) -> bool:
        """이 기준일 대기열의 처리 잠금을 쥔다. 웹 화면이나 다른 ``analyze`` 가 처리 중이면 False. 연결을 닫으면 풀린다."""
        session_id = self._session_id(as_of)
        return session_id is not None and self._repo.try_lock(session_id)

    # --- 등록 ---

    def enqueue(self, as_of: str, entries: Iterable[QueueEntry]) -> int:
        """기준일의 후보 목록을 넣는다 (같은 목록을 다시 넣어도 안전). 새로 들어간 종목 수를 돌려준다.

        - 새 종목: 서버가 받는 코드면 ``pending``, 아니면 ``failed`` + "보내지 않았습니다" 사유로 넣는다.
        - 이미 있는 종목: 순번·이름·수급·목록 값만 갱신하고 분석 상태는 건드리지 않는다 (없는 값은 이전 값을 둔다).
        - 목록에서 빠진 종목: 끝난 것은 보관하고(순번만 뒤로), 끝나지 않은 것은 지운다.
        한 트랜잭션이라 중간에 실패하면 아무것도 바뀌지 않는다.
        """
        entries = list(entries)
        with self._conn.transaction():
            session_id = self._ensure_session(as_of)
            existing = dict(
                self._conn.execute("SELECT code, state FROM session_items WHERE session_id = %s", (session_id,)).fetchall()
            )
            # 순번은 세션 안에서 겹치면 안 되므로, 다시 매기기 전에 모두 멀리 옮겨 둔다
            self._conn.execute("UPDATE session_items SET seq = seq + %s WHERE session_id = %s", (_PARK, session_id))
            inserted = 0
            for e in entries:
                flow = _CODE_BY_LABEL.get(e.flow_status, "unconfirmed")
                if e.code in existing:
                    self._conn.execute(
                        "UPDATE session_items SET seq = %s, name = %s, flow_status = %s, archived = false, "
                        "market = COALESCE(NULLIF(%s, ''), market), close = COALESCE(%s, close), "
                        "avg_trading_value = COALESCE(%s, avg_trading_value), rsi = COALESCE(%s, rsi), "
                        "foreign_net_5d = COALESCE(%s, foreign_net_5d), institution_net_5d = COALESCE(%s, institution_net_5d), "
                        "is_caution = %s, is_foreign = %s WHERE session_id = %s AND code = %s",
                        (e.rank, e.name, flow, e.market, e.close, e.avg_trading_value, e.rsi, e.foreign_net_5d,
                         e.institution_net_5d, e.is_caution, e.is_foreign, session_id, e.code),
                    )
                    continue
                state, message = (PENDING, "") if server_accepts(e.code) else ("failed", skipped_outcome(e.code, e.name).message)
                self._conn.execute(
                    "INSERT INTO session_items (session_id, seq, code, name, market, close, avg_trading_value, rsi, flow_status, "
                    "foreign_net_5d, institution_net_5d, is_caution, is_foreign, state, error_message) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (session_id, e.rank, e.code, e.name, e.market, e.close, e.avg_trading_value, e.rsi, flow,
                     e.foreign_net_5d, e.institution_net_5d, e.is_caution, e.is_foreign, state, message),
                )
                inserted += 1

            keep = {e.code for e in entries}
            archived = 0
            for code, state in existing.items():
                if code in keep:
                    continue
                if state == DONE_STATE:
                    self._conn.execute(
                        "UPDATE session_items SET seq = %s, archived = true WHERE session_id = %s AND code = %s",
                        (_ARCHIVE_BASE + archived, session_id, code),
                    )
                    archived += 1
                else:
                    self._conn.execute("DELETE FROM session_items WHERE session_id = %s AND code = %s", (session_id, code))
        return inserted

    # --- 조회 ---

    def rows(self, as_of: str, count: int, offset: int = 0) -> list[QueueRow]:
        """목록에서 ``offset`` 개를 건너뛴 자리부터 ``count`` 개 (순번 순서, 보관한 종목은 뺀다).

        순번은 1부터이고 건너뛴 영숫자 코드 종목도 순번을 차지한다.
        """
        session_id = self._session_id(as_of)
        if session_id is None:
            return []
        with self._conn.cursor(row_factory=dict_row) as cur:
            found = cur.execute(
                f"SELECT {_ROW_COLUMNS} FROM session_items "
                "WHERE session_id = %s AND NOT archived AND seq > %s AND seq <= %s ORDER BY seq",
                (session_id, offset, offset + count),
            ).fetchall()
        return [
            QueueRow(
                as_of=as_of, code=r["code"], name=r["name"], rank=r["seq"],
                flow_status=STATUS_LABELS.get(r["flow_status"], STATUS_LABELS["unconfirmed"]),
                state=r["state"], decision=r["decision"], report_id=r["report_id"],
                elapsed_seconds=r["elapsed_seconds"] or 0.0, message=r["error_message"],
            )
            for r in found
        ]

    def all_rows(self, as_of: str) -> list[QueueRow]:
        """기준일의 후보 전체 (보관한 종목은 뺀다)."""
        return self.rows(as_of, _ARCHIVE_BASE - 1)

    def archived_codes(self, as_of: str) -> list[str]:
        """후보에서 빠졌지만 끝난 기록이 남아 있는 종목 (종목코드 순)."""
        session_id = self._session_id(as_of)
        if session_id is None:
            return []
        rows = self._conn.execute(
            "SELECT code FROM session_items WHERE session_id = %s AND archived ORDER BY code", (session_id,)
        ).fetchall()
        return [r[0] for r in rows]

    def outcomes(self, as_of: str, limit: int) -> list[AnalysisOutcome]:
        """아직 대기가 아닌(끝났거나 실패·건너뛴) 종목의 결과를 순번 순서로."""
        return [o for o in (r.to_outcome() for r in self.rows(as_of, limit)) if o.status != PENDING]

    def all_outcomes(self, as_of: str) -> list[AnalysisOutcome]:
        """기준일의 모든 후보 중 대기가 아닌 종목의 결과 (analysis.csv 내보내기용)."""
        return self.outcomes(as_of, _ARCHIVE_BASE - 1)

    def done_durations(self) -> list[float]:
        """끝난 분석의 소요 시간(초). 예상 시간을 재는 근거로 쓴다 (웹 처리로 끝난 것도 포함)."""
        rows = self._conn.execute(
            "SELECT elapsed_seconds FROM session_items WHERE state = 'done' AND elapsed_seconds > 0"
        ).fetchall()
        return [float(r[0]) for r in rows]

    # --- 처리 상태와 결과 ---

    def mark_running(self, as_of: str, code: str) -> None:
        self._repo.mark_running(self._require_session(as_of), code)

    def record(self, as_of: str, outcome: AnalysisOutcome, *, state_log_root: Path) -> None:
        """분석 결과를 적는다. 끝났으면 웹 처리와 같게 에이전트별 등급·점수·보고서 번호까지 저장하고,
        실패면 사유를 적는다 (이미 있는 등급은 지우지 않는다)."""
        session_id = self._require_session(as_of)
        if outcome.status == DONE:
            finish_item(self._conn, self._repo, session_id, outcome.code, date.fromisoformat(as_of), outcome, state_log_root)
        else:
            self._repo.mark_failed(session_id, outcome.code, outcome.message or "분석에 실패했습니다.")

    def import_done(self, as_of: str, done: dict[str, AnalysisOutcome]) -> int:
        """이전 방식(analysis.csv)으로 끝난 결과를 가져온다. 이미 끝난 종목과 서버가 받지 않는 코드는 건드리지 않는다."""
        session_id = self._require_session(as_of)
        states = dict(
            self._conn.execute(
                "SELECT code, state FROM session_items WHERE session_id = %s AND NOT archived", (session_id,)
            ).fetchall()
        )
        imported = 0
        for code, o in done.items():
            if code not in states or states[code] == DONE_STATE or not server_accepts(code):
                continue
            self._repo.mark_done(
                session_id, code, decision=o.decision or "", score=None, agents_rated=0, agents_total=0,
                report_id=o.report_id, elapsed_seconds=o.elapsed_seconds or None,
            )
            imported += 1
        return imported
