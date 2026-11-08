"""서버에서는 끝났는데 실패로 기록된 종목은 다시 처리할 때 새로 분석하지 않고 서버의 끝난 보고서를 가져온다 (#75).

연결이 끊기거나 시간 초과로 기다리기를 멈춘 분석은 서버에서 계속 돌아 보고서까지 저장하는데, 그 종목은 실패로 남아 있었다.
TradingAgents-KR 의 ``GET /api/reports?ticker=&trade_date=`` (#174)가 기준일과 에이전트별 등급을 주므로 ``run_id`` 없이도 찾을 수 있다.
같은 종목·기준일의 보고서가 여럿이면 가장 최근 것을 쓴다. 옛 서버(필터·기준일 없음)나 읽지 못한 응답은 "없음" 이라서 예전처럼 새로 분석한다.
"""

import logging

import pytest
import requests
from fakes import FakeAnalysisServer
from test_analysis import FakeSession, _Resp
from test_process_api import post, post_failed, seed, states, swap_server

from krx_screener.analysis import AnalysisClient, FinishedReport

AS_OF = "2026-11-05"


def _row(ticker="003380", date=AS_OF, stamp="2026-11-06T17:24:35", decision="Hold", ratings=None, report_id=None):
    return {
        "report_id": report_id or f"{ticker}_{stamp[:10].replace('-', '')}_{stamp[11:].replace(':', '')}",
        "ticker": ticker,
        "name": "종목",
        "stamp": stamp,
        "decision": decision,
        "trade_date": date,
        "agent_ratings": {"market": "Hold", "bull": "Buy"} if ratings is None else ratings,
    }


def _client(*responses):
    session = FakeSession(responses)
    return AnalysisClient("http://127.0.0.1:8000/", session=session), session


# --- 클라이언트: 서버의 끝난 보고서 찾기 ---


def test_the_report_list_is_asked_with_the_ticker_and_the_trade_date_and_the_report_is_read():
    client, session = _client(_Resp(200, [_row(decision="Underweight", ratings={"market": "Hold", "bear": "Underweight"})]))
    assert client.find_finished_report("003380", AS_OF) == FinishedReport(
        report_id="003380_20261106_172435", decision="Underweight", agent_ratings={"market": "Hold", "bear": "Underweight"}
    )
    assert [(m, u) for m, u, _ in session.calls] == [
        ("GET", "http://127.0.0.1:8000/api/reports?ticker=003380&trade_date=2026-11-05&limit=20")
    ]


def test_the_most_recent_report_is_taken_when_there_are_several():
    rows = [
        _row(stamp="2026-11-06T17:24:35", decision="Hold", report_id="003380_20261106_172435"),
        _row(stamp="2026-11-07T09:00:00", decision="Buy", report_id="003380_20261107_090000"),  # 목록 순서와 상관없이 가장 늦은 것
        _row(stamp="2026-11-06T08:00:00", decision="Sell", report_id="003380_20261106_080000"),
    ]
    client, _ = _client(_Resp(200, rows))
    found = client.find_finished_report("003380", AS_OF)
    assert (found.report_id, found.decision) == ("003380_20261107_090000", "Buy")


def test_other_stocks_other_dates_and_old_servers_that_ignore_the_filter_give_nothing():
    rows = [
        _row(ticker="000660"),  # 필터를 무시하는 옛 서버는 다른 종목도 준다
        _row(date="2026-11-04"),
        {k: v for k, v in _row().items() if k != "trade_date"},  # 옛 서버: 기준일 필드가 없다 → 기준일이 맞는지 알 수 없다
        _row(date=None),  # 헤더에 기준일이 없는 보고서
    ]
    client, _ = _client(_Resp(200, rows))
    assert client.find_finished_report("003380", AS_OF) is None


def test_reports_without_a_usable_decision_or_report_id_are_skipped():
    rows = [
        _row(decision=None),
        _row(decision="REDUCE"),  # 5단계 등급이 아닌 값
        _row(report_id="not-a-report-id"),
        _row(stamp="2026-11-06T10:00:00", decision="Overweight", report_id="003380_20261106_100000"),
    ]
    client, _ = _client(_Resp(200, rows))
    found = client.find_finished_report("003380", AS_OF)
    assert (found.report_id, found.decision) == ("003380_20261106_100000", "Overweight")


def test_agent_ratings_are_kept_only_when_they_are_name_to_text_pairs():
    client, _ = _client(_Resp(200, [_row(ratings={"market": "Buy", "news": 3, "bull": None, 7: "Hold"})]))
    assert client.find_finished_report("003380", AS_OF).agent_ratings == {"market": "Buy"}


def test_a_report_without_agent_ratings_is_still_found():
    client, _ = _client(_Resp(200, [{k: v for k, v in _row().items() if k != "agent_ratings"}]))
    assert client.find_finished_report("003380", AS_OF).agent_ratings == {}


@pytest.mark.parametrize(
    "response",
    [_Resp(404), _Resp(500), _Resp(200, json_error=True), _Resp(200, {"not": "a list"}), requests.ConnectionError("refused")],
    ids=["404", "500", "not-json", "not-a-list", "connection"],
)
def test_when_the_report_list_cannot_be_read_it_is_treated_as_nothing_with_a_warning(response, caplog):
    client, _ = _client(response)
    with caplog.at_level(logging.WARNING):
        assert client.find_finished_report("003380", AS_OF) is None  # 예전처럼 새로 분석한다
    assert "끝난 분석" in caplog.text


