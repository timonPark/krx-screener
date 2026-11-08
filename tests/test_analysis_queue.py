"""분석 대기열 (#12): 명령줄(``run``·``analyze``·``list``)이 쓰는 대기열을 PostgreSQL 세션 저장소 위에서 확인한다.

기준일마다 ``source = 'cli'`` 세션 하나가 대기열이다 (웹 화면이 만든 세션과 섞이지 않는다). 기존 SQLite 대기열 테스트가 고정하던 동작
(끝난 종목 재사용, 후보에서 빠진 완료 기록 보존, 재분석 실패 시 기존 등급 유지, 구간 조회 …)을 그대로 지킨다.
"""

import os
from datetime import date

import pytest
from fakes import write_state_log

from krx_screener import db
from krx_screener.analysis import DONE, FAILED, SKIPPED, TIMEOUT, AnalysisOutcome
from krx_screener.analysis_queue import AnalysisQueue, QueueEntry
from krx_screener.repository import Item, NotFoundError, SessionRepository

D = "2026-11-05"
AS_OF = date(2026, 11, 5)


def _entries(*codes):
    return [QueueEntry(c, f"종목{c}", rank, "양호") for rank, c in enumerate(codes, start=1)]


@pytest.fixture
def queue(pg_conn):
    return AnalysisQueue(pg_conn)


@pytest.fixture
def logs(tmp_path):
    return tmp_path / "state_logs"  # 분석 상태 로그 폴더 (비어 있으면 에이전트별 등급은 못 읽는다)


def record(queue, logs, outcome, as_of=D):
    queue.record(as_of, outcome, state_log_root=logs)


# --- 등록 ---


def test_alphanumeric_codes_are_waiting_like_any_other_now_that_the_server_accepts_them(queue):
    assert queue.enqueue(D, _entries("000010", "0126Z0", "00680K")) == 3
    assert {r.code: r.state for r in queue.rows(D, 10)} == {"000010": "pending", "0126Z0": "pending", "00680K": "pending"}


def test_enqueue_marks_pending_and_fails_codes_the_server_rejects_with_a_reason(queue):
    assert queue.enqueue(D, _entries("000010", "0126z0", "00680k")) == 3  # 소문자는 서버가 거절한다
    rows = {r.code: r for r in queue.rows(D, 10)}
    assert rows["000010"].state == "pending"
    for code in ("0126z0", "00680k"):
        assert rows[code].state == "failed"
        assert "영숫자 6자" in rows[code].message
        assert rows[code].to_outcome().status == SKIPPED  # 보내지 않은 종목은 건너뜀으로 보인다


def test_rows_are_ordered_by_rank_and_limited(queue):
    queue.enqueue(D, _entries("000010", "000020", "000030"))
    assert [r.code for r in queue.rows(D, 2)] == ["000010", "000020"]
    assert [r.code for r in queue.rows(D, 99)] == ["000010", "000020", "000030"]
    assert queue.rows("2026-11-04", 99) == []  # 다른 기준일은 섞이지 않는다


def test_rows_with_offset_returns_the_window(queue):
    queue.enqueue(D, _entries("000010", "000020", "000030", "000040", "000050"))
    assert [r.code for r in queue.rows(D, 2, offset=1)] == ["000020", "000030"]
    assert [r.code for r in queue.rows(D, 9, offset=3)] == ["000040", "000050"]
    assert queue.rows(D, 3, offset=5) == []
    assert [r.code for r in queue.rows(D, 2)] == ["000010", "000020"]  # offset 기본값 0


def test_enqueue_twice_is_safe_and_keeps_analysis_state(queue, logs):
    queue.enqueue(D, _entries("000010", "000020"))
    record(queue, logs, AnalysisOutcome("000010", "종목000010", DONE, decision="Buy", report_id="000010_20261105_100000", elapsed_seconds=300))
    assert queue.enqueue(D, _entries("000010", "000020")) == 0
    row = queue.rows(D, 1)[0]
    assert (row.state, row.decision, row.report_id, row.elapsed_seconds) == ("done", "Buy", "000010_20261105_100000", 300)


def test_enqueue_updates_name_rank_and_flow_of_existing_rows(queue):
    queue.enqueue(D, [QueueEntry("000010", "옛이름", 1, "약함"), QueueEntry("000020", "B", 2, "")])
    queue.enqueue(D, [QueueEntry("000020", "B", 1, "양호"), QueueEntry("000010", "새이름", 2, "부분")])  # 순번이 서로 바뀐다
    rows = {r.code: r for r in queue.rows(D, 9)}
    assert (rows["000020"].rank, rows["000020"].flow_status) == (1, "양호")
    assert (rows["000010"].name, rows["000010"].rank, rows["000010"].flow_status) == ("새이름", 2, "부분")


