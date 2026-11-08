"""분석 상태 조회가 한 번 끊겨도 서버가 끝낸 분석을 실패로 기록하지 않는다 (#75).

2026-11-07 실제 서버로 116종목을 돌리는 동안 4건이 ``ConnectionError`` 로 실패로 기록됐는데, 서버에서는 4건 모두 분석이 끝나 있었다.
대기 루프가 상태 조회 한 번이 끊긴 것을 곧바로 "분석 실패" 로 취급했기 때문이다. 상태 조회(GET)만 몇 번 다시 시도한다.
분석 시작(POST)은 다시 시도하지 않는다: 서버가 분석을 하나 더 시작해 쿼터를 한 번 더 쓸 수 있다.
"""

import pytest
import requests
from test_analysis import REPORT, FakeSession, _events, _Resp, _run
from urllib3.exceptions import ProtocolError

from krx_screener.analysis import (
    DONE,
    FAILED,
    RETRY_WAIT_SECONDS,
    SNAPSHOT_RETRIES,
    AnalysisClient,
    AnalysisError,
)


def _reset() -> requests.ConnectionError:
    """서버가 막 닫은 연결에 요청을 보냈을 때 requests 가 내는 예외의 실제 모양 (keep-alive 경쟁)."""
    return requests.ConnectionError(
        ProtocolError("Connection aborted.", ConnectionResetError(54, "Connection reset by peer secret-host"))
    )


def _client(*responses):
    session = FakeSession(responses)
    sleeps: list[float] = []
    client = AnalysisClient("http://127.0.0.1:8000/", session=session, sleep=sleeps.append)
    return client, session, sleeps


# --- 상태 조회: 끊기면 다시 시도한다 ---


def test_a_dropped_connection_while_polling_is_retried_and_the_next_answer_is_used():
    client, session, sleeps = _client(_reset(), _Resp(200, {"status": "done", "decision": "Hold"}))
    assert client.snapshot("run-1") == {"status": "done", "decision": "Hold"}
    assert [(method, url) for method, url, _ in session.calls] == [("GET", "http://127.0.0.1:8000/api/runs/run-1")] * 2
    assert sleeps == [RETRY_WAIT_SECONDS]  # 다시 시도하기 전에 잠깐 기다린다


def test_it_keeps_retrying_up_to_the_limit_and_then_succeeds():
    client, session, sleeps = _client(*[_reset()] * SNAPSHOT_RETRIES, _Resp(200, {"status": "running"}))
    assert client.snapshot("run-1") == {"status": "running"}
    assert len(session.calls) == SNAPSHOT_RETRIES + 1
    assert sleeps == [RETRY_WAIT_SECONDS] * SNAPSHOT_RETRIES


def test_it_gives_up_after_the_retries_and_the_message_says_the_server_may_have_finished():
    client, session, _ = _client(*[_reset()] * (SNAPSHOT_RETRIES + 1))
    with pytest.raises(AnalysisError) as info:
        client.snapshot("run-1")
    assert len(session.calls) == SNAPSHOT_RETRIES + 1  # 더 시도하지 않는다
    message = str(info.value)
    assert "연결하지 못했습니다" in message
    assert "끝났을 수 있습니다" in message  # 서버에서는 분석이 계속되거나 끝났을 수 있음을 알린다
    assert f"{SNAPSHOT_RETRIES}번" in message
    assert "ConnectionResetError" in message  # 다음에 원인을 가릴 수 있게 예외의 속을 남긴다
    assert "secret-host" not in message  # 예외 글에 든 주소는 남기지 않는다 (종류 이름만)


@pytest.mark.parametrize(
    "answer",
    [_Resp(404), _Resp(500), _Resp(200, json_error=True), _Resp(200, ["not a dict"])],
    ids=["404", "500", "not-json", "not-a-dict"],
)
def test_answers_from_the_server_are_not_retried_only_dropped_connections_are(answer):
    client, session, sleeps = _client(answer)
    with pytest.raises(AnalysisError):
        client.snapshot("run-1")
    assert len(session.calls) == 1
    assert sleeps == []


# --- 분석 시작은 다시 시도하지 않는다 (서버가 분석을 하나 더 시작해 쿼터를 한 번 더 쓸 수 있다) ---


def test_starting_an_analysis_is_never_retried():
    client, session, sleeps = _client(_reset(), _Resp(201, {"run_id": "would-be-second-run"}))
    with pytest.raises(AnalysisError, match="연결하지 못했습니다"):
        client.start("005930", "2026-11-05", "low")
    assert [method for method, _, _ in session.calls] == ["POST"]
    assert sleeps == []


# --- 사유에 예외의 속을 남긴다 ---


def test_the_error_names_the_underlying_exceptions_without_leaking_their_text():
    client, _, _ = _client(_reset())
    with pytest.raises(AnalysisError) as info:
        client.check_health()
    message = str(info.value)
    assert message.endswith("ConnectionError (ProtocolError → ConnectionResetError)")
    assert "secret-host" not in message


def test_a_plain_connection_error_keeps_the_old_short_message():
    client, _, _ = _client(requests.ConnectionError("secret-host refused"))
    with pytest.raises(AnalysisError) as info:
        client.check_health()
    assert str(info.value) == "TradingAgents-KR 서버에 연결하지 못했습니다 (http://127.0.0.1:8000): ConnectionError"


# --- run_one: 2026-11-07 의 실제 사고 ---


def _full_run(*poll_answers):
    """목록(도는 분석 없음) → 시작 → 상태 조회들 → 보고서 번호 이벤트 순서로 서버가 답한다."""
    return (
        _Resp(200, []),
        _Resp(201, {"run_id": "run-1"}),
        *poll_answers,
        _Resp(200, lines=_events({"type": "report_saved", "report_id": REPORT}, {"type": "done"})),
    )


def test_a_connection_dropped_in_the_middle_of_polling_no_longer_fails_a_finished_analysis():
    client, session, sleeps = _client(
        *_full_run(
            _Resp(200, {"status": "running"}),
            _reset(),  # 이전에는 여기서 바로 실패로 기록됐다
            _Resp(200, {"status": "done", "decision": "Hold"}),
        )
    )
    outcome = _run(client)
    assert (outcome.status, outcome.decision, outcome.run_id) == (DONE, "Hold", "run-1")
    assert [method for method, _, _ in session.calls].count("POST") == 1  # 분석은 한 번만 보냈다 (쿼터)
    assert sleeps == [RETRY_WAIT_SECONDS]


def test_a_server_that_stays_unreachable_still_fails_with_the_run_id_and_a_clear_reason():
    client, _, _ = _client(*_full_run(*[_reset()] * (SNAPSHOT_RETRIES + 1)))
    outcome = _run(client)
    assert (outcome.status, outcome.run_id) == (FAILED, "run-1")
    assert "끝났을 수 있습니다" in outcome.message
