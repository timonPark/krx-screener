import csv
from pathlib import Path

import pytest
from typer.testing import CliRunner

from krx_screener import cli
from krx_screener.analysis import DONE, FAILED, AnalysisError, AnalysisOutcome
from krx_screener.results import load_done_analysis, write_analysis
from krx_screener.store import QueueStore

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
DATE = "2026-11-05"

CANDIDATES = [
    ("000010", "가나다전자", "양호"),
    ("0126Z0", "영숫자코드", "부분"),  # 서버가 거절하는 코드 형식
    ("000020", "라마바화학", "약함"),
    ("000030", "사아자증권", "미확인"),
]


class _Server:
    """가짜 TradingAgents-KR 서버의 기록과 응답 설정."""

    def __init__(self):
        self.started: list[tuple[str, str, str]] = []
        self.decisions = {"000010": "Buy", "000020": "Hold", "000030": "Overweight"}
        self.errors: set[str] = set()
        self.health_error = False


@pytest.fixture
def server(monkeypatch):
    srv = _Server()

    class FakeClient:
        def __init__(self, base_url, *args, **kwargs):
            self.base_url = base_url

        def check_health(self):
            if srv.health_error:
                raise AnalysisError("TradingAgents-KR 서버에 연결하지 못했습니다")

        def start(self, ticker, trade_date, effort):
            srv.started.append((ticker, trade_date, effort))
            return f"run-{ticker}"

        def snapshot(self, run_id):
            code = run_id.removeprefix("run-")
            if code in srv.errors:
                return {"status": "error", "error": "boom"}
            return {"status": "done", "decision": srv.decisions[code]}

    monkeypatch.setattr(cli, "AnalysisClient", FakeClient)
    return srv


def _make_results(out: Path, date: str = DATE, rows=CANDIDATES) -> Path:
    day = out / date
    day.mkdir(parents=True)
    with (day / "candidates.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["code", "name", "flow_status"])
        writer.writerows(rows)
    return day


def _analyze(tmp_path, *args, input_text=None):
    return CliRunner().invoke(
        cli.app,
        [
            "analyze", "--config", str(REPO_CONFIG), "--out", str(tmp_path / "results"),
            "--db", str(tmp_path / "q.db"), *args,
        ],
        input=input_text,
    )


def _flat(result) -> str:
    return result.output.replace("\n", "")


def test_count_is_required(tmp_path, server):
    _make_results(tmp_path / "results")
    result = _analyze(tmp_path)
    assert result.exit_code == 2
    assert server.started == []


def test_missing_results_folder(tmp_path, server):
    result = _analyze(tmp_path, "--count", "1", "--yes")
    assert result.exit_code == 1
    assert "후보 파일이 없습니다" in _flat(result)
    assert server.started == []


def test_sends_only_the_first_n_in_order_and_skips_alphanumeric(tmp_path, server):
    day = _make_results(tmp_path / "results")
    result = _analyze(tmp_path, "--count", "3", "--yes")
    assert result.exit_code == 0, result.output

    # 앞에서 3개(영숫자 코드 포함) 중 서버가 받는 2개만 순서대로 보낸다. 4번째는 보내지 않는다
    assert server.started == [("000010", DATE, "low"), ("000020", DATE, "low")]

    rows = list(csv.DictReader((day / "analysis.csv").open(encoding="utf-8-sig")))
    assert [(r["code"], r["status"], r["decision"], r["is_target"]) for r in rows] == [
        ("000010", "done", "Buy", "True"),
        ("0126Z0", "skipped", "", "False"),
        ("000020", "done", "Hold", "False"),
    ]
    assert rows[0]["decision_label"] == "매수"

    text = _flat(result)
    assert "최종 등급이 매수·비중 확대인 종목" in text
    assert "가나다전자" in text
    assert "라마바화학" not in text.split("최종 등급이")[1]  # 보유는 표에 없다
    assert "사아자증권" not in text  # 구간 밖
    assert "투자 성과를 보증하지 않습니다" in text


def test_rerun_reuses_finished_analysis_without_asking_or_sending(tmp_path, server):
    _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "3", "--yes")
    sent = list(server.started)

    again = _analyze(tmp_path, "--count", "3")  # --yes 없이도 보낼 게 없으면 묻지 않는다
    assert again.exit_code == 0, again.output
    assert server.started == sent
    assert "재사용" in _flat(again)