def test_the_flow_label_is_kept_and_an_empty_one_is_unconfirmed(queue):
    queue.enqueue(D, [QueueEntry("000010", "가", 1, "양호"), QueueEntry("000020", "나", 2, "부분"), QueueEntry("000030", "다", 3, "약함"),
                      QueueEntry("000040", "라", 4, "미확인"), QueueEntry("000050", "마", 5, "")])
    assert [r.flow_status for r in queue.rows(D, 9)] == ["양호", "부분", "약함", "미확인", "미확인"]


def test_the_listed_values_are_saved_so_the_screen_shows_them_too(queue, pg_conn):
    entry = QueueEntry("000010", "가나다전자", 1, "양호", market="KOSPI", close=64800, avg_trading_value=201_712_536_740, rsi=67.2,
                       foreign_net_5d=-624606, institution_net_5d=645940, is_caution=True, is_foreign=False)
    queue.enqueue(D, [entry])
    session = SessionRepository(pg_conn).latest_session()
    item = SessionRepository(pg_conn).items(session.id)[0]
    assert (item.market, item.close, item.avg_trading_value, item.rsi) == ("KOSPI", 64800, 201_712_536_740, 67.2)
    assert (item.foreign_net_5d, item.institution_net_5d, item.is_caution, item.flow_status) == (-624606, 645940, True, "strong")


def test_missing_values_in_a_later_listing_do_not_wipe_the_saved_ones(queue, pg_conn):
    queue.enqueue(D, [QueueEntry("000010", "가", 1, "양호", market="KOSPI", close=1000)])
    queue.enqueue(D, [QueueEntry("000010", "가", 1, "양호")])  # 값이 없는 목록
    item = SessionRepository(pg_conn).items(SessionRepository(pg_conn).latest_session().id)[0]
    assert (item.market, item.close) == ("KOSPI", 1000)


# --- 후보에서 빠진 종목 ---


def test_candidates_that_dropped_out_are_deleted_if_unfinished_and_kept_if_done(queue, logs):
    queue.enqueue(D, _entries("000010", "000020", "000030"))
    record(queue, logs, AnalysisOutcome("000010", "x", DONE, decision="Hold"))
    record(queue, logs, AnalysisOutcome("000020", "x", FAILED, message="boom"))
    queue.enqueue(D, _entries("000030"))  # 앞의 두 종목이 후보에서 빠짐
    assert [r.code for r in queue.rows(D, 99)] == ["000030"]  # 목록에서는 보이지 않는다
    # 끝난 기록은 남고(보관) 오류 기록은 사라진다
    assert queue.archived_codes(D) == ["000010"]
    # 다시 후보가 되면 순번이 돌아오고 등급도 그대로다
    queue.enqueue(D, _entries("000010", "000030"))
    again = {r.code: r for r in queue.rows(D, 99)}
    assert (again["000010"].rank, again["000010"].decision, again["000010"].state) == (1, "Hold", "done")
    assert queue.archived_codes(D) == []


def test_several_dropped_done_records_can_be_kept_together(queue, logs):
    queue.enqueue(D, _entries("000010", "000020", "000030", "000040"))
    for code in ("000010", "000020", "000030"):
        record(queue, logs, AnalysisOutcome(code, "x", DONE, decision="Hold"))
    queue.enqueue(D, _entries("000040"))
    assert queue.archived_codes(D) == ["000010", "000020", "000030"]
    queue.enqueue(D, _entries("000040", "000020"))
    assert queue.archived_codes(D) == ["000010", "000030"]  # 000020 만 후보로 돌아왔다


def test_archived_records_are_invisible_to_the_web_screen(queue, logs, pg_conn):
    queue.enqueue(D, _entries("000010", "000020", "000030"))
    record(queue, logs, AnalysisOutcome("000010", "x", DONE, decision="Hold"))
    queue.enqueue(D, _entries("000020", "000030"))
    repo = SessionRepository(pg_conn)
    session = repo.latest_session()
    assert session.total == 2  # 종목 수에 들어가지 않는다
    assert [i.code for i in repo.items(session.id)] == ["000020", "000030"]
    assert repo.counts(session.id) == {"pending": 2, "running": 0, "done": 0, "failed": 0}


def test_an_archived_done_record_is_not_ranked_by_organize(queue, logs, pg_conn):
    queue.enqueue(D, _entries("000010", "000020", "000030"))
    for code, score_decision in (("000010", "Buy"), ("000020", "Hold"), ("000030", "Hold")):
        record(queue, logs, AnalysisOutcome(code, "x", DONE, decision=score_decision))
    repo = SessionRepository(pg_conn)
    session = repo.latest_session()
    for code in ("000010", "000020", "000030"):
        repo._update_item(session.id, code, "score = %s", ({"000010": 2.0, "000020": 1.0, "000030": 0.5}[code],))
    queue.enqueue(D, _entries("000020", "000030"))  # 점수가 가장 높은 000010 이 후보에서 빠진다
    assert repo.organize(session.id).ranked == 2
    assert {i.code: i.rank for i in repo.items(session.id)} == {"000020": 1, "000030": 2}
    assert repo.rank_is_stale(session.id) is False


