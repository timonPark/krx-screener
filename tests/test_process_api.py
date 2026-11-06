"""구간 처리 API (#17). 웹 API 경계에서 검증한다.

세션은 저장소로 직접 만들어 둔다(순번이 정해져 있어야 구간을 정확히 검증할 수 있다). 분석 서버는 가짜다.
테스트 클라이언트는 백그라운드 작업이 끝난 뒤에 돌아오므로, 요청 직후 DB 의 최종 상태를 볼 수 있다.
"""

import uuid
from dataclasses import replace
from datetime import date

import pytest
from fakes import FakeAnalysisServer
from fastapi.testclient import TestClient

from krx_screener.repository import Item

CODES = ["000010", "000020", "000030", "000040", "000050"]  # 순번 1~5


def seed(repo, as_of=date(2026, 11, 5), codes=CODES):
    session = repo.create_session(as_of)
    repo.add_items(session.id, [Item(seq=i, code=c, name=f"종목{c}", market="KOSPI") for i, c in enumerate(codes, 1)])
    return session


def states(repo, session):
    return {i.code: i.state for i in repo.items(session.id)}


def post(client, session_id, **body):
    return client.post(f"/api/sessions/{session_id}/process", json=body)


def swap_server(client, server):
    deps = client.app.state.deps
    if server.state_log_root is None:  # 서버와 같은 컴퓨터의 로그 폴더에 상태 로그를 쓴다
        server.state_log_root = deps.externals.state_log_root
    client.app.state.deps = replace(deps, externals=replace(deps.externals, analysis=server))
    return server


# --- 구간 ---


def test_a_range_is_sent_in_order_one_at_a_time_and_the_results_are_saved(client, repo, externals):
    session = seed(repo)
    response = post(client, session.id, offset=1, count=2)  # 순번 2~3
    assert response.status_code == 202
    assert response.json() == {"started": True, "queued": 2}
    # 순서대로, 세션의 기준일과 설정의 effort(low)로
    assert externals.analysis.started == [("000020", "2026-11-05", "low"), ("000030", "2026-11-05", "low")]
    assert states(repo, session) == {
        "000010": "pending", "000020": "done", "000030": "done", "000040": "pending", "000050": "pending",
    }
    done = repo.get_item(session.id, "000020")
    assert (done.decision, done.score, done.error_message) == ("Hold", 0, "")  # 가짜 서버의 에이전트는 모두 보유 → 점수 0
    assert done.analyzed_at is not None


def test_finished_stocks_are_not_sent_again(client, repo, externals):
    session = seed(repo)
    repo.mark_done(session.id, "000020", decision="Buy", score=1.0, agents_rated=10, agents_total=10)
    response = post(client, session.id, offset=0, count=3)
    assert response.json() == {"started": True, "queued": 2}  # 순번 1, 3 만
    assert [s[0] for s in externals.analysis.started] == ["000010", "000030"]
    assert repo.get_item(session.id, "000020").decision == "Buy"  # 끝난 종목은 그대로


def test_a_range_that_is_already_done_sends_nothing_and_says_so(client, repo, externals):
    session = seed(repo)
    for code in CODES[:2]:
        repo.mark_done(session.id, code, decision="Hold", score=0.0, agents_rated=10, agents_total=10)
    response = post(client, session.id, offset=0, count=2)
    assert (response.status_code, response.json()) == (200, {"started": False, "queued": 0})
    assert externals.analysis.started == []


def test_a_count_past_the_end_stops_at_the_last_stock(client, repo, externals):
    session = seed(repo)
    assert post(client, session.id, offset=3, count=100).json() == {"started": True, "queued": 2}
    assert [s[0] for s in externals.analysis.started] == ["000040", "000050"]


def test_the_servers_decision_is_saved(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(decisions={"000010": "Buy", "000020": "Underweight"}))
    post(client, session.id, offset=0, count=2)
    assert [repo.get_item(session.id, c).decision for c in ("000010", "000020")] == ["Buy", "Underweight"]


