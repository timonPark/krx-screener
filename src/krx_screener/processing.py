"""구간 처리 (이슈 #17): 세션의 한 구간을 TradingAgents-KR 로 한 종목씩 분석하고 결과를 저장한다.

``process_codes`` 가 뒤에서 돌아가는 본체다. 웹 API 는 요청을 받으면 바로 응답하고 이 함수를 백그라운드로 돌린다.

- 종목은 넘어온 순서대로 **한 개씩** 보낸다 (쿼터가 한꺼번에 소진되지 않게).
- 종목이 시작되면 ``running``, 끝나면 ``done``(등급 저장) 또는 ``failed``(사유 저장) 로 **즉시** 커밋해서, 조회가 진행 상황을
  볼 수 있게 한다 (연결을 자동 커밋으로 쓴다).
- 한 종목의 실패나 예상하지 못한 오류가 나머지 종목을 멈추게 하지 않는다.
- 분석 설정(``effort``)은 설정의 값(기본 ``low``)을 쓴다. 저장하지 않는다.
- 세션 단위 잠금(#18)은 요청을 받은 쪽이 쥔 연결로 넘어온다. 이 함수는 그 연결로 일하고, 끝나면 연결을 닫아서 잠금을 푼다
  (프로세스가 죽어도 연결이 끊겨 DB 가 풀어 준다).

보고서 번호(#20)와 에이전트별 등급(#21)은 분석이 끝난 종목에 저장한다 (읽지 못해도 최종 등급은 저장한다). 평가점수와 에이전트 수(#22)는
이후 이슈가 이 흐름에 더한다. 지금은 점수를 비워 둔다.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from krx_screener.agent_ratings import read_agent_ratings
from krx_screener.analysis import DONE, run_one
from krx_screener.repository import NotFoundError, SessionRepository

logger = logging.getLogger(__name__)


def process_codes(deps, conn, session_id: uuid.UUID, as_of: date, codes: list[str]) -> None:
    """``codes`` 를 순서대로 분석한다. ``deps`` 는 ``api.Dependencies`` 다.

    ``conn`` 은 세션의 처리 잠금을 쥔 자동 커밋 연결이다 (종목마다 바로 커밋 — 조회가 진행 상황을 볼 수 있다).
    끝나면 이 함수가 연결을 닫아 잠금을 푼다.
    """
    cfg = deps.config.analysis
    with conn:
        repo = SessionRepository(conn)
        for code in codes:
            try:
                item = repo.get_item(session_id, code)
                if item is None:
                    continue  # 처리 도중에 세션이 지워진 경우
                repo.mark_running(session_id, code)
                outcome = run_one(
                    deps.externals.analysis,
                    code,
                    item.name,
                    as_of.isoformat(),
                    cfg.effort,
                    cfg.timeout_seconds,
                    cfg.poll_interval_seconds,
                )
                if outcome.status == DONE:
                    # 분석이 끝난 직후에 읽는다: 같은 종목·기준일을 다시 분석하면 로그 파일이 덮어써진다.
                    # 못 읽은 에이전트는 빈다. 읽지 못해도 분석 결과(등급)는 저장한다.
                    ratings = read_agent_ratings(deps.externals.state_log_root, code, as_of)
                    with conn.transaction():  # 완료 표시와 에이전트별 등급이 함께 반영되거나 함께 안 된다
                        repo.mark_done(
                            session_id,
                            code,
                            decision=outcome.decision or "",
                            score=None,  # 평가점수는 #22 가 채운다
                            agents_rated=0,
                            agents_total=0,
                            report_id=outcome.report_id,  # 서버의 실행 번호(run_id)는 저장하지 않는다
                        )
                        repo.save_agent_ratings(session_id, code, ratings)  # 옛 등급 행은 새 값으로 통째로 바뀐다
                else:
                    repo.mark_failed(session_id, code, outcome.message or "분석에 실패했습니다.")
            except NotFoundError:
                return  # 세션이 사라졌다. 더 할 일이 없다
            except Exception as exc:  # noqa: BLE001 — 한 종목의 예상하지 못한 오류가 나머지를 멈추게 하면 안 된다
                logger.exception("종목 처리 중 오류 (%s)", code)
                try:
                    repo.mark_failed(session_id, code, f"처리 중 오류가 났습니다 ({type(exc).__name__}).")
                except Exception:  # noqa: BLE001
                    logger.exception("실패 표시도 하지 못했습니다 (%s)", code)