# --- 결과 기록 ---


def test_record_saves_a_finished_analysis_like_the_web_does(queue, logs, pg_conn):
    queue.enqueue(D, _entries("000010"))
    write_state_log(logs, "000010", AS_OF, {"market": "Buy", "news": "Overweight", "portfolio_manager": "Buy"})
    record(queue, logs, AnalysisOutcome("000010", "x", DONE, decision="Buy", report_id="000010_20261105_101500", elapsed_seconds=312.5))
    row = queue.rows(D, 1)[0]
    assert (row.state, row.decision, row.report_id, row.elapsed_seconds) == ("done", "Buy", "000010_20261105_101500", 312.5)
    repo = SessionRepository(pg_conn)
    session = repo.latest_session()
    assert repo.agent_ratings(session.id, "000010") == {"market": "Buy", "news": "Overweight", "bull": "Hold", "bear": "Hold", "fundamentals": "Hold",
                                                        "research_manager": "Hold", "aggressive": "Hold", "conservative": "Hold",
                                                        "neutral": "Hold", "portfolio_manager": "Buy"}
    item = repo.get_item(session.id, "000010")
    assert (item.agents_rated, item.agents_total) == (10, 10)
    assert item.score == pytest.approx(0.5)  # 매수 +2, 비중 확대 +1, 포트폴리오 매니저 매수 +2, 나머지 7개는 보유 0 → 5/10


def test_record_without_a_state_log_still_saves_the_grade(queue, logs, pg_conn):
    queue.enqueue(D, _entries("000010"))
    record(queue, logs, AnalysisOutcome("000010", "x", DONE, decision="Sell"))
    item = SessionRepository(pg_conn).items(SessionRepository(pg_conn).latest_session().id)[0]
    assert (item.state, item.decision, item.score, item.agents_rated, item.agents_total) == ("done", "Sell", None, 0, 10)


@pytest.mark.parametrize("status", [FAILED, TIMEOUT])
def test_record_marks_every_failure_with_its_reason(queue, logs, status):
    queue.enqueue(D, _entries("000010", "000020"))
    record(queue, logs, AnalysisOutcome("000010", "x", status, run_id="r9", elapsed_seconds=1800, message="늦음"))
    row = queue.rows(D, 1)[0]
    assert (row.state, row.message, row.decision) == ("failed", "늦음", "")


def test_a_failure_does_not_erase_an_existing_grade(queue, logs):
    queue.enqueue(D, _entries("000010"))
    record(queue, logs, AnalysisOutcome("000010", "x", DONE, decision="Buy"))
    record(queue, logs, AnalysisOutcome("000010", "x", FAILED, message="재분석 실패"))
    row = queue.rows(D, 1)[0]
    assert (row.state, row.decision, row.message) == ("failed", "Buy", "재분석 실패")  # 이미 있는 등급은 그대로다


def test_record_rejects_a_code_that_is_not_in_the_queue(queue, logs):
    queue.enqueue(D, _entries("000010"))
    with pytest.raises(NotFoundError, match="999999"):
        record(queue, logs, AnalysisOutcome("999999", "x", DONE, decision="Buy"))


def test_mark_running_changes_only_that_stock(queue):
    queue.enqueue(D, _entries("000010", "000020"))
    queue.mark_running(D, "000010")
    assert [(r.code, r.state) for r in queue.rows(D, 9)] == [("000010", "running"), ("000020", "pending")]


def test_outcomes_excludes_waiting_stocks_and_converts_an_empty_decision_to_none(queue, logs):
    queue.enqueue(D, _entries("000010", "000020", "000030", "0126z0"))
    queue.mark_running(D, "000010")
    record(queue, logs, AnalysisOutcome("000020", "x", FAILED, message="boom"))
    record(queue, logs, AnalysisOutcome("000030", "x", DONE, decision="Overweight"))
    outs = queue.all_outcomes(D)
    assert [(o.code, o.status, o.decision) for o in outs] == [
        ("000020", FAILED, None),
        ("000030", DONE, "Overweight"),
        ("0126z0", SKIPPED, None),
    ]
    assert outs[1].is_target
    assert [o.code for o in queue.outcomes(D, 2)] == ["000020"]  # 1~2번 중 끝난 것


# --- 이전 방식(analysis.csv)에서 가져오기 ---