def test_latest_means_the_most_recent_session_and_other_sessions_are_untouched(client, repo, externals):
    older = seed(repo, date(2026, 11, 4))
    newer = seed(repo, date(2026, 11, 5))
    assert post(client, "latest", offset=0, count=1).json() == {"started": True, "queued": 1}
    assert states(repo, newer)["000010"] == "done"
    assert set(states(repo, older).values()) == {"pending"}
    assert externals.analysis.started[0][1] == "2026-11-05"


# --- 입력 검사 ---


@pytest.mark.parametrize(
    "body",
    [{"offset": -1, "count": 1}, {"offset": 0, "count": 0}, {"offset": "a", "count": 1}, {"count": 1}, {"offset": 0}],
)
def test_a_bad_range_is_a_korean_422_and_nothing_is_sent(client, repo, externals, body):
    session = seed(repo)
    response = post(client, session.id, **body)
    assert response.status_code == 422
    assert response.json() == {"detail": "오프셋은 0 이상, 개수는 1 이상의 정수로 입력하세요."}
    assert externals.analysis.started == []
    assert set(states(repo, session).values()) == {"pending"}


def test_an_offset_beyond_the_list_is_a_korean_422(client, repo, externals):
    session = seed(repo)
    response = post(client, session.id, offset=5, count=1)  # 종목은 5개라 오프셋 5 부터는 없다
    assert response.status_code == 422
    assert response.json() == {"detail": "오프셋이 종목 수(5)보다 크거나 같습니다."}
    assert externals.analysis.started == []


@pytest.mark.parametrize("session_id", [str(uuid.uuid4()), "abc"])
def test_an_unknown_session_is_a_korean_404(client, session_id):
    response = post(client, session_id, offset=0, count=1)
    assert (response.status_code, response.json()) == (404, {"detail": "세션을 찾지 못했습니다."})


def test_when_the_analysis_server_is_down_nothing_is_started(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(health_error="TradingAgents-KR 서버에 연결하지 못했습니다 (http://127.0.0.1:8000)"))
    response = post(client, session.id, offset=0, count=2)
    assert response.status_code == 503
    assert "서버에 연결하지 못했습니다" in response.json()["detail"]
    assert set(states(repo, session).values()) == {"pending"}  # 처리 중으로 바뀐 종목도 없다


# --- 실패와 진행 ---


def test_a_failure_does_not_stop_the_others_and_is_recorded(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(errors=["000020"]))
    post(client, session.id, offset=0, count=3)
    assert states(repo, session) == {
        "000010": "done", "000020": "failed", "000030": "done", "000040": "pending", "000050": "pending",
    }
    failed = repo.get_item(session.id, "000020")
    assert "가짜 서버 오류" in failed.error_message
    assert failed.decision == ""


def test_an_unexpected_error_on_one_stock_marks_it_failed_and_the_rest_go_on(client, repo):
    class Exploding(FakeAnalysisServer):
        def start(self, ticker, trade_date, effort):
            if ticker == "000020":
                raise RuntimeError("예상하지 못한 실패")
            return super().start(ticker, trade_date, effort)

    session = seed(repo)
    swap_server(client, Exploding())
    post(client, session.id, offset=0, count=3)
    assert states(repo, session)["000010"] == "done"
    assert states(repo, session)["000030"] == "done"
    failed = repo.get_item(session.id, "000020")
    assert failed.state == "failed"
    assert "처리 중 오류" in failed.error_message


def test_a_failed_stock_is_tried_again_on_the_next_call(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(errors=["000010"]))
    post(client, session.id, offset=0, count=1)
    assert states(repo, session)["000010"] == "failed"
    swap_server(client, FakeAnalysisServer())  # 이번에는 서버가 성공한다
    assert post(client, session.id, offset=0, count=1).json() == {"started": True, "queued": 1}
    after = repo.get_item(session.id, "000010")
    assert (after.state, after.error_message) == ("done", "")  # 재시도하면 사유가 지워진다


def test_progress_is_visible_while_a_stock_is_being_analyzed(client, repo):
    seen = []

    class Watching(FakeAnalysisServer):
        def snapshot(self, run_id):
            code = run_id.removeprefix("run-")
            # 분석 중에 다른 연결로 읽는다: 이 종목은 처리 중이고, 앞 종목은 이미 완료로 보여야 한다
            seen.append((code, {i.code: i.state for i in repo.items(session.id)}))
            return super().snapshot(run_id)

    session = seed(repo)
    swap_server(client, Watching())
    post(client, session.id, offset=0, count=2)
    assert seen[0] == ("000010", {"000010": "running", "000020": "pending", "000030": "pending", "000040": "pending", "000050": "pending"})
    assert seen[1][1]["000010"] == "done" and seen[1][1]["000020"] == "running"  # 앞 종목의 완료가 바로 보인다