def test_redo_sends_again(tmp_path, server):
    _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "1", "--yes")
    _analyze(tmp_path, "--count", "1", "--yes", "--redo")
    assert server.started == [("000010", DATE, "low")] * 2


def test_declining_the_confirmation_sends_nothing(tmp_path, server):
    day = _make_results(tmp_path / "results")
    result = _analyze(tmp_path, "--count", "2", input_text="n\n")
    assert result.exit_code == 1
    assert "아무것도 보내지 않았습니다" in _flat(result)
    assert server.started == []
    assert not (day / "analysis.csv").exists()


def test_confirmation_names_quota_use(tmp_path, server):
    _make_results(tmp_path / "results")
    result = _analyze(tmp_path, "--count", "1", input_text="y\n")
    assert result.exit_code == 0, result.output
    assert "쿼터" in _flat(result)
    assert server.started == [("000010", DATE, "low")]


def test_server_down_stops_before_sending(tmp_path, server):
    day = _make_results(tmp_path / "results")
    server.health_error = True
    result = _analyze(tmp_path, "--count", "2", "--yes")
    assert result.exit_code == 1
    assert "연결하지 못했습니다" in _flat(result)
    assert server.started == []
    assert not (day / "analysis.csv").exists()


def test_one_failure_does_not_stop_the_rest_and_is_recorded(tmp_path, server):
    day = _make_results(tmp_path / "results")
    server.errors.add("000010")
    result = _analyze(tmp_path, "--count", "4", "--yes")
    assert result.exit_code == 0, result.output
    assert [s[0] for s in server.started] == ["000010", "000020", "000030"]
    rows = {r["code"]: r for r in csv.DictReader((day / "analysis.csv").open(encoding="utf-8-sig"))}
    assert rows["000010"]["status"] == "error"
    assert "boom" in rows["000010"]["message"]
    assert rows["000030"]["decision"] == "Overweight"
    # 오류 난 종목은 다음 실행에서 다시 보낸다 (완료된 것만 재사용)
    with QueueStore(tmp_path / "q.db") as store:
        states = {r.code: r.state for r in store.rows(DATE, 10)}
    assert states == {"000010": "error", "0126Z0": "skipped", "000020": "done", "000030": "done"}
    server.errors.clear()
    again = _analyze(tmp_path, "--count", "4", "--yes")
    assert again.exit_code == 0, again.output
    assert [s[0] for s in server.started][3:] == ["000010"]  # 오류 난 하나만 다시 보냄


def test_defaults_to_latest_results_folder(tmp_path, server):
    _make_results(tmp_path / "results", "2026-11-04")
    _make_results(tmp_path / "results", "2026-11-05")
    (tmp_path / "results" / "not-a-date").mkdir()
    result = _analyze(tmp_path, "--count", "1", "--yes")
    assert result.exit_code == 0, result.output
    assert server.started == [("000010", "2026-11-05", "low")]


def test_unreadable_candidates_file(tmp_path, server):
    day = tmp_path / "results" / DATE
    day.mkdir(parents=True)
    (day / "candidates.csv").write_text("name\n나나\n", encoding="utf-8")  # code 컬럼 없음
    result = _analyze(tmp_path, "--count", "1", "--yes")
    assert result.exit_code == 1
    assert "읽지 못했습니다" in _flat(result)


def test_bad_date_and_bad_config(tmp_path, server):
    _make_results(tmp_path / "results")
    assert _analyze(tmp_path, "--count", "1", "--date", "11/05").exit_code == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text("filters: {}\n", encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["analyze", "--count", "1", "--config", str(bad)])
    assert result.exit_code == 2


# --- analysis.csv 입출력 ---


def test_analysis_csv_roundtrip_only_reuses_done_with_a_decision(tmp_path):
    outcomes = [
        AnalysisOutcome("000010", "가", DONE, decision="Buy", run_id="r1", elapsed_seconds=61.26),
        AnalysisOutcome("000020", "나", FAILED, run_id="r2", message="boom"),
        AnalysisOutcome("000030", "다", DONE, decision="REVIEW", run_id="r3"),
    ]
    write_analysis(tmp_path, outcomes)
    loaded = load_done_analysis(tmp_path)
    assert set(loaded) == {"000010", "000030"}
    assert loaded["000010"].decision == "Buy"
    assert loaded["000010"].elapsed_seconds == 61.3
    assert loaded["000030"].decision == "REVIEW"
    assert loaded["000010"].is_target