def test_import_done_never_overwrites_finished_or_unsendable_rows(queue, logs):
    queue.enqueue(D, _entries("000010", "000020", "0126z0"))
    record(queue, logs, AnalysisOutcome("000020", "x", DONE, decision="Buy", report_id="000020_20261105_090000"))
    imported = queue.import_done(
        D,
        {
            "000010": AnalysisOutcome("000010", "x", DONE, decision="Hold", report_id="000010_20261105_080000", elapsed_seconds=300),
            "000020": AnalysisOutcome("000020", "x", DONE, decision="Sell", report_id="old"),
            "0126z0": AnalysisOutcome("0126z0", "x", DONE, decision="Buy"),
        },
    )
    assert imported == 1
    rows = {r.code: r for r in queue.rows(D, 9)}
    assert (rows["000010"].state, rows["000010"].decision, rows["000010"].elapsed_seconds) == ("done", "Hold", 300)
    assert (rows["000020"].decision, rows["000020"].report_id) == ("Buy", "000020_20261105_090000")
    assert rows["0126z0"].state == "failed"  # 서버가 받지 않는 코드는 가져오지 않는다


def test_a_failed_stock_can_be_filled_in_from_the_old_file(queue, logs):
    queue.enqueue(D, _entries("000010"))
    record(queue, logs, AnalysisOutcome("000010", "x", FAILED, message="boom"))
    assert queue.import_done(D, {"000010": AnalysisOutcome("000010", "x", DONE, decision="Hold")}) == 1
    assert queue.rows(D, 1)[0].state == "done"


# --- 소요 시간 ---


def test_done_durations_only_counts_finished_analyses_with_a_time(queue, logs):
    queue.enqueue(D, _entries("000010", "000020", "000030"))
    record(queue, logs, AnalysisOutcome("000010", "x", DONE, decision="Hold", elapsed_seconds=300))
    record(queue, logs, AnalysisOutcome("000020", "x", FAILED, elapsed_seconds=50))
    record(queue, logs, AnalysisOutcome("000030", "x", DONE, decision="Hold", elapsed_seconds=0))
    assert queue.done_durations() == [300.0]


# --- 기준일과 세션 ---


def test_latest_date_is_the_most_recent_queue_date(queue):
    assert queue.latest_date() is None
    queue.enqueue("2026-11-04", _entries("000010"))
    queue.enqueue("2026-11-05", _entries("000010"))
    assert queue.latest_date() == "2026-11-05"


def test_the_queue_is_one_cli_session_per_date_and_the_screen_can_see_it(queue, pg_conn):
    queue.enqueue(D, _entries("000010"))
    queue.enqueue(D, _entries("000010", "000020"))
    queue.enqueue("2026-11-04", _entries("000010"))
    sessions = SessionRepository(pg_conn).list_sessions()
    assert sorted((s.as_of.isoformat(), s.source, s.total) for s in sessions) == [("2026-11-04", "cli", 1), ("2026-11-05", "cli", 2)]


def test_a_web_session_of_the_same_date_is_never_touched_by_the_queue(queue, pg_conn):
    repo = SessionRepository(pg_conn)
    web = repo.create_session(AS_OF)
    repo.add_items(web.id, [Item(seq=1, code="000099", name="웹종목", market="KOSPI")])
    queue.enqueue(D, _entries("000010"))
    queue.enqueue(D, _entries("000020"))  # 대기열 쪽에서 종목이 바뀌어도
    assert [i.code for i in repo.items(web.id)] == ["000099"]
    assert [r.code for r in queue.rows(D, 9)] == ["000020"]
    assert repo.get_session(web.id).source == "web"


def test_the_state_is_in_the_database_not_in_the_connection(pg_conn):
    AnalysisQueue(pg_conn).enqueue(D, _entries("000010"))
    other = db.connect(os.environ["KRX_SCREENER_TEST_DATABASE_URL"])
    try:
        assert [r.code for r in AnalysisQueue(other).rows(D, 9)] == ["000010"]
    finally:
        other.close()


# --- 같은 세션을 동시에 처리하지 않는다 ---


def test_the_queue_session_is_locked_like_a_web_session(queue, pg_conn):
    queue.enqueue(D, _entries("000010"))
    assert queue.lock(D) is True
    session = SessionRepository(pg_conn).latest_session()
    other = db.connect(os.environ["KRX_SCREENER_TEST_DATABASE_URL"])
    try:
        other.autocommit = True
        assert AnalysisQueue(other).lock(D) is False  # 다른 연결(웹 화면의 처리나 다른 analyze)은 못 쥔다
        assert SessionRepository(other).is_locked(session.id) is True
    finally:
        other.close()
    assert queue.lock("2026-01-01") is False  # 대기열이 없는 기준일은 잠글 수 없다
