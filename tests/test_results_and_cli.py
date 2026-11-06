import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest
from helpers import falling_closes, make_history, make_listing, passing_closes
from typer.testing import CliRunner

from krx_screener import cli
from krx_screener import universe as uni
from krx_screener.config import load_config
from krx_screener.flow import FlowFetchError, apply_flow
from krx_screener.results import write_results
from krx_screener.screen import run_screen

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
CONFIG = load_config(REPO_CONFIG)
AS_OF = date(2026, 11, 6)
TODAY = date(2026, 11, 9)  # 월요일 → 기본 기준일은 직전 영업일인 11-06


class _Store:
    def get(self, code):
        return make_history(passing_closes(), 3_000_000, AS_OF)


def _result():
    classified = uni.classify(
        make_listing([{"Code": "000010", "Name": "통과전자"}, {"Code": "000025", "Name": "통과전자우"}]), AS_OF
    )
    return run_screen(classified, CONFIG, AS_OF, _Store())


def test_write_results_files_and_contents(tmp_path):
    out = write_results(_result(), CONFIG, tmp_path, datetime(2026, 11, 9, 9, 0, 0))
    assert out == tmp_path / "2026-11-06"

    raw = (out / "candidates.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # 엑셀용 BOM
    frame = pd.read_csv(out / "candidates.csv", dtype={"code": str})
    assert frame["code"].tolist() == ["000010"]
    assert {"name", "market", "close", "avg_trading_value_approx", "sma20", "sma60", "rsi", "is_caution", "is_foreign"} <= set(frame.columns)

    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert info["as_of"] == "2026-11-06"
    assert info["generated_at"] == "2026-11-09T09:00:00"
    assert info["funnel"] == {
        "listing_total": 2,
        "after_static_exclusion": 1,
        "zero_volume_excluded": 0,
        "undetermined": 0,
        "evaluated": 1,
        "candidates": 1,
    }
    assert "근사" in info["approximations"]["trading_value"]
    assert info["config"]["filters"]["min_avg_trading_value_krw"] == 1_000_000_000
    assert "rsi_max" not in info["config"]["filters"]  # RSI 조건은 두지 않는다
    assert info["static_excluded_by_kind"]["preferred"] == 1


def test_write_results_overwrites_same_day_and_handles_zero_candidates(tmp_path):
    classified = uni.classify(make_listing([{"Code": "000010", "Name": "하락중"}]), AS_OF)

    class Falling:
        def get(self, code):
            return make_history(falling_closes(), 3_000_000, AS_OF)

    empty = run_screen(classified, CONFIG, AS_OF, Falling())
    write_results(_result(), CONFIG, tmp_path, datetime(2026, 11, 9))
    out = write_results(empty, CONFIG, tmp_path, datetime(2026, 11, 9))
    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert info["funnel"]["candidates"] == 0
    assert (out / "candidates.csv").exists()


# --- run 명령 전체 흐름 (네트워크 없이 가짜 시세·목록) ---


class _Calls(list):
    """가짜 시세 조회 기록. ``flow_fail`` 에 넣은 종목코드는 수급 조회가 실패한다."""

    def __init__(self):
        super().__init__()
        self.flow_fail: set[str] = set()


def _naver_rows():
    days = pd.bdate_range(end="2026-11-06", periods=10)
    return [
        {"bizdate": d.strftime("%Y%m%d"), "foreignerPureBuyQuant": "+100", "organPureBuyQuant": "+50"}
        for d in reversed(days)
    ]


@pytest.fixture
def fake_world(monkeypatch):
    calls = _Calls()

    def fetch(code, start, end):
        calls.append(code)
        frame = make_history(passing_closes(), 3_000_000, end)
        frame["Open"] = 1.0
        return frame

    listing = make_listing(
        [
            {"Code": "000010", "Name": "통과전자"},
            {"Code": "000020", "Name": "통과화학"},
            {"Code": "000025", "Name": "통과화학우"},
            {"Code": "0126Z0", "Name": "영숫자코드"},
        ]
    )
    def naver(code):
        if code in calls.flow_fail:
            raise FlowFetchError("HTTP 503")
        return _naver_rows()

    monkeypatch.setattr(cli, "fdr_fetcher", fetch)
    monkeypatch.setattr(cli, "naver_trend", naver)
    monkeypatch.setattr(uni, "fetch_listing", lambda: listing)
    monkeypatch.setattr(cli, "_today_kst", lambda: TODAY)
    return calls


def _run(tmp_path, *extra):
    return CliRunner().invoke(
        cli.app,
        [
            "run", "--config", str(REPO_CONFIG), "--out", str(tmp_path / "out"),
            "--cache", str(tmp_path / "cache"), "--db", str(tmp_path / "q.db"), *extra,
        ],
    )


def test_run_command_writes_results_and_reuses_cache(tmp_path, fake_world):
    first = _run(tmp_path)
    assert first.exit_code == 0, first.output
    out = tmp_path / "out" / "2026-11-06"
    codes = pd.read_csv(out / "candidates.csv", dtype={"code": str})["code"].tolist()
    assert sorted(codes) == ["000010", "000020", "0126Z0"]  # 우선주는 제외, 영숫자 코드는 처리됨
    assert "후보" in first.output

    calls_after_first = list(fake_world)
    second = _run(tmp_path)
    assert second.exit_code == 0, second.output
    new_calls = fake_world[len(calls_after_first):]
    assert new_calls == ["005930"]  # 기준일 확인용 삼성전자만 조회, 종목 시세는 캐시에서 읽음

    refreshed = _run(tmp_path, "--refresh")
    assert refreshed.exit_code == 0
    assert {"000010", "000020", "0126Z0"} <= set(fake_world[len(calls_after_first) + 1:])


def test_run_command_explicit_date_and_limit(tmp_path, fake_world):
    result = _run(tmp_path, "--date", "2026-11-05", "--limit", "1")
    assert result.exit_code == 0, result.output
    frame = pd.read_csv(tmp_path / "out" / "2026-11-05" / "candidates.csv", dtype={"code": str})
    assert frame["code"].tolist() == ["000010"]


@pytest.mark.parametrize(
    ("args", "exit_code", "message"),
    [
        (["--date", "2026/11/06"], 2, "날짜 형식"),
        (["--date", "2026-11-10"], 1, "이후"),
        (["--date", "2026-11-07"], 1, "거래일이 아니"),  # 토요일
    ],
)
def test_run_command_input_errors(tmp_path, fake_world, args, exit_code, message):
    result = _run(tmp_path, *args)
    assert result.exit_code == exit_code
    assert message in result.output.replace("\n", "")


def test_run_command_bad_config(tmp_path, fake_world):
    bad = tmp_path / "bad.yaml"
    bad.write_text("filters: {}\n", encoding="utf-8")
    result = CliRunner().invoke(
        cli.app, ["run", "--config", str(bad), "--out", str(tmp_path / "out"), "--db", str(tmp_path / "q.db")]
    )
    assert result.exit_code == 2
    assert "설정" in result.output


def test_run_command_adds_flow_columns_and_keeps_unconfirmed(tmp_path, fake_world):
    fake_world.flow_fail.add("000020")
    result = _run(tmp_path)
    assert result.exit_code == 0, result.output
    out = tmp_path / "out" / "2026-11-06"

    frame = pd.read_csv(out / "candidates.csv", dtype={"code": str}).set_index("code")
    assert {"foreign_net_5d", "institution_net_5d", "flow_status"} <= set(frame.columns)
    assert frame.loc["000010", "flow_status"] == "양호"
    assert frame.loc["000010", "foreign_net_5d"] == 500
    # 수급 조회에 실패해도 후보에서 빠지지 않고 "미확인" 으로 남는다
    assert frame.loc["000020", "flow_status"] == "미확인"
    assert pd.isna(frame.loc["000020", "foreign_net_5d"])
    assert len(frame) == 3

    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert info["flow"]["mode"] == "mark"
    assert info["flow"]["status_counts"] == {"strong": 2, "partial": 0, "weak": 0, "unconfirmed": 1}
    assert info["flow"]["unconfirmed_by_reason"] == {"fetch_failed": 1}
    assert info["flow"]["unconfirmed"][0]["code"] == "000020"
    assert info["funnel"]["stage1_candidates"] == info["funnel"]["candidates"] == 3
    assert "수급 미확인 사유" in result.output.replace("\n", "")
    assert "조회 실패 1" in result.output.replace("\n", "")


def test_run_json_flow_section_for_filter_mode(tmp_path):
    classified = uni.classify(make_listing([{"Code": "000010", "Name": "통과전자"}]), AS_OF)
    result = run_screen(classified, CONFIG, AS_OF, _Store())
    weak = [{"bizdate": d.strftime("%Y%m%d"), "foreignerPureBuyQuant": "-1", "organPureBuyQuant": "-1"}
            for d in pd.bdate_range(end="2026-11-06", periods=6)]
    result.candidates, result.flow = apply_flow(result.candidates, "filter", 5, AS_OF, lambda c: weak)
    out = write_results(result, CONFIG, tmp_path, datetime(2026, 11, 9))
    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert info["funnel"]["stage1_candidates"] == 1
    assert info["funnel"]["candidates"] == 0
    assert info["flow"]["dropped_by_filter"] == 1
    assert "미확인" in info["approximations"]["flow"]
