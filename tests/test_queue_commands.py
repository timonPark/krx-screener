# ruff: noqa: F811  (픽스처를 다른 테스트 파일에서 가져와 매개변수로 받는다)
"""명령줄 분석 대기열이 PostgreSQL 위에서 하는 일 (#12): 진행 표시·잠금·에이전트별 등급·웹 세션과의 관계·`list`·장애 안내.

가짜 분석 서버와 후보 파일은 ``test_analyze_command`` 의 것을 쓴다. 명령은 환경변수의 접속 정보로 자기 연결을 열고,
테스트는 ``queue_db`` 연결로 결과를 본다.
"""

import csv
import os
from datetime import date
from pathlib import Path

import pytest
from fakes import write_state_log
from test_analyze_command import (  # noqa: F401  (server 는 픽스처)
    CANDIDATES,
    DATE,
    REPO_CONFIG,
    _analyze,
    _flat,
    _make_results,
    server,
)
from test_results_and_cli import _run, fake_world  # noqa: F401  (fake_world: 시세·목록·수급을 가짜로 바꾸는 픽스처)
from typer.testing import CliRunner

from krx_screener import cli, db
from krx_screener.analysis_queue import AnalysisQueue
from krx_screener.repository import SessionRepository

AS_OF = date(2026, 11, 5)


def _list(tmp_path, *args):
    return CliRunner().invoke(cli.app, ["list", *args])


def _rows(queue_db):
    return {r.code: r for r in AnalysisQueue(queue_db).rows(DATE, 100)}


# --- 진행 상태가 바로 보인다 ---


def test_the_stock_being_analyzed_is_running_and_earlier_ones_are_already_done(tmp_path, server, queue_db):
    _make_results(tmp_path / "results")
    seen = []
    server.on_snapshot = lambda code: seen.append((code, {c: r.state for c, r in _rows(queue_db).items()}))
    assert _analyze(tmp_path, "--count", "4", "--yes").exit_code == 0
    first, second = seen[0], seen[1]
    assert first[0] == "000010" and first[1]["000010"] == "running" and first[1]["000020"] == "pending"
    assert second[1]["000010"] == "done" and second[1]["000020"] == "running"  # 앞 종목의 완료가 바로 보인다 (웹 화면과 같은 저장소)


def test_a_stock_left_running_by_an_interrupted_run_is_sent_again(tmp_path, server, queue_db):
    _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "1", "--dry-run")  # 목록만 등록
    AnalysisQueue(queue_db).mark_running(DATE, "000010")  # 처리하다 끊긴 상태
    assert _analyze(tmp_path, "--count", "1", "--yes").exit_code == 0
    assert [s[0] for s in server.started] == ["000010"]
    assert _rows(queue_db)["000010"].state == "done"


def test_ctrl_c_stops_cleanly_and_keeps_what_was_finished(tmp_path, server, queue_db):
    _make_results(tmp_path / "results")

    def interrupt(code):
        if code == "000020":
            raise KeyboardInterrupt

    server.on_snapshot = interrupt
    result = _analyze(tmp_path, "--count", "4", "--yes")
    assert result.exit_code == 130
    assert "이어서 합니다" in _flat(result)
    rows = _rows(queue_db)
    assert rows["000010"].state == "done"
    assert rows["000020"].state == "running"  # 다음 실행이 다시 보낸다
    server.on_snapshot = None
    assert _analyze(tmp_path, "--count", "4", "--yes").exit_code == 0
    assert [s[0] for s in server.started] == ["000010", "000020", "000020", "000030"]


# --- 같은 목록을 동시에 처리하지 않는다 ---


def test_analyze_refuses_to_start_while_the_same_list_is_being_processed(tmp_path, server, queue_db):
    _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "1", "--dry-run")
    assert AnalysisQueue(queue_db).lock(DATE)  # 웹 화면(또는 다른 analyze)이 처리 중이라고 치자
    result = _analyze(tmp_path, "--count", "4", "--yes")
    assert result.exit_code == 1
    assert "처리 중" in _flat(result) and "아무것도 보내지 않았습니다" in _flat(result)
    assert server.started == []