def test_load_done_analysis_tolerates_missing_or_corrupt_files(tmp_path):
    assert load_done_analysis(tmp_path) == {}
    (tmp_path / "analysis.csv").write_text("garbage,only\nx\n", encoding="utf-8")
    assert load_done_analysis(tmp_path) == {}


# --- DB 대기열 동작 ---


def _db_rows(tmp_path):
    with QueueStore(tmp_path / "q.db") as store:
        return {r.code: r for r in store.rows(DATE, 100)}


def test_progress_is_written_to_the_db_as_each_stock_finishes(tmp_path, server):
    _make_results(tmp_path / "results")
    result = _analyze(tmp_path, "--count", "4", "--yes")
    assert result.exit_code == 0, result.output
    rows = _db_rows(tmp_path)
    assert rows["000010"].state == "done" and rows["000010"].decision == "Buy"
    assert rows["000010"].effort == "low" and rows["000010"].run_id == "run-000010"
    assert rows["0126Z0"].state == "skipped"
    assert rows["000030"].decision == "Overweight"
    assert [rows[c].rank for c in ("000010", "0126Z0", "000020", "000030")] == [1, 2, 3, 4]
    assert rows["000010"].flow_status == "양호"


def test_next_run_continues_with_what_is_not_finished(tmp_path, server):
    _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "1", "--yes")  # 첫 종목만 끝낸 상태
    assert [s[0] for s in server.started] == ["000010"]
    again = _analyze(tmp_path, "--count", "4", "--yes")
    assert again.exit_code == 0, again.output
    assert [s[0] for s in server.started] == ["000010", "000020", "000030"]  # 끝난 건 다시 안 보냄
    assert "재사용" in _flat(again)


def test_dry_run_lists_without_connecting_or_changing_analysis_state(tmp_path, server):
    day = _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "1", "--yes")  # 소요 시간 기록용으로 하나 끝낸다
    sent = list(server.started)
    server.health_error = True  # 서버에 연결하면 실패하도록 — dry-run 은 연결하지 않아야 한다

    result = _analyze(tmp_path, "--count", "4", "--dry-run")
    assert result.exit_code == 0, result.output
    text = _flat(result)
    assert "분석에 보낼 목록 2개" in text
    assert "라마바화학" in text and "사아자증권" in text
    assert "가나다전자" not in text.split("분석에 보낼 목록")[1]  # 끝난 종목은 목록에 없다
    assert "예상 소요" in text
    assert "쿼터를 쓰지 않았습니다" in text
    assert server.started == sent
    rows = _db_rows(tmp_path)
    assert rows["000020"].state == "pending"
    assert (day / "analysis.csv").exists()  # 이전 실행의 내보내기 그대로


def test_dry_run_without_any_finished_analysis_cannot_estimate(tmp_path, server):
    _make_results(tmp_path / "results")
    result = _analyze(tmp_path, "--count", "4", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "알 수 없음" in _flat(result)
    assert server.started == []


def test_dry_run_with_redo_lists_finished_ones_but_changes_nothing(tmp_path, server):
    _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "1", "--yes")
    result = _analyze(tmp_path, "--count", "1", "--dry-run", "--redo")
    assert "분석에 보낼 목록 1개" in _flat(result)
    assert _db_rows(tmp_path)["000010"].state == "done"


def test_failed_redo_keeps_the_existing_rating(tmp_path, server):
    day = _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "1", "--yes")
    server.errors.add("000010")  # 다시 분석하다 오류가 나는 상황
    result = _analyze(tmp_path, "--count", "1", "--yes", "--redo")
    assert result.exit_code == 0, result.output
    assert "기존 등급 유지" in _flat(result)
    row = _db_rows(tmp_path)["000010"]
    assert (row.state, row.decision) == ("done", "Buy")
    assert "Buy" in (day / "analysis.csv").read_text(encoding="utf-8-sig")


