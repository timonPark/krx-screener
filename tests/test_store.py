import pytest

from krx_screener.analysis import DONE, FAILED, SKIPPED, TIMEOUT, AnalysisOutcome
from krx_screener.store import HISTORY_RANK, PENDING, QueueEntry, QueueStore

D = "2026-11-05"


def _entries(*codes):
    return [QueueEntry(c, f"종목{c}", rank, "양호") for rank, c in enumerate(codes, start=1)]


@pytest.fixture
def store(tmp_path):
    with QueueStore(tmp_path / "sub" / "q.db") as s:  # 상위 폴더가 없어도 만든다
        yield s


def test_enqueue_marks_pending_and_skips_codes_the_server_rejects(store):
    assert store.enqueue(D, _entries("000010", "0126Z0", "00680K")) == 3
    rows = {r.code: r for r in store.rows(D, 10)}
    assert rows["000010"].state == PENDING
    assert rows["0126Z0"].state == SKIPPED
    assert rows["00680K"].state == SKIPPED
    assert "6자리" in rows["0126Z0"].message


def test_rows_are_ordered_by_rank_and_limited(store):
    store.enqueue(D, _entries("000010", "000020", "000030"))
    assert [r.code for r in store.rows(D, 2)] == ["000010", "000020"]
    assert [r.code for r in store.rows(D, 99)] == ["000010", "000020", "000030"]
    assert store.rows("2026-11-04", 99) == []  # 다른 기준일은 섞이지 않는다


def test_enqueue_twice_is_safe_and_keeps_analysis_state(store):
    store.enqueue(D, _entries("000010", "000020"))
    store.mark(D, AnalysisOutcome("000010", "종목000010", DONE, decision="Buy", run_id="r1", elapsed_seconds=300), "low")
    assert store.enqueue(D, _entries("000010", "000020")) == 0
    row = store.rows(D, 1)[0]
    assert (row.state, row.decision, row.run_id, row.effort) == (DONE, "Buy", "r1", "low")


def test_enqueue_updates_name_rank_and_flow_of_existing_rows(store):
    store.enqueue(D, [QueueEntry("000010", "옛이름", 1, "약함"), QueueEntry("000020", "B", 2, "")])
    store.enqueue(D, [QueueEntry("000020", "B", 1, "양호"), QueueEntry("000010", "새이름", 2, "부분")])
    rows = {r.code: r for r in store.rows(D, 9)}
    assert (rows["000020"].rank, rows["000020"].flow_status) == (1, "양호")
    assert (rows["000010"].name, rows["000010"].rank) == ("새이름", 2)


def test_candidates_that_dropped_out_are_deleted_if_unfinished_and_kept_if_done(store):
    store.enqueue(D, _entries("000010", "000020", "000030"))
    store.mark(D, AnalysisOutcome("000010", "x", DONE, decision="Hold"), "low")
    store.mark(D, AnalysisOutcome("000020", "x", FAILED, message="boom"), "low")
    store.enqueue(D, _entries("000030"))  # 앞의 두 종목이 후보에서 빠짐
    assert [r.code for r in store.rows(D, 99)] == ["000030"]  # 어떤 --limit 에도 안 걸린다
    kept = {r.code: r for r in store.rows(D, HISTORY_RANK)}
    assert set(kept) == {"000030", "000010"}  # 끝난 기록은 남고 오류 기록은 사라짐
    assert kept["000010"].rank == HISTORY_RANK
    # 다시 후보가 되면 순번이 돌아오고 등급도 그대로다
    store.enqueue(D, _entries("000010", "000030"))
    again = {r.code: r for r in store.rows(D, 99)}
    assert (again["000010"].rank, again["000010"].decision) == (1, "Hold")


def test_mark_records_every_outcome_and_rejects_unknown_codes(store):
    store.enqueue(D, _entries("000010", "000020"))
    store.mark(D, AnalysisOutcome("000010", "x", TIMEOUT, run_id="r9", elapsed_seconds=1800, message="늦음"), "high")
    row = store.rows(D, 1)[0]
    assert (row.state, row.effort, row.run_id, row.message) == (TIMEOUT, "high", "r9", "늦음")
    assert row.decision == ""
    with pytest.raises(ValueError, match="대기열에 없는"):
        store.mark(D, AnalysisOutcome("999999", "x", DONE, decision="Buy"), "low")


def test_outcomes_excludes_pending_and_converts_empty_decision_to_none(store):
    store.enqueue(D, _entries("000010", "000020", "000030"))
    store.mark(D, AnalysisOutcome("000020", "x", FAILED, message="boom"), "low")
    store.mark(D, AnalysisOutcome("000030", "x", DONE, decision="Overweight"), "low")
    outs = store.all_outcomes(D)
    assert [(o.code, o.status, o.decision) for o in outs] == [
        ("000020", FAILED, None),
        ("000030", DONE, "Overweight"),
    ]
    assert outs[1].is_target
    assert [o.code for o in store.outcomes(D, 2)] == ["000020"]


def test_import_done_never_overwrites_finished_or_skipped_rows(store):
    store.enqueue(D, _entries("000010", "000020", "0126Z0"))
    store.mark(D, AnalysisOutcome("000020", "x", DONE, decision="Buy", run_id="new"), "low")
    imported = store.import_done(
        D,
        {
            "000010": AnalysisOutcome("000010", "x", DONE, decision="Hold", run_id="old", elapsed_seconds=300),
            "000020": AnalysisOutcome("000020", "x", DONE, decision="Sell", run_id="old"),
            "0126Z0": AnalysisOutcome("0126Z0", "x", DONE, decision="Buy", run_id="old"),
        },
    )
    assert imported == 1
    rows = {r.code: r for r in store.rows(D, 9)}
    assert (rows["000010"].state, rows["000010"].decision) == (DONE, "Hold")
    assert (rows["000020"].decision, rows["000020"].run_id) == ("Buy", "new")
    assert rows["0126Z0"].state == SKIPPED


def test_done_durations_only_counts_finished_analyses_with_a_time(store):
    store.enqueue(D, _entries("000010", "000020", "000030"))
    store.mark(D, AnalysisOutcome("000010", "x", DONE, decision="Hold", elapsed_seconds=300), "low")
    store.mark(D, AnalysisOutcome("000020", "x", FAILED, elapsed_seconds=50), "low")
    store.mark(D, AnalysisOutcome("000030", "x", DONE, decision="Hold", elapsed_seconds=0), "low")
    assert store.done_durations() == [300.0]


def test_state_survives_reopening_the_file(tmp_path):
    path = tmp_path / "q.db"
    with QueueStore(path) as s:
        s.enqueue(D, _entries("000010"))
        s.mark(D, AnalysisOutcome("000010", "x", DONE, decision="Buy"), "low")
    with QueueStore(path) as s:
        assert s.rows(D, 1)[0].decision == "Buy"


def test_rows_with_offset_returns_the_window(store):
    store.enqueue(D, _entries("000010", "000020", "000030", "000040", "000050"))
    assert [r.code for r in store.rows(D, 2, offset=1)] == ["000020", "000030"]
    assert [r.code for r in store.rows(D, 9, offset=3)] == ["000040", "000050"]
    assert store.rows(D, 3, offset=5) == []
    assert [r.code for r in store.rows(D, 2)] == ["000010", "000020"]  # offset 기본값 0