def test_the_session_view_shows_the_finished_results(client, repo):
    session = seed(repo)
    post(client, session.id, offset=0, count=2)
    body = client.get(f"/api/sessions/{session.id}").json()
    assert body["counts"] == {"pending": 3, "running": 0, "done": 2, "failed": 0}
    assert [i["decision_label"] for i in body["items"][:2]] == ["보유", "보유"]  # Hold → 보유
    assert body["processing"] is False


def test_when_the_database_is_down_it_is_a_korean_503(tmp_path):
    from fakes import make_externals, repo_config

    from krx_screener.api import Dependencies, create_app
    from krx_screener.db import DatabaseError

    def broken():
        raise DatabaseError("PostgreSQL 에 연결하지 못했습니다 (127.0.0.1:1/krx).")

    deps = Dependencies(connect=broken, externals=make_externals(tmp_path), config=repo_config())
    response = TestClient(create_app(deps)).post(f"/api/sessions/{uuid.uuid4()}/process", json={"offset": 0, "count": 1})
    assert response.status_code == 503
    assert response.json() == {"detail": "PostgreSQL 에 연결하지 못했습니다 (127.0.0.1:1/krx)."}


# --- 보고서 번호 (#20) ---


def test_the_report_id_of_a_finished_stock_is_saved_and_shown(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(report_ids={"000010": "000010_20261106_172435"}))
    post(client, session.id, offset=0, count=2)
    assert repo.get_item(session.id, "000010").report_id == "000010_20261106_172435"
    assert repo.get_item(session.id, "000020").report_id == "000020_20261106_100000"  # 종목마다 자기 번호
    items = client.get(f"/api/sessions/{session.id}").json()["items"]
    assert [i["report_id"] for i in items[:3]] == ["000010_20261106_172435", "000020_20261106_100000", ""]


def test_the_run_id_is_never_exposed(client, repo):
    session = seed(repo)
    post(client, session.id, offset=0, count=1)
    assert "run-" not in client.get(f"/api/sessions/{session.id}").text  # 가짜 서버의 run_id 는 run-<종목코드>


def test_a_failed_stock_has_no_report_id(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(errors=["000010"]))
    post(client, session.id, offset=0, count=1)
    assert repo.get_item(session.id, "000010").report_id == ""


def test_when_the_report_id_cannot_be_read_the_grade_is_still_saved(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(decisions={"000010": "Buy"}, report_error="서버가 이 분석을 찾지 못합니다."))
    post(client, session.id, offset=0, count=1)
    item = repo.get_item(session.id, "000010")
    assert (item.state, item.decision, item.report_id, item.error_message) == ("done", "Buy", "", "")  # 번호만 비고 실패가 아니다


def test_a_run_that_saved_no_report_has_an_empty_report_id(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(report_ids={"000010": None}))
    post(client, session.id, offset=0, count=1)
    item = repo.get_item(session.id, "000010")
    assert (item.state, item.report_id) == ("done", "")


def test_a_successful_reanalysis_replaces_the_report_id(client, repo):
    session = seed(repo)
    repo.mark_done(session.id, "000010", decision="Buy", score=1.0, agents_rated=10, agents_total=10, report_id="000010_20261101_090000")
    repo.mark_failed(session.id, "000010", "이전 재분석 실패")
    swap_server(client, FakeAnalysisServer(report_ids={"000010": "000010_20261106_172435"}))
    post(client, session.id, offset=0, count=1)
    assert repo.get_item(session.id, "000010").report_id == "000010_20261106_172435"


# --- 에이전트별 등급 (#21) ---


