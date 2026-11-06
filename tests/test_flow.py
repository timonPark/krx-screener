import re
from datetime import date

import pandas as pd
import pytest
import requests

from krx_screener import flow
from krx_screener.flow import (
    FETCH_FAILED,
    NO_AS_OF_ROW,
    NO_DATA,
    PARTIAL,
    SHORT_HISTORY,
    STRONG,
    UNCONFIRMED,
    UNREADABLE,
    WEAK,
    FlowFetchError,
    apply_flow,
    classify_status,
    fetch_flow,
    naver_trend,
    read_flow,
)

AS_OF = date(2026, 11, 5)
DAYS = ["20261029", "20261030", "20261102", "20261103", "20261104", "20261105"]


def _num(value: int) -> str:
    return f"{value:+,}" if value else "0"


def _rows(foreign, institution, days=DAYS):
    """네이버 응답 모양의 행 (최신이 먼저)."""
    return [
        {"bizdate": d, "foreignerPureBuyQuant": _num(f), "organPureBuyQuant": _num(o)}
        for d, f, o in zip(reversed(days), reversed(foreign), reversed(institution), strict=True)
    ]


def test_classify_status_truth_table():
    assert classify_status(1, 1) == STRONG
    assert classify_status(1, 0) == PARTIAL
    assert classify_status(0, 5) == PARTIAL
    assert classify_status(-1, -1) == WEAK
    assert classify_status(0, 0) == WEAK


def test_strong_partial_and_weak_use_only_the_window():
    # 가장 오래된 하루(10-29)는 5일 창 밖이라 합계에 들어가지 않는다
    rows = _rows([-9_999, 100, 100, 100, 100, 100], [-9_999, 50, 50, 50, 50, 50])
    reading = read_flow(rows, AS_OF, 5)
    assert (reading.status, reading.foreign, reading.institution) == (STRONG, 500, 250)

    assert read_flow(_rows([10] * 6, [-10] * 6), AS_OF, 5).status == PARTIAL
    assert read_flow(_rows([-10] * 6, [-10] * 6), AS_OF, 5).status == WEAK
    assert read_flow(_rows([0] * 6, [0] * 6), AS_OF, 5).status == WEAK


def test_number_formats_are_parsed():
    rows = _rows([9_822, -21_196, 0, 5, 5, 5], [1] * 6)
    assert read_flow(rows, AS_OF, 5).foreign == -21_196 + 0 + 5 + 5 + 5


def test_rows_after_as_of_are_ignored():
    rows = _rows([100] * 6, [100] * 6) + [
        {"bizdate": "20261106", "foreignerPureBuyQuant": "-999,999", "organPureBuyQuant": "-999,999"}
    ]
    reading = read_flow(rows, AS_OF, 5)
    assert (reading.status, reading.foreign) == (STRONG, 500)


@pytest.mark.parametrize(
    ("rows", "as_of", "code"),
    [
        ([], AS_OF, NO_DATA),  # 없는 종목코드는 HTTP 200 + 빈 목록
        (_rows([1] * 6, [1] * 6), date(2026, 10, 1), NO_DATA),  # 제공 구간보다 과거
        (_rows([1] * 6, [1] * 6), date(2026, 11, 6), NO_AS_OF_ROW),  # 기준일 행 없음
        (_rows([1] * 3, [1] * 3, DAYS[-3:]), AS_OF, SHORT_HISTORY),
        (
            [{"bizdate": "20261105", "foreignerPureBuyQuant": "N/A", "organPureBuyQuant": "1"}]
            + _rows([1] * 6, [1] * 6)[1:],
            AS_OF,
            UNREADABLE,
        ),
    ],
)
def test_unreadable_flow_is_unconfirmed_never_zero(rows, as_of, code):
    reading = read_flow(rows, as_of, 5)
    assert reading.status == UNCONFIRMED
    assert reading.reason_code == code
    assert reading.foreign is None
    assert reading.institution is None
    assert reading.reason


def test_fetch_flow_retries_once_then_gives_up():
    calls = []

    def flaky(code):
        calls.append(code)
        if len(calls) == 1:
            raise FlowFetchError("일시 오류")
        return _rows([1] * 6, [1] * 6)

    assert fetch_flow("000010", AS_OF, 5, flaky, retry_wait=0).status == STRONG
    assert len(calls) == 2

    def broken(code):
        raise FlowFetchError("HTTP 503")

    reading = fetch_flow("000010", AS_OF, 5, broken, retry_wait=0)
    assert reading.status == UNCONFIRMED
    assert reading.reason_code == FETCH_FAILED
    assert "HTTP 503" in reading.reason


def _candidates():
    return pd.DataFrame(
        {"code": ["000010", "000020", "000030", "000040"], "name": ["강한", "약한", "실패", "부분"]}
    )