def test_garbled_rows_in_the_report_list_are_skipped():
    client, _ = _client(_Resp(200, ["x", None, {"ticker": "003380"}, _row()]))
    assert client.find_finished_report("003380", AS_OF).report_id == "003380_20261106_172435"


# --- 웹: 실패한 종목 다시 처리가 서버의 끝난 보고서를 가져온다 ---

REPORT = "000010_20261105_120000"


def _server(client, **kwargs):
    return swap_server(client, FakeAnalysisServer(**kwargs))


def test_a_failed_stock_the_server_already_finished_is_recovered_without_sending_a_new_analysis(client, repo):
    session = seed(repo)
    repo.mark_failed(session.id, "000010", "TradingAgents-KR 서버에 연결하지 못했습니다 (http://127.0.0.1:8000): ConnectionError")
    server = _server(
        client,
        finished_reports={
            "000010": FinishedReport(REPORT, "Underweight", {"market": "Hold", "bull": "Buy", "bear": "Underweight", "news": "Hold"})
        },
    )
    assert post_failed(client, session.id).json() == {"started": True, "queued": 1}
    assert server.started == []  # 쿼터를 쓰지 않았다
    item = repo.get_item(session.id, "000010")
    assert (item.state, item.decision, item.error_message, item.report_id) == ("done", "Underweight", "", REPORT)
    assert (item.score, item.agents_rated, item.agents_total) == (0.25, 4, 10)  # (0+2-1+0)/4 — 읽은 칸만 평균, 총 10
    assert repo.agent_ratings(session.id, "000010") == {"market": "Hold", "bull": "Buy", "bear": "Underweight", "news": "Hold"}


def test_only_the_stocks_without_a_finished_report_are_analyzed_again(client, repo):
    session = seed(repo)
    for code in ("000010", "000020", "000030"):
        repo.mark_failed(session.id, code, "이전 실패")
    server = _server(client, finished_reports={"000020": FinishedReport("000020_20261105_120000", "Buy", {"market": "Buy"})})
    assert post_failed(client, session.id).json() == {"started": True, "queued": 3}
    assert [s[0] for s in server.started] == ["000010", "000030"]  # 000020 은 가져왔다
    assert repo.get_item(session.id, "000020").decision == "Buy"
    assert repo.get_item(session.id, "000020").report_id == "000020_20261105_120000"


def test_the_earlier_elapsed_time_is_kept_because_nothing_was_measured(client, repo, pg_conn):
    session = seed(repo)
    repo.mark_done(session.id, "000010", decision="Hold", score=0.0, agents_rated=10, agents_total=10, elapsed_seconds=321.5)
    repo.mark_failed(session.id, "000010", "이전 재분석 실패")
    _server(client, finished_reports={"000010": FinishedReport(REPORT, "Buy", {"market": "Buy"})})
    post_failed(client, session.id)
    item = repo.get_item(session.id, "000010")
    assert item.state == "done" and item.decision == "Buy"
    elapsed = pg_conn.execute("SELECT elapsed_seconds FROM session_items WHERE code = '000010'").fetchone()[0]
    assert elapsed == 321.5  # 가져온 종목에는 분석 시간이 없다 (예상 시간 평균을 흐리지 않는다)


def test_unknown_agents_and_ratings_in_the_servers_answer_are_not_saved(client, repo):
    session = seed(repo)
    repo.mark_failed(session.id, "000010", "이전 실패")
    _server(
        client,
        finished_reports={"000010": FinishedReport(REPORT, "Hold", {"market": "Buy", "ghost": "Buy", "news": "REDUCE"})},
    )
    post_failed(client, session.id)
    assert repo.agent_ratings(session.id, "000010") == {"market": "Buy"}
    item = repo.get_item(session.id, "000010")
    assert (item.score, item.agents_rated) == (2.0, 1)


def test_a_finished_report_does_not_change_a_range_call_pending_stocks_are_still_analyzed(client, repo):
    session = seed(repo)
    server = _server(client, finished_reports={"000010": FinishedReport(REPORT, "Buy", {"market": "Buy"})})
    assert post(client, session.id, offset=0, count=1).json() == {"started": True, "queued": 1}
    assert [s[0] for s in server.started] == ["000010"]  # 대기 종목은 예전처럼 새로 분석한다 (가져오기는 다시 처리 버튼만)
    assert repo.get_item(session.id, "000010").report_id != REPORT


def test_a_stock_with_no_finished_report_is_analyzed_again_as_before(client, repo):
    session = seed(repo)
    repo.mark_failed(session.id, "000010", "이전 실패")
    server = _server(client)
    assert post_failed(client, session.id).json() == {"started": True, "queued": 1}
    assert [s[0] for s in server.started] == ["000010"]
    assert states(repo, session)["000010"] == "done"


def test_recovered_stocks_make_the_ranking_stale_like_any_new_result(client, repo):
    from test_organize_api import organize

    session = seed(repo)
    for code in ("000010", "000020", "000030"):
        repo.mark_done(session.id, code, decision="Hold", score=0.0, agents_rated=10, agents_total=10)
    for code in ("000040", "000050"):
        repo.mark_done(session.id, code, decision="Hold", score=0.1, agents_rated=10, agents_total=10)
    repo.mark_failed(session.id, "000030", "이전 실패")
    organize(client, session.id)
    assert client.get(f"/api/sessions/{session.id}").json()["rank_stale"] is False
    _server(client, finished_reports={"000030": FinishedReport("000030_20261105_120000", "Buy", {"market": "Buy"})})
    post_failed(client, session.id)
    assert client.get(f"/api/sessions/{session.id}").json()["rank_stale"] is True
