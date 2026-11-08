"""실패한 종목은 구간 처리에서 빼고, "실패한 종목 다시 처리" 로만 다시 돌린다 (#77).

구간 처리(``/process``)가 실패한 종목을 순번 순서대로 끼워 돌리면 아직 안 돌린 대기 종목이 뒤로 밀리고 쿼터와 시간을 쓴다.
그래서 ``/process`` 는 대기(와 처리 중으로 남은) 종목만 보내고, 실패한 종목은 ``POST /api/sessions/{id}/process-failed`` 가
세션 전체에서 한꺼번에 다시 보낸다. 서버가 받지 않는 형식의 코드(대문자 영숫자 6자가 아님)는 다시 돌려도 계속 실패하므로 뺀다.
영숫자 코드(``0126Z0`` 등)는 서버가 받게 되어(TradingAgents-KR #181) 다른 종목처럼 다시 처리한다 (#82).
"""

import uuid
from datetime import date

import pytest
from fakes import FakeAnalysisServer
from test_process_api import post, post_failed, seed, states, swap_server

CODES = ["000010", "000020", "000030", "000040", "000050", "0126z0"]  # 순번 1~6, 마지막은 서버가 거절하는 코드(소문자)
BUSY = {"detail": "이미 처리 중입니다."}


def fail(repo, session, *codes, reason="이전 처리 실패"):
    for code in codes:
        repo.mark_failed(session.id, code, reason)


# --- 구간 처리는 실패한 종목을 보내지 않는다 ---


def test_a_range_does_not_send_failed_stocks_and_the_pending_ones_run_first(client, repo, externals):
    session = seed(repo)
    fail(repo, session, "000010")  # 순번이 앞선 실패 종목이 대기 종목보다 먼저 돌던 것이 문제였다
    assert post(client, session.id, offset=0, count=3).json() == {"started": True, "queued": 2}
    assert [s[0] for s in externals.analysis.started] == ["000020", "000030"]
    assert repo.get_item(session.id, "000010").state == "failed"  # 실패는 그대로 남는다
    assert repo.get_item(session.id, "000010").error_message == "이전 처리 실패"


def test_a_range_of_only_failed_stocks_sends_nothing_and_says_so(client, repo, externals):
    session = seed(repo)
    fail(repo, session, "000010", "000020")
    response = post(client, session.id, offset=0, count=2)
    assert (response.status_code, response.json()) == (200, {"started": False, "queued": 0})
    assert externals.analysis.started == []


def test_a_stock_left_running_by_a_crash_is_still_sent_by_the_range(client, repo):
    session = seed(repo)
    repo.mark_running(session.id, "000010")  # 처리하던 프로세스가 죽어서 남은 표시. 실패가 아니라 끝나지 않은 것이다
    assert post(client, session.id, offset=0, count=1).json() == {"started": True, "queued": 1}
    assert states(repo, session)["000010"] == "done"


# --- 실패한 종목 다시 처리 ---


def test_all_failed_stocks_of_the_session_are_sent_again_in_order_and_the_rest_are_untouched(client, repo, externals):
    session = seed(repo, codes=CODES)
    fail(repo, session, "000020", "000040")
    repo.mark_done(session.id, "000010", decision="Buy", score=1.0, agents_rated=10, agents_total=10)
    response = post_failed(client, session.id)
    assert (response.status_code, response.json()) == (202, {"started": True, "queued": 2})
    assert [s[0] for s in externals.analysis.started] == ["000020", "000040"]  # 순번 순서, 구간과 상관없이 세션 전체
    assert states(repo, session) == {
        "000010": "done", "000020": "done", "000030": "pending", "000040": "done", "000050": "pending", "0126z0": "pending",
    }
    assert repo.get_item(session.id, "000010").decision == "Buy"  # 끝난 종목은 다시 보내지 않는다


def test_a_retried_stock_gets_its_result_and_its_reason_is_cleared(client, repo):
    session = seed(repo)
    fail(repo, session, "000010", reason="분석 서버가 응답하지 않았습니다")
    swap_server(client, FakeAnalysisServer(decisions={"000010": "Buy"}))
    post_failed(client, session.id)
    item = repo.get_item(session.id, "000010")
    assert (item.state, item.decision, item.error_message) == ("done", "Buy", "")


def test_a_stock_that_fails_again_stays_failed_with_the_new_reason(client, repo):
    session = seed(repo)
    fail(repo, session, "000010", reason="처음 이유")
    swap_server(client, FakeAnalysisServer(errors=["000010"]))
    assert post_failed(client, session.id).json() == {"started": True, "queued": 1}
    item = repo.get_item(session.id, "000010")
    assert item.state == "failed" and "가짜 서버 오류" in item.error_message