def test_the_agent_ratings_are_read_right_after_the_analysis_and_saved(client, repo):
    session = seed(repo)
    ratings = {"market": "Buy", "news": "Hold", "bull": "Overweight", "bear": "Underweight", "portfolio_manager": "Buy"}
    swap_server(client, FakeAnalysisServer(agent_ratings={"000010": ratings}))
    post(client, session.id, offset=0, count=2)
    saved = repo.agent_ratings(session.id, "000010")
    assert {a: saved[a] for a in ratings} == ratings
    assert len(saved) == 10  # 나머지 에이전트는 기본 Hold 로 읽힌다
    assert set(repo.agent_ratings(session.id, "000020").values()) == {"Hold"}  # 종목마다 자기 로그


def test_a_cell_that_cannot_be_read_is_left_out_and_the_item_is_still_done(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(agent_ratings={"000010": {"news": None, "neutral": None}}))
    post(client, session.id, offset=0, count=1)
    saved = repo.agent_ratings(session.id, "000010")
    assert "news" not in saved and "neutral" not in saved and len(saved) == 8
    assert repo.get_item(session.id, "000010").state == "done"


def test_when_the_state_log_is_missing_the_grade_is_saved_without_agent_ratings(client, repo, tmp_path):
    session = seed(repo)
    server = swap_server(client, FakeAnalysisServer(decisions={"000010": "Buy"}))
    server.state_log_root = tmp_path / "서버가-쓰는-다른-폴더"  # 이 컴퓨터가 읽는 폴더에는 로그가 없다
    post(client, session.id, offset=0, count=1)
    item = repo.get_item(session.id, "000010")
    assert (item.state, item.decision) == ("done", "Buy")
    assert repo.agent_ratings(session.id, "000010") == {}


def test_a_successful_reanalysis_replaces_the_old_agent_ratings(client, repo):
    session = seed(repo)
    repo.mark_done(session.id, "000010", decision="Buy", score=1.0, agents_rated=2, agents_total=10)
    repo.save_agent_ratings(session.id, "000010", {"market": "Buy", "news": "Buy", "bull": "Buy"})
    repo.mark_failed(session.id, "000010", "이전 재분석 실패")
    swap_server(client, FakeAnalysisServer(agent_ratings={"000010": {"market": "Sell", "news": None}}))
    post(client, session.id, offset=0, count=1)
    saved = repo.agent_ratings(session.id, "000010")
    assert saved["market"] == "Sell"  # 새 값
    assert "news" not in saved  # 새 로그에서 비었으면 옛 값을 남기지 않는다
    assert saved["bull"] == "Hold"  # 새 로그에서 읽은 값


def test_a_state_log_that_is_left_from_an_earlier_analysis_does_not_leak_into_a_failed_one(client, repo):
    from fakes import write_state_log

    session = seed(repo)
    deps_root = client.app.state.deps.externals.state_log_root
    write_state_log(deps_root, "000010", date(2026, 11, 5), {"market": "Buy"})  # 이전 분석이 남긴 로그
    swap_server(client, FakeAnalysisServer(errors=["000010"]))
    post(client, session.id, offset=0, count=1)
    assert repo.get_item(session.id, "000010").state == "failed"
    assert repo.agent_ratings(session.id, "000010") == {}  # 실패한 분석의 로그는 읽지 않는다


def test_the_ratings_of_one_stock_do_not_change_the_others(client, repo):
    session = seed(repo)
    post(client, session.id, offset=0, count=1)
    assert repo.agent_ratings(session.id, "000020") == {}  # 처리하지 않은 종목


def test_if_saving_the_agent_ratings_fails_the_stock_is_not_half_done(client, repo, monkeypatch):
    from krx_screener.repository import SessionRepository

    def broken(self, session_id, code, ratings):
        raise RuntimeError("DB 오류")

    monkeypatch.setattr(SessionRepository, "save_agent_ratings", broken)
    session = seed(repo)
    post(client, session.id, offset=0, count=1)
    item = repo.get_item(session.id, "000010")
    assert (item.state, item.decision) == ("failed", "")  # 완료 표시와 등급이 함께 취소된다
    assert "처리 중 오류" in item.error_message


# --- 평가점수 (#22) ---


def view_item(client, session, index=0):
    return client.get(f"/api/sessions/{session.id}").json()["items"][index]


