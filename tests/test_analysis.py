import json

import pytest
import requests

from krx_screener.analysis import (
    DONE,
    FAILED,
    REVIEW,
    SKIPPED,
    TIMEOUT,
    AnalysisClient,
    AnalysisError,
    AnalysisOutcome,
    normalize_rating,
    rating_label,
    run_one,
    server_accepts,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Buy", "Buy"),
        ("overweight", "Overweight"),
        (" HOLD ", "Hold"),
        ("매수", "Buy"),
        ("비중 확대", "Overweight"),
        ("비중 축소", "Underweight"),
        ("매도", "Sell"),
        ("REVIEW", REVIEW),
        ("강력 매수", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_rating(raw, expected):
    assert normalize_rating(raw) == expected


def test_rating_label_and_server_code_rule():
    assert rating_label("Overweight") == "비중 확대"
    assert rating_label(REVIEW) == REVIEW
    assert rating_label(None) == ""
    assert server_accepts("005930")
    assert not server_accepts("0126Z0")
    assert not server_accepts("00680K")
    assert not server_accepts("5930")


class FakeClient:
    """snapshot 을 미리 정한 순서대로 돌려주는 가짜 클라이언트."""

    def __init__(self, snapshots, start_error=None):
        self.snapshots = list(snapshots)
        self.start_error = start_error
        self.started: list[tuple] = []

    def start(self, ticker, trade_date, effort):
        if self.start_error:
            raise self.start_error
        self.started.append((ticker, trade_date, effort))
        return "run-1"

    def snapshot(self, run_id):
        item = self.snapshots.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    report = "005930_20261106_172435"  # 서버가 알려주는 보고서 번호 (기본). Exception 이면 조회가 실패한다.

    def report_id(self, run_id):
        if isinstance(self.report, Exception):
            raise self.report
        return self.report


class FakeTime:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def clock(self):
        return self.now


def _run(client, code="005930", timeout=100, poll=5):
    t = FakeTime()
    return run_one(client, code, "삼성전자", "2026-11-05", "low", timeout, poll, t.sleep, t.clock)


def test_polls_until_done_and_reads_rating():
    client = FakeClient(
        [{"status": "pending"}, {"status": "running"}, {"status": "done", "decision": "Overweight"}]
    )
    outcome = _run(client)
    assert (outcome.status, outcome.decision, outcome.run_id) == (DONE, "Overweight", "run-1")
    assert outcome.is_target
    assert outcome.elapsed_seconds == 10  # 두 번 기다림 (5초 × 2)
    assert client.started == [("005930", "2026-11-05", "low")]


@pytest.mark.parametrize(
    ("decision", "target"),
    [("Buy", True), ("Overweight", True), ("Hold", False), ("Underweight", False), ("Sell", False), ("매수", True)],
)
def test_only_buy_and_overweight_are_targets(decision, target):
    outcome = _run(FakeClient([{"status": "done", "decision": decision}]))
    assert outcome.status == DONE
    assert outcome.is_target is target


def test_unreadable_decision_becomes_review_not_target():
    outcome = _run(FakeClient([{"status": "done", "decision": "아주 좋음"}]))
    assert outcome.status == DONE
    assert outcome.decision == REVIEW
    assert not outcome.is_target
    assert "아주 좋음" in outcome.message

    missing = _run(FakeClient([{"status": "done", "decision": None}]))
    assert missing.decision == REVIEW


def test_server_error_is_a_failed_outcome_with_message():
    outcome = _run(FakeClient([{"status": "running"}, {"status": "error", "error": "claude 호출 실패"}]))
    assert outcome.status == FAILED
    assert "claude 호출 실패" in outcome.message
    assert not outcome.is_target


def test_timeout_stops_waiting_and_says_server_may_still_run():
    outcome = _run(FakeClient([{"status": "running"}] * 50), timeout=20, poll=5)
    assert outcome.status == TIMEOUT
    assert outcome.run_id == "run-1"
    assert "계속 실행 중" in outcome.message
    assert outcome.elapsed_seconds >= 20


def test_start_rejection_and_lost_run_are_failed_outcomes():
    rejected = _run(FakeClient([], start_error=AnalysisError("분석 시작을 거절당했습니다 (HTTP 422)")))
    assert rejected.status == FAILED
    assert rejected.run_id == ""
    assert "422" in rejected.message

    lost = _run(FakeClient([{"status": "running"}, AnalysisError("서버가 이 분석을 찾지 못합니다")]))
    assert lost.status == FAILED
    assert lost.run_id == "run-1"


def test_alphanumeric_code_is_skipped_without_calling_the_server():
    client = FakeClient([])
    outcome = _run(client, code="0126Z0")
    assert outcome.status == SKIPPED
    assert client.started == []
    assert not outcome.is_target


# --- AnalysisClient (HTTP) ---


class _Resp:
    def __init__(self, status=200, body=None, json_error=False, lines=None):
        self.status_code = status
        self._body = body
        self._json_error = json_error
        self._lines = lines
        self.closed = False

    def iter_lines(self, decode_unicode=False):
        # 실제 requests 는 decode_unicode 를 주지 않으면 bytes 를 돌려준다
        for line in self._lines:
            if isinstance(line, Exception):
                raise line
            yield line

    def close(self):
        self.closed = True

    def json(self):
        if self._json_error:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, timeout=None, json=None, stream=False):
        self.calls.append((method, url, json))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(*responses):
    session = FakeSession(responses)
    return AnalysisClient("http://127.0.0.1:8000/", session=session), session


def test_start_posts_expected_body_and_returns_run_id():
    client, session = _client(_Resp(201, {"run_id": "abc"}))
    assert client.start("005930", "2026-11-05", "low") == "abc"
    method, url, body = session.calls[0]
    assert (method, url) == ("POST", "http://127.0.0.1:8000/api/runs")
    assert body == {"ticker": "005930", "trade_date": "2026-11-05", "effort": "low"}


@pytest.mark.parametrize(
    ("resp", "message"),
    [
        (_Resp(422, {"detail": "ticker pattern"}), "HTTP 422"),
        (_Resp(500, json_error=True), "HTTP 500"),
        (_Resp(201, {"nope": 1}), "run_id"),
        (_Resp(201, json_error=True), "JSON"),
        (_Resp(201, ["x"]), "구조"),
    ],
)
def test_start_errors(resp, message):
    client, _ = _client(resp)
    with pytest.raises(AnalysisError, match=message):
        client.start("005930", "2026-11-05", "low")


def test_connection_failure_is_reported_without_leaking_details():
    client, _ = _client(requests.ConnectionError("secret-host refused"))
    with pytest.raises(AnalysisError, match="연결하지 못했습니다") as info:
        client.check_health()
    assert "secret-host" not in str(info.value)


def test_health_and_snapshot_errors():
    client, _ = _client(_Resp(503))
    with pytest.raises(AnalysisError, match="HTTP 503"):
        client.check_health()
    client, _ = _client(_Resp(404))
    with pytest.raises(AnalysisError, match="재시작"):
        client.snapshot("gone")
    client, _ = _client(_Resp(500))
    with pytest.raises(AnalysisError, match="HTTP 500"):
        client.snapshot("x")
    client, _ = _client(_Resp(200, {"status": "done"}))
    assert client.snapshot("x") == {"status": "done"}


def test_outcome_target_property_requires_done():
    assert AnalysisOutcome("1", "a", DONE, decision="Buy").is_target
    assert not AnalysisOutcome("1", "a", FAILED, decision="Buy").is_target


# --- 보고서 번호 (#20) ---

REPORT = "028050_20261106_172435"


def _events(*events, as_bytes=False):
    """서버의 진행 이벤트 스트림(SSE) 줄들. 이벤트마다 ``event:`` 줄, ``data:`` 줄, 빈 줄."""
    lines = []
    for event in events:
        lines += [f"event: {event['type']}", "data: " + json.dumps(event, ensure_ascii=False), ""]
    return [line.encode() for line in lines] if as_bytes else lines


@pytest.mark.parametrize("as_bytes", [False, True])
def test_report_id_is_read_from_the_report_saved_event(as_bytes):
    stream = _events(
        {"type": "run_start"},
        {"type": "node_complete", "node": "market"},
        {"type": "report_saved", "report_id": REPORT},
        {"type": "done", "decision": "Buy"},
        as_bytes=as_bytes,
    )
    client, session = _client(_Resp(200, lines=stream))
    assert client.report_id("abc") == REPORT
    assert session.calls[0][:2] == ("GET", "http://127.0.0.1:8000/api/runs/abc/events")


def test_the_event_stream_is_closed_after_reading():
    resp = _Resp(200, lines=_events({"type": "report_saved", "report_id": REPORT}, {"type": "done"}))
    client, _ = _client(resp)
    client.report_id("abc")
    assert resp.closed


def test_report_id_is_none_when_the_run_finished_without_saving_a_report():
    client, _ = _client(_Resp(200, lines=_events({"type": "run_start"}, {"type": "error", "message": "x"})))
    assert client.report_id("abc") is None


def test_report_id_skips_garbled_lines_and_keeps_reading():
    lines = [": ping", "data: {깨진 JSON", "", *_events({"type": "report_saved", "report_id": REPORT})]
    client, _ = _client(_Resp(200, lines=lines))
    assert client.report_id("abc") == REPORT


@pytest.mark.parametrize("bad", ["", "../../etc", "ABC_20261106_172435", "028050_2026_1", None, 123])
def test_a_report_id_in_an_unexpected_shape_is_not_trusted(bad):
    client, _ = _client(_Resp(200, lines=_events({"type": "report_saved", "report_id": bad}, {"type": "done"})))
    assert client.report_id("abc") is None


def test_report_id_errors_are_korean_analysis_errors():
    client, _ = _client(_Resp(404, {"detail": "run not found"}))
    with pytest.raises(AnalysisError, match="HTTP 404"):
        client.report_id("abc")
    client, _ = _client(requests.ConnectionError("secret-host refused"))
    with pytest.raises(AnalysisError, match="연결하지 못했습니다"):
        client.report_id("abc")
    client, _ = _client(_Resp(200, lines=[requests.exceptions.ChunkedEncodingError("끊김")]))
    with pytest.raises(AnalysisError, match="보고서 번호"):
        client.report_id("abc")


def test_a_finished_analysis_carries_the_report_id():
    outcome = _run(FakeClient([{"status": "done", "decision": "Buy"}]))
    assert (outcome.status, outcome.report_id) == (DONE, "005930_20261106_172435")


def test_a_missing_report_id_does_not_fail_the_analysis():
    for report in (None, AnalysisError("보고서 번호를 읽지 못했습니다.")):
        client = FakeClient([{"status": "done", "decision": "Buy"}])
        client.report = report
        outcome = _run(client)
        assert (outcome.status, outcome.decision, outcome.report_id) == (DONE, "Buy", "")


def test_the_report_id_is_not_asked_for_when_the_analysis_did_not_finish():
    client = FakeClient([{"status": "error", "error": "x"}])
    client.report = AnalysisError("호출되면 안 된다")
    assert _run(client).status == FAILED


def test_report_id_is_read_from_a_real_http_event_stream():
    """가짜 응답이 아니라 진짜 ``requests`` 로 읽는다. 서버의 text/event-stream 은 charset 이 없고 한글이 그대로 들어 있다."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    body = "".join(
        f"event: {e['type']}\r\ndata: {json.dumps(e, ensure_ascii=False)}\r\n\r\n"
        for e in (
            {"type": "run_start"},
            {"type": "node_complete", "node": "market", "content": "한글 본문"},
            {"type": "report_saved", "report_id": REPORT},
            {"type": "done", "decision": "Buy"},
        )
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")  # charset 없음 (서버와 같다)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = AnalysisClient(f"http://127.0.0.1:{server.server_port}", request_timeout=5)
        assert client.report_id("abc") == REPORT
    finally:
        server.shutdown()
        server.server_close()