def test_failed_stocks_the_server_would_refuse_are_left_out_because_they_can_never_succeed(client, repo, externals):
    session = seed(repo, codes=CODES)
    fail(repo, session, "000020", "0126z0", reason="서버가 받는 종목코드(대문자 영숫자 6자)가 아니라 보내지 않았습니다.")
    assert post_failed(client, session.id).json() == {"started": True, "queued": 1}
    assert [s[0] for s in externals.analysis.started] == ["000020"]  # 서버가 거절하는 형식은 보내지 않는다
    assert states(repo, session)["0126z0"] == "failed"
    assert "영숫자 6자" in repo.get_item(session.id, "0126z0").error_message  # 사유는 그대로 남는다


@pytest.mark.parametrize("failed", [(), ("0126z0",)], ids=["no-failures", "only-refused-code"])
def test_nothing_is_sent_when_there_is_nothing_to_retry(client, repo, externals, failed):
    session = seed(repo, codes=CODES)
    fail(repo, session, *failed)
    response = post_failed(client, session.id)
    assert (response.status_code, response.json()) == (200, {"started": False, "queued": 0})
    assert externals.analysis.started == []


def test_a_failed_alphanumeric_code_is_sent_again_now_that_the_server_accepts_it(client, repo, externals):
    session = seed(repo, codes=["000010", "0126Z0", "0088M0"])
    fail(repo, session, "0126Z0", "0088M0", reason="서버가 숫자 6자리 종목코드만 받아 보내지 않았습니다.")  # 서버가 받기 전에 남은 옛 사유
    assert post_failed(client, session.id).json() == {"started": True, "queued": 2}
    assert [s[0] for s in externals.analysis.started] == ["0126Z0", "0088M0"]
    item = repo.get_item(session.id, "0126Z0")
    assert (item.state, item.error_message) == ("done", "")  # 옛 사유는 지워진다


# --- 구간 처리와 같은 안전장치 ---


def test_retrying_while_the_session_is_being_processed_is_a_409_and_nothing_is_sent(client, repo, externals, hold_lock):
    session = seed(repo)
    fail(repo, session, "000010")
    hold_lock(session.id)
    response = post_failed(client, session.id)
    assert (response.status_code, response.json()) == (409, BUSY)
    assert externals.analysis.started == []
    assert states(repo, session)["000010"] == "failed"


def test_the_lock_is_held_while_retrying_so_a_range_call_gets_409(client, repo):
    session = seed(repo)
    fail(repo, session, "000010")
    seen = []

    class Watching(FakeAnalysisServer):
        def snapshot(self, run_id):
            seen.append(post(client, session.id, offset=1, count=1))  # 다시 처리하는 동안 구간 처리가 들어온다
            return super().snapshot(run_id)

    swap_server(client, Watching())
    assert post_failed(client, session.id).status_code == 202
    assert [(r.status_code, r.json()) for r in seen][:1] == [(409, BUSY)]
    assert states(repo, session)["000020"] == "pending"


def test_retrying_when_the_analysis_server_is_down_is_a_503_and_changes_nothing(client, repo):
    session = seed(repo)
    fail(repo, session, "000010", reason="이전 이유")
    server = swap_server(client, FakeAnalysisServer(health_error="TradingAgents-KR 서버에 연결하지 못했습니다"))
    assert post_failed(client, session.id).status_code == 503
    assert server.started == []
    assert repo.get_item(session.id, "000010").error_message == "이전 이유"  # 아무것도 시작하지 않았다


def test_retrying_is_a_409_while_the_list_is_still_being_built(client, repo, externals, hold_lock):
    session_id = uuid.uuid4()
    hold_lock(session_id)  # 목록을 만드는 쪽이 쥔 잠금
    repo.create_building_session(session_id, date(2026, 11, 5))
    response = post_failed(client, session_id)
    assert (response.status_code, response.json()) == (409, {"detail": "목록을 만드는 중입니다. 끝난 뒤에 처리할 수 있습니다."})
    assert externals.analysis.started == []


@pytest.mark.parametrize("session_id", ["no-such-session", "00000000-0000-0000-0000-000000000000"])
def test_an_unknown_session_is_a_korean_404(client, session_id):
    response = post_failed(client, session_id)
    assert (response.status_code, response.json()) == (404, {"detail": "세션을 찾지 못했습니다."})


def test_latest_means_the_most_recent_session(client, repo, externals):
    older = seed(repo, date(2026, 11, 4))
    newer = seed(repo, date(2026, 11, 5))
    fail(repo, older, "000010")
    fail(repo, newer, "000020")
    assert post_failed(client, "latest").json() == {"started": True, "queued": 1}
    assert states(repo, newer)["000020"] == "done"
    assert states(repo, older)["000010"] == "failed"  # 다른 세션은 건드리지 않는다
    assert externals.analysis.started[0][1] == "2026-11-05"