def test_successful_redo_replaces_the_rating(tmp_path, server):
    _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "1", "--yes")
    server.decisions["000010"] = "Sell"
    _analyze(tmp_path, "--count", "1", "--yes", "--redo")
    row = _db_rows(tmp_path)["000010"]
    assert (row.state, row.decision) == ("done", "Sell")


def test_confirm_decline_with_redo_leaves_finished_result_untouched(tmp_path, server):
    _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "1", "--yes")
    declined = _analyze(tmp_path, "--count", "1", "--redo", input_text="n\n")
    assert declined.exit_code == 1
    row = _db_rows(tmp_path)["000010"]
    assert (row.state, row.decision) == ("done", "Buy")


def test_legacy_analysis_csv_is_imported_once_on_first_registration(tmp_path, server):
    day = _make_results(tmp_path / "results")
    write_analysis(day, [AnalysisOutcome("000010", "가나다전자", DONE, decision="Hold", run_id="old", elapsed_seconds=300)])
    result = _analyze(tmp_path, "--count", "1", "--yes")
    assert result.exit_code == 0, result.output
    assert server.started == []  # DB 도입 전에 끝난 결과는 다시 보내지 않는다
    row = _db_rows(tmp_path)["000010"]
    assert (row.state, row.decision, row.run_id) == ("done", "Hold", "old")


def test_rerunning_the_same_candidates_does_not_reset_state(tmp_path, server):
    _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "1", "--yes")
    _analyze(tmp_path, "--count", "1", "--dry-run")  # 목록을 다시 등록해도
    assert _db_rows(tmp_path)["000010"].state == "done"


# --- --offset / --count: 목록의 원하는 구간만 ---


def test_offset_skips_the_first_positions(tmp_path, server):
    _make_results(tmp_path / "results")
    result = _analyze(tmp_path, "--offset", "2", "--count", "2", "--yes")
    assert result.exit_code == 0, result.output
    # 3~4번(라마바화학, 사아자증권)만 보내고 1번은 건드리지 않는다
    assert [s[0] for s in server.started] == ["000020", "000030"]
    assert _db_rows(tmp_path)["000010"].state == "pending"
    assert "3~4번" in _flat(result)


def test_offset_counts_positions_in_the_whole_list_including_unsendable(tmp_path, server):
    _make_results(tmp_path / "results")
    # 2번은 영숫자 코드라 보내지 않지만 순번은 차지한다
    result = _analyze(tmp_path, "--offset", "1", "--count", "1", "--yes")
    assert result.exit_code == 0, result.output
    assert server.started == []
    assert "건너뜀" in _flat(result)


def test_next_page_hint_tells_the_command_to_run_next(tmp_path, server):
    _make_results(tmp_path / "results")
    first = _analyze(tmp_path, "--offset", "0", "--count", "2", "--yes")
    assert "--offset 2 --count 2" in _flat(first)
    last = _analyze(tmp_path, "--offset", "2", "--count", "2", "--yes")
    assert "목록 끝까지 왔습니다" in _flat(last)


def test_window_reuses_finished_ones_and_sends_the_rest(tmp_path, server):
    _make_results(tmp_path / "results")
    _analyze(tmp_path, "--offset", "0", "--count", "1", "--yes")
    again = _analyze(tmp_path, "--offset", "0", "--count", "4", "--yes")
    assert again.exit_code == 0, again.output
    assert [s[0] for s in server.started] == ["000010", "000020", "000030"]  # 1번은 다시 안 보냄
    assert "재사용" in _flat(again)


def test_offset_past_the_end_sends_nothing(tmp_path, server):
    _make_results(tmp_path / "results")
    result = _analyze(tmp_path, "--offset", "10", "--count", "5", "--yes")
    assert result.exit_code == 0, result.output
    assert "종목이 없습니다" in _flat(result)
    assert server.started == []


def test_offset_dry_run_shows_only_that_window(tmp_path, server):
    _make_results(tmp_path / "results")
    result = _analyze(tmp_path, "--offset", "2", "--count", "1", "--dry-run")
    assert result.exit_code == 0, result.output
    text = _flat(result)
    assert "라마바화학" in text
    assert "가나다전자" not in text and "사아자증권" not in text
    assert server.started == []


def test_negative_offset_is_rejected(tmp_path, server):
    _make_results(tmp_path / "results")
    assert _analyze(tmp_path, "--offset", "-1", "--count", "1", "--yes").exit_code == 2
