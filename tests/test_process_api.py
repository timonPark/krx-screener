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
    assert (done.decision, done.score, done.error_message) == ("Hold", None, "")  # 점수는 #22 가 채운다
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
