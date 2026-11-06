"""분석 대기열: SQLite 파일 하나에 기준일별 후보와 분석 상태를 남긴다.

``run`` 이 후보 목록을 넣고, ``analyze`` 가 아직 끝나지 않은 종목부터 보내며 끝나는 즉시 상태를
적는다. 중간에 멈춰도 다음 실행이 이어서 한다. 서버(TradingAgents-KR)가 진행 중인 작업을 되찾는
기능은 두지 않는다 — 죽은 시점에 보내던 종목 하나를 다시 보내는 정도의 손실만 감수한다.

상태 값은 ``pending`` / ``done`` / ``error`` / ``timeout`` / ``skipped`` (영어 내부값).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from krx_screener.analysis import (
    DONE,
    SKIPPED,
    AnalysisOutcome,
    server_accepts,
    skipped_outcome,
)

PENDING = "pending"

# 후보에서 빠진 뒤에도 남겨 두는 완료 기록의 순번. 어떤 --limit 에도 걸리지 않는 큰 값이다.
HISTORY_RANK = 100_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis (
    as_of           TEXT    NOT NULL,
    code            TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    rank            INTEGER NOT NULL,
    flow_status     TEXT    NOT NULL DEFAULT '',
    state           TEXT    NOT NULL,
    effort          TEXT    NOT NULL DEFAULT '',
    decision        TEXT    NOT NULL DEFAULT '',
    run_id          TEXT    NOT NULL DEFAULT '',
    elapsed_seconds REAL    NOT NULL DEFAULT 0,
    message         TEXT    NOT NULL DEFAULT '',
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (as_of, code)
)
"""


@dataclass(frozen=True)
class QueueEntry:
    code: str
    name: str
    rank: int  # 1 = 거래대금이 가장 큰 후보
    flow_status: str = ""


@dataclass(frozen=True)
class QueueRow:
    as_of: str
    code: str
    name: str
    rank: int
    flow_status: str
    state: str
    effort: str
    decision: str
    run_id: str
    elapsed_seconds: float
    message: str

    def to_outcome(self) -> AnalysisOutcome:
        return AnalysisOutcome(
            code=self.code,
            name=self.name,
            status=self.state,
            decision=self.decision or None,
            run_id=self.run_id,
            elapsed_seconds=self.elapsed_seconds,
            message=self.message,
        )


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")


class QueueStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> QueueStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def enqueue(self, as_of: str, entries: Iterable[QueueEntry]) -> int:
        """기준일의 후보 목록을 넣는다 (같은 목록을 다시 넣어도 안전). 새로 들어간 종목 수를 돌려준다.

        - 새 종목: 서버가 받는 코드면 ``pending``, 아니면 ``skipped`` 로 넣는다.
        - 이미 있는 종목: 이름·순번·수급만 갱신하고 분석 상태는 건드리지 않는다.
        - 목록에서 빠진 종목: 끝난 것은 기록으로 남기고(순번만 뒤로), 끝나지 않은 것은 지운다.
        """
        entries = list(entries)
        with self._db:
            existing = {
                r["code"]: r["state"]
                for r in self._db.execute("SELECT code, state FROM analysis WHERE as_of = ?", (as_of,))
            }
            inserted = 0
            for e in entries:
                if e.code in existing:
                    self._db.execute(
                        "UPDATE analysis SET name = ?, rank = ?, flow_status = ? "
                        "WHERE as_of = ? AND code = ?",
                        (e.name, e.rank, e.flow_status, as_of, e.code),
                    )
                    continue
                if server_accepts(e.code):
                    state, message = PENDING, ""
                else:
                    state, message = SKIPPED, skipped_outcome(e.code, e.name).message
                self._db.execute(
                    "INSERT INTO analysis (as_of, code, name, rank, flow_status, state, message, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (as_of, e.code, e.name, e.rank, e.flow_status, state, message, _now()),
                )
                inserted += 1

            keep = {e.code for e in entries}
            for code, state in existing.items():
                if code in keep:
                    continue
                if state == DONE:
                    self._db.execute(
                        "UPDATE analysis SET rank = ? WHERE as_of = ? AND code = ?",
                        (HISTORY_RANK, as_of, code),
                    )
                else:
                    self._db.execute(
                        "DELETE FROM analysis WHERE as_of = ? AND code = ?", (as_of, code)
                    )
        return inserted

    def rows(self, as_of: str, count: int, offset: int = 0) -> list[QueueRow]:
        """목록에서 ``offset`` 개를 건너뛴 자리부터 ``count`` 개 (순번 순서).

        순번은 1부터이고 건너뛴 영숫자 코드 종목도 순번을 차지한다.
        """
        cur = self._db.execute(
            "SELECT * FROM analysis WHERE as_of = ? AND rank > ? AND rank <= ? ORDER BY rank",
            (as_of, offset, offset + count),
        )
        return [_row(r) for r in cur]

    def latest_date(self) -> str | None:
        """대기열에 있는 가장 최근 기준일 (비어 있으면 None)."""
        row = self._db.execute("SELECT MAX(as_of) AS d FROM analysis").fetchone()
        return row["d"] if row and row["d"] else None

    def mark(self, as_of: str, outcome: AnalysisOutcome, effort: str) -> None:
        """분석 결과를 적는다."""
        with self._db:
            cur = self._db.execute(
                "UPDATE analysis SET state = ?, effort = ?, decision = ?, run_id = ?, "
                "elapsed_seconds = ?, message = ?, updated_at = ? WHERE as_of = ? AND code = ?",
                (
                    outcome.status,
                    effort,
                    outcome.decision or "",
                    outcome.run_id,
                    outcome.elapsed_seconds,
                    outcome.message,
                    _now(),
                    as_of,
                    outcome.code,
                ),
            )
        if cur.rowcount == 0:
            raise ValueError(f"대기열에 없는 종목입니다: {as_of} {outcome.code}")

    def import_done(self, as_of: str, done: dict[str, AnalysisOutcome]) -> int:
        """이전 방식(analysis.csv)으로 끝난 결과를 가져온다. 이미 끝난 종목은 덮어쓰지 않는다."""
        imported = 0
        with self._db:
            for code, o in done.items():
                cur = self._db.execute(
                    "UPDATE analysis SET state = ?, decision = ?, run_id = ?, elapsed_seconds = ?, "
                    "message = ?, updated_at = ? "
                    "WHERE as_of = ? AND code = ? AND state NOT IN (?, ?)",
                    (DONE, o.decision or "", o.run_id, o.elapsed_seconds, o.message, _now(),
                     as_of, code, DONE, SKIPPED),
                )
                imported += cur.rowcount
        return imported

    def outcomes(self, as_of: str, limit: int) -> list[AnalysisOutcome]:
        """아직 대기가 아닌(끝났거나 건너뛴) 종목의 결과를 순번 순서로."""
        return [r.to_outcome() for r in self.rows(as_of, limit) if r.state != PENDING]

    def all_outcomes(self, as_of: str) -> list[AnalysisOutcome]:
        """기준일의 모든 후보 중 대기가 아닌 종목의 결과 (analysis.csv 내보내기용)."""
        return self.outcomes(as_of, HISTORY_RANK - 1)

    def done_durations(self) -> list[float]:
        """끝난 분석의 소요 시간(초). 예상 시간을 재는 근거로 쓴다."""
        cur = self._db.execute(
            "SELECT elapsed_seconds FROM analysis WHERE state = ? AND elapsed_seconds > 0", (DONE,)
        )
        return [r["elapsed_seconds"] for r in cur]


def _row(r: sqlite3.Row) -> QueueRow:
    return QueueRow(
        as_of=r["as_of"],
        code=r["code"],
        name=r["name"],
        rank=r["rank"],
        flow_status=r["flow_status"],
        state=r["state"],
        effort=r["effort"],
        decision=r["decision"],
        run_id=r["run_id"],
        elapsed_seconds=r["elapsed_seconds"],
        message=r["message"],
    )
