"""실패 표시·사유·재시도·시간 초과 (#19). 웹 API 경계에서 검증한다.

세션과 가짜 분석 서버는 구간 처리(#17) 테스트와 같은 것을 쓴다.
"""

from dataclasses import replace

from fakes import FakeAnalysisServer
from test_process_api import post, seed, states, swap_server

from krx_screener.analysis import AnalysisError


def use_timeout(client, seconds: int, poll: float = 0.001):
    """분석을 기다리는 최대 시간을 줄인다 (테스트가 실제로 기다리지 않도록 확인 간격도 줄인다)."""
    deps = client.app.state.deps
    analysis = replace(deps.config.analysis, timeout_seconds=seconds, poll_interval_seconds=poll)
    client.app.state.deps = replace(deps, config=replace(deps.config, analysis=analysis))


# --- 시간 초과 ---


def test_a_timeout_marks_the_stock_failed_with_a_reason_that_says_the_server_may_still_run(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(polls_before_done=10_000))  # 끝나지 않는 분석
    use_timeout(client, 0)  # 첫 확인에서 바로 시간 초과
    post(client, session.id, offset=0, count=1)
    item = repo.get_item(session.id, "000010")
    assert item.state == "failed"
    assert "기다리기를 멈췄습니다" in item.error_message
    assert "서버에서는 계속 실행 중일 수 있음" in item.error_message  # 서버에는 취소 기능이 없다
    assert item.decision == ""


def test_a_timeout_does_not_stop_the_other_stocks(client, repo):
    class SlowFirst(FakeAnalysisServer):
        def snapshot(self, run_id):
            if run_id == "run-000010":
                return {"status": "running"}  # 이 종목만 끝나지 않는다
            return super().snapshot(run_id)

    session = seed(repo)
    swap_server(client, SlowFirst())
    use_timeout(client, 0)
    post(client, session.id, offset=0, count=2)
    assert states(repo, session) == {
        "000010": "failed", "000020": "done", "000030": "pending", "000040": "pending", "000050": "pending",
    }


# --- 사유 ---


def test_a_server_error_in_the_middle_is_recorded_as_a_korean_reason(client, repo):
    class Restarted(FakeAnalysisServer):
        def snapshot(self, run_id):
            raise AnalysisError("서버가 이 분석을 찾지 못합니다 (서버가 재시작됐을 수 있음).")

    session = seed(repo)
    swap_server(client, Restarted())
    post(client, session.id, offset=0, count=1)
    item = repo.get_item(session.id, "000010")
    assert (item.state, item.error_message) == ("failed", "서버가 이 분석을 찾지 못합니다 (서버가 재시작됐을 수 있음).")


def test_the_failure_reason_and_count_show_in_the_session_view(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(errors=["000020"]))
    post(client, session.id, offset=0, count=2)
    body = client.get(f"/api/sessions/{session.id}").json()
    assert body["counts"] == {"pending": 3, "running": 0, "done": 1, "failed": 1}
    failed = body["items"][1]
    assert (failed["state"], failed["error_message"]) == ("failed", "가짜 서버 오류")
    assert body["items"][0]["error_message"] == ""


def test_a_code_the_server_does_not_accept_is_failed_with_a_reason_and_never_sent(client, repo, externals):
    session = seed(repo, codes=["000010", "0126Z0", "000030"])  # 영숫자 코드는 서버가 거절한다
    post(client, session.id, offset=0, count=3)
    assert [s[0] for s in externals.analysis.started] == ["000010", "000030"]
    item = repo.get_item(session.id, "0126Z0")
    assert item.state == "failed"
    assert "숫자 6자리" in item.error_message
    assert states(repo, session)["000030"] == "done"  # 나머지는 계속 간다


# --- 재시도 ---


def test_a_timed_out_stock_is_retried_and_the_reason_is_cleared(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(polls_before_done=10_000))
    use_timeout(client, 0)
    post(client, session.id, offset=0, count=1)
    assert states(repo, session)["000010"] == "failed"
    swap_server(client, FakeAnalysisServer(decisions={"000010": "Buy"}))
    assert post(client, session.id, offset=0, count=1).json() == {"started": True, "queued": 1}
    item = repo.get_item(session.id, "000010")
    assert (item.state, item.decision, item.error_message) == ("done", "Buy", "")


def test_only_the_failed_stocks_are_sent_again_when_the_range_is_called_again(client, repo, externals):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(errors=["000020"]))
    post(client, session.id, offset=0, count=3)
    server = swap_server(client, FakeAnalysisServer())
    assert post(client, session.id, offset=0, count=3).json() == {"started": True, "queued": 1}
    assert [s[0] for s in server.started] == ["000020"]  # 끝난 종목은 다시 보내지 않는다


# --- 기존 등급 보존 ---


def graded_then_failed(repo, session, code="000010"):
    """등급이 있던 종목이 다시 분석하다 실패해 failed 가 된 상태를 만든다."""
    repo.mark_done(session.id, code, decision="Buy", score=1.5, agents_rated=8, agents_total=10, report_id="000010_20261105_090000")
    repo.save_agent_ratings(session.id, code, {"market": "Buy", "news": "Hold"})
    repo.mark_failed(session.id, code, "이전 재분석 실패")


def test_a_failed_reanalysis_keeps_the_grade_that_was_already_there(client, repo):
    session = seed(repo)
    graded_then_failed(repo, session)
    swap_server(client, FakeAnalysisServer(errors=["000010"]))
    post(client, session.id, offset=0, count=1)
    item = repo.get_item(session.id, "000010")
    assert item.state == "failed"
    assert item.error_message == "가짜 서버 오류"  # 사유는 새 사유로 바뀐다
    assert (item.decision, item.score, item.agents_rated, item.agents_total) == ("Buy", 1.5, 8, 10)
    assert item.report_id == "000010_20261105_090000"
    assert repo.agent_ratings(session.id, "000010") == {"market": "Buy", "news": "Hold"}


def test_a_timed_out_reanalysis_keeps_the_grade_too(client, repo):
    session = seed(repo)
    graded_then_failed(repo, session)
    swap_server(client, FakeAnalysisServer(polls_before_done=10_000))
    use_timeout(client, 0)
    post(client, session.id, offset=0, count=1)
    item = repo.get_item(session.id, "000010")
    assert (item.state, item.decision, item.score) == ("failed", "Buy", 1.5)


def test_the_old_grade_stays_visible_while_the_reanalysis_is_running(client, repo):
    seen = []

    class Watching(FakeAnalysisServer):
        def snapshot(self, run_id):
            seen.append(repo.get_item(session.id, "000010"))
            return super().snapshot(run_id)

    session = seed(repo)
    graded_then_failed(repo, session)
    swap_server(client, Watching())
    post(client, session.id, offset=0, count=1)
    assert (seen[0].state, seen[0].decision, seen[0].score) == ("running", "Buy", 1.5)


def test_a_successful_reanalysis_replaces_the_old_grade(client, repo):
    session = seed(repo)
    graded_then_failed(repo, session)
    swap_server(client, FakeAnalysisServer(decisions={"000010": "Sell"}))
    post(client, session.id, offset=0, count=1)
    item = repo.get_item(session.id, "000010")
    assert (item.state, item.decision, item.error_message) == ("done", "Sell", "")