def test_the_lock_does_not_get_in_the_way_when_nothing_needs_sending(tmp_path, server, queue_db):
    _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "1", "--yes")
    AnalysisQueue(queue_db).lock(DATE)
    again = _analyze(tmp_path, "--count", "1", "--yes")  # 끝난 종목만 있어서 보낼 것이 없다
    assert again.exit_code == 0 and "재사용" in _flat(again)


def test_analyze_leaves_no_lock_behind(tmp_path, server, queue_db):
    _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "2", "--yes")
    assert AnalysisQueue(queue_db).lock(DATE)  # 명령이 끝나면 연결이 닫혀 잠금이 풀렸다


# --- 웹 처리와 같은 결과 저장 ---


def test_a_finished_stock_gets_its_agent_ratings_and_score_like_in_the_web_flow(tmp_path, server, queue_db):
    _make_results(tmp_path / "results")
    root = Path(os.environ["TRADINGAGENTS_RESULTS_DIR"])
    write_state_log(root, "000010", AS_OF, {"market": "Buy", "news": "Overweight", "portfolio_manager": "Buy"})
    assert _analyze(tmp_path, "--count", "1", "--yes").exit_code == 0
    repo = SessionRepository(queue_db)
    session = next(s for s in repo.list_sessions() if s.source == "cli")
    item = repo.get_item(session.id, "000010")
    assert (item.decision, item.agents_rated, item.agents_total) == ("Buy", 10, 10)
    assert item.score == pytest.approx(0.5)
    assert repo.agent_ratings(session.id, "000010")["market"] == "Buy"


def test_without_a_state_log_the_grade_is_saved_and_the_score_is_empty(tmp_path, server, queue_db):
    _make_results(tmp_path / "results")
    assert _analyze(tmp_path, "--count", "1", "--yes").exit_code == 0
    repo = SessionRepository(queue_db)
    item = repo.get_item(next(s for s in repo.list_sessions() if s.source == "cli").id, "000010")
    assert (item.decision, item.score, item.agents_rated) == ("Buy", None, 0)


def test_the_time_an_analysis_took_is_saved_for_the_estimate(tmp_path, server, queue_db):
    _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "1", "--yes")
    assert _rows(queue_db)["000010"].elapsed_seconds >= 0
    assert len(AnalysisQueue(queue_db).done_durations()) <= 1  # 가짜 서버는 즉시 끝나서 0초일 수 있다


# --- 웹 화면의 세션으로도 보인다 ---


def _full_candidates(path: Path):
    path.mkdir(parents=True)
    with (path / "candidates.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["code", "name", "market", "close", "avg_trading_value_approx", "sma20", "sma60", "rsi", "is_caution",
                    "is_foreign", "foreign_net_5d", "institution_net_5d", "flow_status"])
        w.writerow(["000010", "가나다전자", "KOSPI", 64800, 201712536740.0, 54395.0, 50547.5, 67.2, True, False, -624606, 645940, "부분"])
        w.writerow(["000020", "라마바화학", "KOSDAQ", 23250, 24033763926.0, 1.0, 1.0, 51.6, False, True, "", "", ""])


def test_the_queue_is_a_cli_session_with_the_candidate_values_the_screen_can_show(tmp_path, server, queue_db):
    _full_candidates(tmp_path / "results" / DATE)
    assert _analyze(tmp_path, "--count", "2", "--dry-run").exit_code == 0
    repo = SessionRepository(queue_db)
    sessions = repo.list_sessions()
    assert [(s.as_of.isoformat(), s.source, s.total) for s in sessions] == [(DATE, "cli", 2)]
    first, second = repo.items(sessions[0].id)
    assert (first.market, first.close, first.avg_trading_value, first.rsi) == ("KOSPI", 64800, 201712536740, 67.2)
    assert (first.foreign_net_5d, first.institution_net_5d, first.flow_status, first.is_caution) == (-624606, 645940, "partial", True)
    assert (second.market, second.foreign_net_5d, second.flow_status, second.is_foreign) == ("KOSDAQ", None, "unconfirmed", True)  # 빈 값은 비어 있다


