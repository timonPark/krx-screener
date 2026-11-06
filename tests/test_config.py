import copy
from pathlib import Path

import pytest

from krx_screener.config import ConfigError, load_config, parse_config

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


def _raw() -> dict:
    return copy.deepcopy(load_config(REPO_CONFIG).raw)


def test_repo_config_matches_agreed_rules():
    cfg = load_config(REPO_CONFIG)
    assert cfg.exclude == {"preferred", "spac", "managed", "zero_volume", "new_listing", "reit"}
    # 투자주의환기종목·외국기업은 제외하지 않고 표시만 한다
    assert "caution" not in cfg.exclude
    assert "foreign" not in cfg.exclude
    f = cfg.filters
    assert f.min_avg_trading_value_krw == 1_000_000_000
    assert f.avg_trading_value_window == 20
    assert f.price_above_ma == (20, 60)
    assert (f.rsi_period, f.rsi_max) == (14, 70.0)
    assert f.required_rows == 60
    # 수급은 표시만 하고 후보를 거르지 않는다
    assert (cfg.flow.mode, cfg.flow.window_days) == ("mark", 5)


def test_flow_section_is_optional_and_defaults_to_mark():
    raw = _raw()
    raw.pop("flow")
    cfg = parse_config(raw)
    assert (cfg.flow.mode, cfg.flow.window_days) == ("mark", 5)


def test_flow_filter_mode_is_accepted():
    raw = _raw()
    raw["flow"] = {"mode": "filter", "window_days": 10}
    cfg = parse_config(raw)
    assert (cfg.flow.mode, cfg.flow.window_days) == ("filter", 10)


@pytest.mark.parametrize(
    ("flow", "message"),
    [
        ({"mode": "drop"}, "flow.mode"),
        ({"mode": "mark", "window_days": 0}, "flow.window_days"),
        ({"mode": "mark", "window_days": 11}, "10 이하"),
        ({"mode": "mark", "window_days": True}, "flow.window_days"),
        (["mark"], "flow"),
    ],
)
def test_invalid_flow_config_is_rejected(flow, message):
    raw = _raw()
    raw["flow"] = flow
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_analysis_section_defaults_and_overrides():
    raw = _raw()
    raw.pop("analysis", None)
    cfg = parse_config(raw)
    assert cfg.analysis.base_url == "http://127.0.0.1:8000"
    assert (cfg.analysis.effort, cfg.analysis.timeout_seconds, cfg.analysis.poll_interval_seconds) == ("low", 1800, 5.0)

    raw["analysis"] = {"effort": "high", "timeout_seconds": 60, "poll_interval_seconds": 0.5}
    cfg = parse_config(raw)
    assert (cfg.analysis.effort, cfg.analysis.timeout_seconds, cfg.analysis.poll_interval_seconds) == ("high", 60, 0.5)
    assert cfg.analysis.base_url == "http://127.0.0.1:8000"  # 생략한 값은 기본값


@pytest.mark.parametrize(
    ("analysis", "message"),
    [
        ({"base_url": "127.0.0.1:8000"}, "base_url"),
        ({"base_url": 8000}, "base_url"),
        ({"effort": "turbo"}, "effort"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": True}, "timeout_seconds"),
        ({"poll_interval_seconds": 0}, "poll_interval_seconds"),
        ({"poll_interval_seconds": True}, "poll_interval_seconds"),
        ({"retries": 3}, "알 수 없는 항목"),
        (["low"], "analysis"),
    ],
)
def test_invalid_analysis_config_is_rejected(analysis, message):
    raw = _raw()
    raw["analysis"] = analysis
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_required_rows_covers_rsi_warmup():
    raw = _raw()
    raw["filters"]["price_above_ma"] = [5]
    raw["filters"]["avg_trading_value_window"] = 5
    raw["filters"]["rsi_period"] = 14
    assert parse_config(raw).filters.required_rows == 15


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r["universe"]["exclude"].update(typo=True), "알 수 없는 종류"),
        (lambda r: r["universe"]["exclude"].update(spac="yes"), "true/false"),
        (lambda r: r["filters"].update(rsi_max=0), "rsi_max"),
        (lambda r: r["filters"].update(rsi_max=101), "rsi_max"),
        (lambda r: r["filters"].update(rsi_max=True), "rsi_max"),
        (lambda r: r["filters"].update(rsi_period=0), "rsi_period"),
        (lambda r: r["filters"].update(avg_trading_value_window=True), "avg_trading_value_window"),
        (lambda r: r["filters"].update(price_above_ma=[]), "price_above_ma"),
        (lambda r: r["filters"].update(price_above_ma=[20, "x"]), "price_above_ma"),
        (lambda r: r["filters"].pop("rsi_max"), "빠진 항목"),
        (lambda r: r.pop("filters"), "filters"),
    ],
)
def test_invalid_config_is_rejected_with_korean_message(mutate, message):
    raw = _raw()
    mutate(raw)
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_non_mapping_config_is_rejected():
    with pytest.raises(ConfigError):
        parse_config(["not", "a", "mapping"])


def test_missing_and_broken_files(tmp_path):
    with pytest.raises(ConfigError, match="읽지 못했습니다"):
        load_config(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(bad)
