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