def test_a_web_session_of_the_same_date_is_left_alone(tmp_path, server, queue_db):
    repo = SessionRepository(queue_db)
    web = repo.create_session(AS_OF)
    _make_results(tmp_path / "results")
    assert _analyze(tmp_path, "--count", "2", "--yes").exit_code == 0
    assert repo.items(web.id) == []
    assert repo.get_session(web.id).source == "web"


def test_run_again_with_fewer_candidates_keeps_the_finished_result_out_of_the_list_but_in_the_db(tmp_path, server, queue_db):
    day = _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "1", "--yes")  # 000010 이 끝난다
    with (day / "candidates.csv").open("w", encoding="utf-8-sig", newline="") as fh:  # 다시 뽑았더니 000010 이 후보에서 빠졌다
        w = csv.writer(fh)
        w.writerow(["code", "name", "flow_status"])
        w.writerows(CANDIDATES[2:])
    _analyze(tmp_path, "--count", "5", "--dry-run")
    assert "000010" not in _rows(queue_db)  # 목록에는 나오지 않는다
    assert AnalysisQueue(queue_db).archived_codes(DATE) == ["000010"]  # 쿼터를 쓴 결과는 남아 있다
    with (day / "candidates.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["code", "name", "flow_status"])
        w.writerows(CANDIDATES)
    _analyze(tmp_path, "--count", "5", "--dry-run")
    assert (_rows(queue_db)["000010"].state, _rows(queue_db)["000010"].decision) == ("done", "Buy")  # 다시 후보가 되면 등급이 돌아온다
    assert server.started == [("000010", DATE, "low")]  # 다시 보내지 않았다


# --- list ---


def test_list_shows_every_candidate_with_its_state_and_a_summary(tmp_path, server, queue_db):
    _make_results(tmp_path / "results")
    server.errors.add("000020")
    _analyze(tmp_path, "--count", "3", "--yes")  # 1~3번: 완료 1, 영숫자 건너뜀 1, 오류 1 (4번은 구간 밖이라 대기)
    result = _list(tmp_path)
    assert result.exit_code == 0, result.output
    text = _flat(result)
    for expected in ("가나다전자", "영숫자코드", "라마바화학", "사아자증권", "기준일 2026-11-05"):
        assert expected in text
    assert "전체 4개" in text
    assert "대기 1" in text and "완료 1" in text and "실패 1" in text and "건너뜀 1" in text
    assert "진행: 완료 1 / 분석 가능 3" in text  # 영숫자 코드는 분석 가능에서 뺀다
    assert "매수" in text  # 등급 분포


@pytest.mark.parametrize(
    ("state", "shown", "hidden"),
    [
        ("pending", "사아자증권", "가나다전자"),
        ("done", "가나다전자", "사아자증권"),
        ("failed", "라마바화학", "영숫자코드"),  # 보내지 않은 영숫자 코드는 실패가 아니라 건너뜀이다
        ("skipped", "영숫자코드", "라마바화학"),
    ],
)
def test_list_can_show_only_one_state(tmp_path, server, queue_db, state, shown, hidden):
    _make_results(tmp_path / "results")
    server.errors.add("000020")
    _analyze(tmp_path, "--count", "3", "--yes")
    text = _flat(_list(tmp_path, "--state", state))
    assert shown in text and hidden not in text.split("전체")[0]
    assert "요약은 전체 기준입니다" in text


def test_list_window_and_unknown_state(tmp_path, server, queue_db):
    _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "1", "--dry-run")
    windowed = _flat(_list(tmp_path, "--offset", "1", "--count", "1"))
    assert "영숫자코드" in windowed and "가나다전자" not in windowed.split("전체")[0]
    bad = _list(tmp_path, "--state", "error")  # 이전 상태 이름 (오류·시간 초과는 실패로 합쳤다)
    assert bad.exit_code == 2 and "pending" in bad.output and "failed" in bad.output


