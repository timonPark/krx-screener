"""세션 단위 동시 처리 잠금 (#18). 웹 API 경계와 저장소의 잠금 동작을 검증한다.

잠금은 PostgreSQL 의 advisory lock 이다. 처리를 하는 연결이 잠금을 쥐고, 연결이 끊기면 DB 가 잠금을 푼다.
"""

from datetime import date

from fakes import FakeAnalysisServer
from test_process_api import post, seed, states, swap_server

BUSY = {"detail": "이미 처리 중입니다."}


# --- 저장소 ---


def test_a_session_can_be_locked_by_only_one_connection_at_a_time(repo, hold_lock):
    session = seed(repo)
    assert repo.is_locked(session.id) is False
    holder = hold_lock(session.id)
    assert repo.is_locked(session.id) is True
    assert repo.try_lock(session.id) is False  # 다른 연결은 쥘 수 없다
    holder.close()
    assert repo.is_locked(session.id) is False
    assert repo.try_lock(session.id) is True  # 풀린 뒤에는 쥘 수 있다


def test_locking_one_session_does_not_lock_another(repo, hold_lock):
    first, second = seed(repo), seed(repo, date(2026, 11, 4))
    hold_lock(first.id)
    assert repo.is_locked(second.id) is False
    assert repo.try_lock(second.id) is True


# --- 처리 API ---


def test_processing_a_session_that_is_being_processed_is_a_409_and_nothing_is_sent(client, repo, externals, hold_lock):
    session = seed(repo)
    hold_lock(session.id)
    response = post(client, session.id, offset=0, count=2)
    assert (response.status_code, response.json()) == (409, BUSY)
    assert externals.analysis.started == []
    assert set(states(repo, session).values()) == {"pending"}


def test_another_session_is_not_blocked(client, repo, externals, hold_lock):
    busy, free = seed(repo), seed(repo, date(2026, 11, 4))
    hold_lock(busy.id)
    assert post(client, free.id, offset=0, count=1).status_code == 202
    assert states(repo, free)["000010"] == "done"


def test_when_the_processing_connection_drops_the_session_can_be_processed_again(client, repo, hold_lock):
    session = seed(repo)
    holder = hold_lock(session.id)
    assert post(client, session.id, offset=0, count=1).status_code == 409
    holder.close()  # 처리하던 연결이 끊겼다
    assert post(client, session.id, offset=0, count=1).json() == {"started": True, "queued": 1}
    assert states(repo, session)["000010"] == "done"


def test_the_lock_is_held_while_processing_and_a_second_call_gets_409(client, repo):
    seen = []

    class Watching(FakeAnalysisServer):
        def snapshot(self, run_id):
            if not seen:  # 첫 종목을 분석하는 동안 한 번만 본다
                seen.append((repo.is_locked(session.id), post(client, session.id, offset=0, count=2)))
            return super().snapshot(run_id)

    session = seed(repo)
    swap_server(client, Watching())
    post(client, session.id, offset=0, count=2)
    locked, second = seen[0]
    assert locked is True
    assert (second.status_code, second.json()) == (409, BUSY)


def test_the_lock_is_released_when_processing_finishes(client, repo):
    session = seed(repo)
    post(client, session.id, offset=0, count=2)
    assert repo.is_locked(session.id) is False
    assert post(client, session.id, offset=2, count=1).status_code == 202  # 바로 다시 처리할 수 있다


def test_the_lock_is_released_even_when_every_stock_fails(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(errors=["000010", "000020"]))
    post(client, session.id, offset=0, count=2)
    assert repo.is_locked(session.id) is False


def test_a_request_that_starts_nothing_leaves_no_lock(client, repo):
    session = seed(repo)
    assert post(client, session.id, offset=5, count=1).status_code == 422  # 오프셋이 목록 밖
    assert repo.is_locked(session.id) is False
    for code in ("000010", "000020"):
        repo.mark_done(session.id, code, decision="Hold", score=0.0, agents_rated=10, agents_total=10)
    assert post(client, session.id, offset=0, count=2).status_code == 200  # 보낼 것이 없다
    assert repo.is_locked(session.id) is False


def test_when_the_analysis_server_is_down_the_lock_is_released(client, repo):
    session = seed(repo)
    swap_server(client, FakeAnalysisServer(health_error="TradingAgents-KR 서버에 연결하지 못했습니다"))
    assert post(client, session.id, offset=0, count=1).status_code == 503
    assert repo.is_locked(session.id) is False
    swap_server(client, FakeAnalysisServer())
    assert post(client, session.id, offset=0, count=1).status_code == 202


# --- 조회 ---


def test_the_session_view_reports_the_lock_as_processing(client, repo, hold_lock):
    session = seed(repo)
    assert client.get(f"/api/sessions/{session.id}").json()["processing"] is False
    holder = hold_lock(session.id)
    assert client.get(f"/api/sessions/{session.id}").json()["processing"] is True
    holder.close()
    assert client.get(f"/api/sessions/{session.id}").json()["processing"] is False


def test_a_stock_left_running_by_a_crash_does_not_make_the_session_look_busy(client, repo):
    session = seed(repo)
    repo.mark_running(session.id, "000010")  # 처리하던 프로세스가 죽어서 남은 표시
    body = client.get(f"/api/sessions/{session.id}").json()
    assert body["counts"]["running"] == 1
    assert body["processing"] is False  # 잠금이 없으니 처리 중이 아니다
    assert post(client, session.id, offset=0, count=1).status_code == 202  # 다시 처리할 수 있다
    assert states(repo, session)["000010"] == "done"
