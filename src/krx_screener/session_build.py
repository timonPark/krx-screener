"""목록을 뒤에서 만든다 (이슈 #56): 세션을 먼저 만들어 응답하고, 이 함수가 시세·수급·업종을 조회해 종목을 채운다.

- 웹 요청이 만든 연결(``conn``)이 **만드는 동안 세션의 처리 잠금을 쥔다**. 연결을 닫으면(끝나거나 프로세스가 죽으면) 잠금이 풀린다.
  그래서 "만드는 중인데 잠금이 없는 세션" 은 서버가 중간에 죽은 것(중단됨)으로 알 수 있다.
- 진행(단계·한 개수·전체 개수)은 DB 에 적는다. 새로고침하거나 다른 탭에서 열어도 보인다. 종목마다 적으면 많아서
  ``progress_interval`` 초에 한 번만 적는다 (단계가 바뀔 때와 한 단계가 끝날 때는 언제나 적는다).
- 종목은 다 만든 뒤에 한 트랜잭션으로 저장하고 완성(ready)으로 바꾼다. 실패하면 실패(failed)와 한국어 사유를 남긴다.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date

from krx_screener.listing import build_items
from krx_screener.repository import SessionRepository
from krx_screener.universe import UniverseFetchError

logger = logging.getLogger(__name__)


class _ProgressWriter:
    """진행을 DB 에 적는다. ``build_items`` 의 진행 콜백으로 쓴다 (부른 스레드 하나에서만 불린다)."""

    def __init__(self, repo: SessionRepository, session_id: uuid.UUID, interval: float) -> None:
        self._repo = repo
        self._session_id = session_id
        self._interval = interval
        self._stage = ""
        self._last = float("-inf")

    def __call__(self, stage: str, done: int, total: int) -> None:
        now = time.monotonic()
        stage_changed = stage != self._stage
        stage_finished = total > 0 and done >= total
        if stage_changed or stage_finished or now - self._last >= self._interval:
            self._repo.set_build_progress(self._session_id, stage, done, total)
            self._stage, self._last = stage, now


def failure_message(exc: Exception) -> str:
    """사용자에게 보일 실패 사유. 목록을 받지 못한 경우는 이미 한국어라 그대로 쓰고, 그 밖의 오류는 종류만 알린다."""
    if isinstance(exc, UniverseFetchError):
        return str(exc)
    return f"목록을 만드는 중 오류가 났습니다 ({type(exc).__name__})."


def build_session(deps, conn, session_id: uuid.UUID, as_of: date) -> None:
    """``session_id`` 세션의 종목을 만들어 채운다. ``conn`` 은 세션의 잠금을 쥔 자동 커밋 연결이고, 끝나면 이 함수가 닫는다."""
    with conn:
        repo = SessionRepository(conn)
        try:
            items, universe_stats = build_items(
                deps.externals,
                deps.config,
                as_of,
                deps.today(),
                workers=deps.workers,
                cache_dir=deps.cache_dir,
                retry_wait=deps.retry_wait,
                progress=_ProgressWriter(repo, session_id, deps.progress_interval),
            )
            repo.finish_build(session_id, items, universe_stats)
        except Exception as exc:  # noqa: BLE001 — 어떤 오류든 세션을 실패로 남겨야 화면이 알 수 있다
            logger.exception("목록 만들기 실패 (%s)", as_of)
            try:
                repo.fail_build(session_id, failure_message(exc))
            except Exception:  # noqa: BLE001
                logger.exception("실패 표시도 하지 못했습니다 (%s)", session_id)