def _fetcher(code):
    table = {
        "000010": _rows([10] * 6, [10] * 6),
        "000020": _rows([-10] * 6, [-10] * 6),
        "000040": _rows([10] * 6, [-10] * 6),
    }
    if code not in table:
        raise FlowFetchError("HTTP 500")  # 000030 은 조회 실패
    return table[code]


def test_mark_mode_keeps_every_candidate_and_adds_columns():
    out, stats = apply_flow(_candidates(), "mark", 5, AS_OF, _fetcher, workers=2, retry_wait=0)
    assert out["code"].tolist() == ["000010", "000020", "000030", "000040"]
    assert out["flow_status"].tolist() == ["양호", "약함", "미확인", "부분"]
    assert out["foreign_net_5d"].tolist()[:2] == [50, -50]
    assert pd.isna(out.loc[2, "foreign_net_5d"])
    assert pd.isna(out.loc[2, "institution_net_5d"])
    assert stats.status_counts == {STRONG: 1, PARTIAL: 1, WEAK: 1, UNCONFIRMED: 1}
    assert stats.unconfirmed_by_reason == {FETCH_FAILED: 1}
    assert stats.unconfirmed[0]["code"] == "000030"
    assert "HTTP 500" in stats.unconfirmed[0]["reason"]
    assert stats.dropped_by_filter == 0
    assert stats.stage1_candidates == 4


def test_filter_mode_drops_only_weak_not_unconfirmed():
    out, stats = apply_flow(_candidates(), "filter", 5, AS_OF, _fetcher, workers=2, retry_wait=0)
    assert out["code"].tolist() == ["000010", "000030", "000040"]
    assert stats.dropped_by_filter == 1
    assert stats.stage1_candidates == 4
    assert stats.status_counts[WEAK] == 1  # 탈락했어도 집계에는 남는다


def test_unconfirmed_is_written_as_empty_csv_cells_not_nan_text(tmp_path):
    out, _ = apply_flow(_candidates(), "mark", 5, AS_OF, _fetcher, workers=2, retry_wait=0)
    path = tmp_path / "c.csv"
    out.to_csv(path, index=False)
    text = path.read_text(encoding="utf-8")
    assert "nan" not in text.lower()
    assert "000030,실패,,,미확인" in text


def test_window_days_sets_column_names():
    out, _ = apply_flow(_candidates(), "mark", 3, AS_OF, _fetcher, retry_wait=0)
    assert {"foreign_net_3d", "institution_net_3d"} <= set(out.columns)


def test_no_candidates_is_handled():
    empty = pd.DataFrame()
    out, stats = apply_flow(empty, "filter", 5, AS_OF, _fetcher)
    assert out.empty
    assert stats.stage1_candidates == 0
    assert sum(stats.status_counts.values()) == 0


def test_progress_reports_every_candidate():
    seen = []
    apply_flow(_candidates(), "mark", 5, AS_OF, _fetcher, workers=2, retry_wait=0, progress=lambda d, t: seen.append((d, t)))
    assert [d for d, _ in seen] == [1, 2, 3, 4]
    assert seen[-1] == (4, 4)


# --- naver_trend: 네트워크 없이 requests.get 을 바꿔서 ---


class _Resp:
    def __init__(self, status=200, payload=None, json_error=False):
        self.status_code = status
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("no json")
        return self._payload


def test_naver_trend_returns_dict_rows(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        return _Resp(payload=[{"bizdate": "20261105"}, "junk", {"bizdate": "20261104"}])

    monkeypatch.setattr(requests, "get", fake_get)
    assert naver_trend("0126Z0") == [{"bizdate": "20261105"}, {"bizdate": "20261104"}]
    assert seen["url"].endswith("/0126Z0/trend")


@pytest.mark.parametrize(
    ("resp", "message"),
    [
        (_Resp(status=500), "HTTP 500"),
        (_Resp(json_error=True), "JSON"),
        (_Resp(payload={"error": "x"}), "구조"),
        (requests.ConnectionError("boom"), "요청 실패 (ConnectionError)"),
    ],
)
def test_naver_trend_errors_become_flow_fetch_error(monkeypatch, resp, message):
    def fake_get(url, **kwargs):
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(FlowFetchError, match=re.escape(message)):
        naver_trend("005930")


@pytest.mark.parametrize("code", ["../x", "5930", "", "00593O!"])
def test_naver_trend_rejects_bad_code_without_request(monkeypatch, code):
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("요청을 보내면 안 됩니다"))
    with pytest.raises(FlowFetchError, match="형식"):
        naver_trend(code)


def test_reason_labels_cover_every_reason_code():
    for code in (FETCH_FAILED, NO_DATA, NO_AS_OF_ROW, SHORT_HISTORY, UNREADABLE):
        assert code in flow.REASON_LABELS
