"""서버에서 이미 도는 분석은 새로 보내지 않고 이어서 기다린다 (#70).

서버에는 취소 API 가 없고, 같은 종목을 다시 보내도 거절하지 않고 새 분석을 하나 더 시작해서 쿼터를 한 번 더 쓴다
(2026-11-07 실제 서버로 확인). 그래서 시간 초과로 기다리기를 멈춘 분석이 서버에서 계속 도는 중에 다시 처리해도 새로 보내지 않는다.
"""

import logging

import pytest
import requests
from fakes import FakeAnalysisServer
from test_analysis import FakeClient, FakeSession, _Resp, _run  # 같은 가짜들을 쓴다
from test_process_api import post, post_failed, seed, states, swap_server
from test_process_failures import use_timeout

from krx_screener.analysis import DONE, AnalysisClient

# --- run_one: 이미 도는 분석이 있으면 새로 보내지 않는다 ---


def test_an_analysis_already_running_on_the_server_is_waited_for_instead_of_sending_a_new_one():
    client = FakeClient([{"status": "running"}, {"status": "done", "decision": "Buy"}], running="run-old")
    outcome = _run(client)
    assert client.started == []  # 새 분석을 시작하지 않았다 (쿼터를 더 쓰지 않는다)
    assert (outcome.status, outcome.decision, outcome.run_id) == (DONE, "Buy", "run-old")
    assert outcome.report_id  # 이어서 기다린 분석도 보고서 번호를 읽는다


def test_a_new_analysis_is_sent_when_nothing_is_running_on_the_server():
    client = FakeClient([{"status": "done", "decision": "Hold"}], running=None)
    outcome = _run(client)
    assert client.started == [("005930", "2026-11-05", "low")]
    assert (outcome.status, outcome.run_id) == (DONE, "run-1")


def test_waiting_for_a_running_analysis_also_times_out_without_sending_anything(caplog):
    client = FakeClient([{"status": "running"}] * 50, running="run-old")
    with caplog.at_level(logging.INFO):
        outcome = _run(client, timeout=20, poll=5)
    assert outcome.status == "timeout" and outcome.run_id == "run-old"
    assert client.started == []  # 다시 처리해도 또 이어서 기다릴 뿐 쿼터를 더 쓰지 않는다
    assert "이어서 기다립니다" in caplog.text


# --- AnalysisClient.find_running: 서버의 분석 목록을 읽는다 ---


def _run_row(ticker="005930", date="2026-11-05", status="running", run_id="r1", created="2026-11-07T01:00:00+00:00"):
    return {"run_id": run_id, "ticker": ticker, "trade_date": date, "status": status, "created_at": created, "reports": {}}


def _client(*responses):
    session = FakeSession(responses)
    return AnalysisClient("http://127.0.0.1:8000/", session=session), session


def test_the_server_run_list_is_asked_with_a_limit_and_a_running_run_of_that_stock_is_found():
    client, session = _client(_Resp(200, [_run_row(run_id="mine")]))
    assert client.find_running("005930", "2026-11-05") == "mine"
    assert [(method, url) for method, url, _ in session.calls] == [("GET", "http://127.0.0.1:8000/api/runs?limit=20")]


@pytest.mark.parametrize("status", ["pending", "running"])
def test_pending_and_running_runs_count_as_still_going(status):
    client, _ = _client(_Resp(200, [_run_row(status=status)]))
    assert client.find_running("005930", "2026-11-05") == "r1"


def test_finished_runs_other_stocks_and_other_dates_are_not_taken():
    rows = [
        _run_row(status="done", run_id="finished"),
        _run_row(status="error", run_id="failed"),
        _run_row(ticker="000660", run_id="other-stock"),
        _run_row(date="2026-11-04", run_id="other-date"),
    ]
    client, _ = _client(_Resp(200, rows))
    assert client.find_running("005930", "2026-11-05") is None


def test_the_oldest_running_run_is_taken_when_there_are_several():
    rows = [
        _run_row(run_id="newer", created="2026-11-07T01:14:49+00:00"),
        _run_row(run_id="older", created="2026-11-07T01:13:17+00:00"),
    ]
    client, _ = _client(_Resp(200, rows))
    assert client.find_running("005930", "2026-11-05") == "older"  # 끝에 가장 가깝다


@pytest.mark.parametrize(
    "response",
    [
        _Resp(404),  # 분석 목록 주소가 없는 서버 버전
        _Resp(500),
        _Resp(200, json_error=True),
        _Resp(200, {"not": "a list"}),
        requests.ConnectionError("refused"),
    ],
)
def test_when_the_run_list_cannot_be_read_it_is_treated_as_nothing_running_with_a_warning(response, caplog):
    client, _ = _client(response)
    with caplog.at_level(logging.WARNING):
        assert client.find_running("005930", "2026-11-05") is None  # 예전처럼 새로 보낸다
    assert "확인하지 못했습니다" in caplog.text or "모양이 예상과 다릅니다" in caplog.text


def test_garbled_rows_in_the_run_list_are_skipped():
    rows = ["x", None, {"ticker": "005930"}, _run_row(run_id="good")]
    client, _ = _client(_Resp(200, rows))
    assert client.find_running("005930", "2026-11-05") == "good"


# --- 웹: 시간 초과로 실패한 종목을 서버의 분석이 아직 도는 중에 다시 처리해도 새로 보내지 않는다 ---


def test_retrying_a_timed_out_stock_while_the_server_still_runs_it_sends_nothing_new(client, repo):
    session = seed(repo)
    server = swap_server(client, FakeAnalysisServer(polls_before_done=3))  # 세 번 확인한 뒤 끝나는 분석
    use_timeout(client, 0)  # 첫 확인에서 바로 시간 초과
    post(client, session.id, offset=0, count=1)
    assert states(repo, session)["000010"] == "failed"
    assert len(server.started) == 1  # 처음 한 번만 보냈다

    use_timeout(client, 100)  # 이번에는 끝까지 기다린다
    assert post_failed(client, session.id).json() == {"started": True, "queued": 1}
    item = repo.get_item(session.id, "000010")
    assert (item.state, item.decision, item.error_message) == ("done", "Hold", "")
    assert len(server.started) == 1  # 서버에서 도는 분석을 이어서 기다렸다 → 새 분석(쿼터)을 더 쓰지 않았다
    assert item.report_id  # 이어서 기다린 분석의 보고서 번호와 에이전트 등급도 저장된다
    assert item.agents_rated == item.agents_total == 10


def test_the_same_stock_is_sent_again_once_the_earlier_analysis_has_finished(client, repo):
    session = seed(repo)
    server = swap_server(client, FakeAnalysisServer(errors=["000010"]))  # 서버가 오류로 끝낸 분석은 '도는 중' 이 아니다
    post(client, session.id, offset=0, count=1)
    assert states(repo, session)["000010"] == "failed"
    server.errors.clear()
    post_failed(client, session.id)
    assert states(repo, session)["000010"] == "done"
    assert len(server.started) == 2  # 끝난 분석은 이어받지 않고 새로 보낸다 (재시도)