def test_list_with_nothing_registered_says_so(tmp_path, queue_db):
    result = _list(tmp_path)
    assert result.exit_code == 1 and "대기열이 비어 있습니다" in _flat(result)
    assert _list(tmp_path, "--date", "2026-11-05").exit_code == 1  # 그 기준일의 목록이 없다


def test_list_defaults_to_the_latest_date_and_can_pick_another(tmp_path, server, queue_db):
    _make_results(tmp_path / "results", "2026-11-04")
    _make_results(tmp_path / "results", "2026-11-05")
    _analyze(tmp_path, "--date", "2026-11-04", "--count", "1", "--dry-run")
    _analyze(tmp_path, "--date", "2026-11-05", "--count", "1", "--dry-run")
    assert "기준일 2026-11-05" in _flat(_list(tmp_path))
    assert "기준일 2026-11-04" in _flat(_list(tmp_path, "--date", "2026-11-04"))


def test_list_does_not_change_anything(tmp_path, server, queue_db):
    _make_results(tmp_path / "results")
    _analyze(tmp_path, "--count", "1", "--dry-run")
    before = {c: r.state for c, r in _rows(queue_db).items()}
    _list(tmp_path)
    assert {c: r.state for c, r in _rows(queue_db).items()} == before


# --- DB 가 준비되지 않았을 때 ---


@pytest.fixture
def no_database(monkeypatch):
    monkeypatch.setenv(db.ENV_URL, "postgresql://krx:krx@127.0.0.1:1/krx_screener")  # 아무것도 듣지 않는 포트


def test_analyze_and_list_without_a_database_are_korean_errors(tmp_path, server, no_database):
    _make_results(tmp_path / "results")
    for result in (_analyze(tmp_path, "--count", "1", "--yes"), _list(tmp_path)):
        assert result.exit_code == 1
        assert "PostgreSQL 에 연결하지 못했습니다" in _flat(result)
    assert server.started == []


def test_analyze_without_the_connection_setting_tells_what_to_set(tmp_path, server, monkeypatch):
    monkeypatch.delenv(db.ENV_URL, raising=False)
    _make_results(tmp_path / "results")
    result = _analyze(tmp_path, "--count", "1", "--yes")
    assert result.exit_code == 1 and db.ENV_URL in _flat(result)


def test_with_an_old_schema_the_commands_tell_to_run_init(tmp_path, server, queue_db):
    queue_db.execute("DELETE FROM schema_migrations WHERE version = 2")
    queue_db.execute("DROP INDEX sessions_cli_as_of_idx")
    queue_db.execute("ALTER TABLE sessions DROP COLUMN source")
    queue_db.execute("ALTER TABLE session_items DROP COLUMN archived, DROP COLUMN elapsed_seconds")
    _make_results(tmp_path / "results")
    for result in (_analyze(tmp_path, "--count", "1", "--yes"), _list(tmp_path)):
        assert result.exit_code == 1
        assert "db init" in _flat(result)


def test_run_without_a_database_still_saves_the_result_files_and_says_so(tmp_path, fake_world, no_database):
    result = _run(tmp_path)
    assert result.exit_code == 1
    assert "PostgreSQL 에 연결하지 못했습니다" in _flat(result)
    assert "결과 파일(candidates.csv, run.json)은 저장했지만" in _flat(result)
    assert (tmp_path / "out" / "2026-11-06" / "candidates.csv").exists()  # 오늘(가짜 11-09 월) 기준 직전 영업일