def test_the_score_is_the_average_of_the_agent_ratings_and_the_count_is_saved(client, repo):
    session = seed(repo)
    ratings = {"market": "Buy", "news": "Overweight", "bull": "Buy", "bear": "Sell"}  # 나머지 6개는 기본 Hold(0)
    swap_server(client, FakeAnalysisServer(agent_ratings={"000010": ratings}))
    post(client, session.id, offset=0, count=2)
    item = repo.get_item(session.id, "000010")
    assert (item.score, item.agents_rated, item.agents_total) == (3 / 10, 10, 10)  # (2+1+2−2 + 0×6) / 10
    other = repo.get_item(session.id, "000020")
    assert (other.score, other.agents_rated, other.agents_total) == (0, 10, 10)  # 모두 보유면 0


def test_all_buy_is_plus_two(client, repo):
    session = seed(repo)
    from krx_screener.agent_ratings import AGENT_FIELDS

    swap_server(client, FakeAnalysisServer(agent_ratings={"000010": dict.fromkeys(AGENT_FIELDS, "Buy")}))
    post(client, session.id, offset=0, count=1)
    assert repo.get_item(session.id, "000010").score == 2


def test_agents_that_could_not_be_read_are_left_out_and_the_count_says_how_many_were_used(client, repo):
    session = seed(repo)
    unread = dict.fromkeys(["news", "fundamentals", "bear", "neutral", "aggressive", "conservative"])  # 6개 칸이 비었다
    swap_server(client, FakeAnalysisServer(agent_ratings={"000010": {"market": "Buy", "bull": "Overweight", **unread}}))
    post(client, session.id, offset=0, count=1)
    item = repo.get_item(session.id, "000010")
    # 읽은 건 market(+2), bull(+1), research_manager·portfolio_manager(기본 Hold 0) 4개 → (2+1+0+0)/4. 빈 칸을 0 으로 치지 않는다
    assert (item.score, item.agents_rated, item.agents_total) == (3 / 4, 4, 10)


def test_when_no_agent_could_be_read_the_score_is_empty_but_the_grade_is_saved(client, repo, tmp_path):
    session = seed(repo)
    server = swap_server(client, FakeAnalysisServer(decisions={"000010": "Buy"}))
    server.state_log_root = tmp_path / "서버가-쓰는-다른-폴더"  # 로그를 읽을 수 없다
    post(client, session.id, offset=0, count=1)
    item = repo.get_item(session.id, "000010")
    assert (item.state, item.decision) == ("done", "Buy")
    assert (item.score, item.agents_rated, item.agents_total) == (None, 0, 10)  # 점수를 지어내지 않는다 (0 이 아니다)


def test_the_score_comes_from_the_agent_ratings_not_from_the_final_decision(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(decisions={"000010": "Buy"}))  # 최종 등급은 매수, 에이전트는 모두 보유
    post(client, session.id, offset=0, count=1)
    item = repo.get_item(session.id, "000010")
    assert (item.decision, item.score) == ("Buy", 0)


def test_the_session_view_shows_the_score_and_the_counts(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(agent_ratings={"000010": {"market": "Buy", "news": "Buy"}}))
    post(client, session.id, offset=0, count=1)
    done, pending = view_item(client, session, 0), view_item(client, session, 1)
    assert (done["score"], done["agents_rated"], done["agents_total"]) == (4 / 10, 10, 10)  # (2+2+0×8)/10
    assert (pending["score"], pending["agents_rated"], pending["agents_total"]) == (None, 0, 0)  # 처리 전


def test_a_successful_reanalysis_replaces_the_score(client, repo):
    session = seed(repo)
    repo.mark_done(session.id, "000010", decision="Buy", score=2.0, agents_rated=10, agents_total=10)
    repo.mark_failed(session.id, "000010", "이전 재분석 실패")
    swap_server(client, FakeAnalysisServer(agent_ratings={"000010": {"market": "Sell"}}))
    post(client, session.id, offset=0, count=1)
    assert repo.get_item(session.id, "000010").score == -2 / 10


def test_a_failed_stock_gets_no_score(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(errors=["000010"]))
    post(client, session.id, offset=0, count=1)
    item = repo.get_item(session.id, "000010")
    assert (item.state, item.score, item.agents_rated, item.agents_total) == ("failed", None, 0, 0)
