"""설정 파일(config.yaml) 읽기와 검증."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# 유니버스에서 제외 여부를 고를 수 있는 종류. 값은 universe.py 의 ``is_<key>`` 플래그와 대응한다.
EXCLUDE_KEYS: tuple[str, ...] = (
    "preferred",
    "spac",
    "managed",
    "zero_volume",
    "new_listing",
    "reit",
    "caution",
    "foreign",
)


class ConfigError(ValueError):
    """설정 파일이 없거나 값이 올바르지 않을 때. 메시지는 사용자에게 그대로 보여준다."""


@dataclass(frozen=True)
class Filters:
    min_avg_trading_value_krw: int
    avg_trading_value_window: int
    price_above_ma: tuple[int, ...]
    rsi_period: int  # RSI 값을 계산하는 기간 (조건이 없어도 값은 저장한다)
    rsi_max: float | None  # None 이면 RSI 조건을 두지 않는다

    @property
    def required_rows(self) -> int:
        """모든 지표를 계산하는 데 필요한 최소 거래일 수."""
        return max(
            self.avg_trading_value_window,
            *self.price_above_ma,
            self.rsi_period + 1,
        )


FLOW_MODES: tuple[str, ...] = ("mark", "filter")
MAX_FLOW_WINDOW_DAYS = 10  # 네이버 수급이 주는 이력 길이


@dataclass(frozen=True)
class FlowConfig:
    mode: str  # "mark": 표시만 / "filter": 수급 약함 종목을 탈락
    window_days: int


# TradingAgents-KR API 가 받는 effort 값 (그쪽 요청 검증과 같은 목록)
ANALYSIS_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class AnalysisConfig:
    base_url: str
    effort: str
    timeout_seconds: int  # 종목 하나를 기다리는 최대 시간
    poll_interval_seconds: float


@dataclass(frozen=True)
class Config:
    exclude: frozenset[str]
    filters: Filters
    flow: FlowConfig
    analysis: AnalysisConfig
    raw: dict[str, Any]  # run.json 에 그대로 남기기 위한 원본


def _positive_int(value: Any, name: str) -> int:
    # bool 은 int 의 하위 클래스라 따로 막는다 (true 가 1 로 통과하는 것 방지).
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"설정 '{name}' 은 1 이상의 정수여야 합니다 (현재: {value!r}).")
    return value


def parse_config(raw: Any) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("설정 파일의 최상위는 키-값 구조여야 합니다.")

    universe = raw.get("universe") or {}
    exclude_raw = universe.get("exclude") or {}
    if not isinstance(exclude_raw, dict):
        raise ConfigError("설정 'universe.exclude' 는 종류: true/false 구조여야 합니다.")
    unknown = set(exclude_raw) - set(EXCLUDE_KEYS)
    if unknown:
        raise ConfigError(
            f"설정 'universe.exclude' 에 알 수 없는 종류가 있습니다: {sorted(unknown)} "
            f"(가능: {', '.join(EXCLUDE_KEYS)})"
        )
    for key, flag in exclude_raw.items():
        if not isinstance(flag, bool):
            raise ConfigError(f"설정 'universe.exclude.{key}' 는 true/false 여야 합니다 (현재: {flag!r}).")
    exclude = frozenset(key for key, flag in exclude_raw.items() if flag)

    f = raw.get("filters")
    if not isinstance(f, dict):
        raise ConfigError("설정에 'filters' 항목이 없습니다.")
    missing = {
        "min_avg_trading_value_krw",
        "avg_trading_value_window",
        "price_above_ma",
        "rsi_period",
    } - set(f)
    if missing:
        raise ConfigError(f"설정 'filters' 에 빠진 항목이 있습니다: {sorted(missing)}")

    mas = f["price_above_ma"]
    if not isinstance(mas, list) or not mas:
        raise ConfigError("설정 'filters.price_above_ma' 는 이동평균 기간의 비어 있지 않은 목록이어야 합니다.")
    rsi_max = f.get("rsi_max")  # 없거나 null 이면 RSI 조건을 두지 않는다
    if rsi_max is not None and (
        isinstance(rsi_max, bool) or not isinstance(rsi_max, (int, float)) or not 0 < rsi_max <= 100
    ):
        raise ConfigError(f"설정 'filters.rsi_max' 는 0 초과 100 이하의 숫자여야 합니다 (현재: {rsi_max!r}).")

    filters = Filters(
        min_avg_trading_value_krw=_positive_int(f["min_avg_trading_value_krw"], "filters.min_avg_trading_value_krw"),
        avg_trading_value_window=_positive_int(f["avg_trading_value_window"], "filters.avg_trading_value_window"),
        price_above_ma=tuple(_positive_int(n, "filters.price_above_ma") for n in mas),
        rsi_period=_positive_int(f["rsi_period"], "filters.rsi_period"),
        rsi_max=None if rsi_max is None else float(rsi_max),
    )
    return Config(
        exclude=exclude,
        filters=filters,
        flow=_parse_flow(raw.get("flow")),
        analysis=_parse_analysis(raw.get("analysis")),
        raw=raw,
    )


def _parse_flow(raw: Any) -> FlowConfig:
    """``flow`` 항목은 생략할 수 있다 (기본값: 표시만, 최근 5거래일)."""
    if raw is None:
        return FlowConfig(mode="mark", window_days=5)
    if not isinstance(raw, dict):
        raise ConfigError("설정 'flow' 는 키-값 구조여야 합니다.")
    mode = raw.get("mode", "mark")
    if mode not in FLOW_MODES:
        raise ConfigError(
            f"설정 'flow.mode' 는 {' 또는 '.join(FLOW_MODES)} 여야 합니다 (현재: {mode!r})."
        )
    window = _positive_int(raw.get("window_days", 5), "flow.window_days")
    if window > MAX_FLOW_WINDOW_DAYS:
        raise ConfigError(
            f"설정 'flow.window_days' 는 {MAX_FLOW_WINDOW_DAYS} 이하여야 합니다 "
            f"(수급 데이터는 최근 {MAX_FLOW_WINDOW_DAYS}거래일만 제공, 현재: {window})."
        )
    return FlowConfig(mode=mode, window_days=window)


def _parse_analysis(raw: Any) -> AnalysisConfig:
    """``analysis`` 항목은 생략할 수 있다 (로컬 서버, effort low, 종목당 30분 대기)."""
    defaults = {
        "base_url": "http://127.0.0.1:8000",
        "effort": "low",
        "timeout_seconds": 1800,
        "poll_interval_seconds": 5,
    }
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("설정 'analysis' 는 키-값 구조여야 합니다.")
    unknown = set(raw) - set(defaults)
    if unknown:
        raise ConfigError(f"설정 'analysis' 에 알 수 없는 항목이 있습니다: {sorted(unknown)}")
    merged = {**defaults, **raw}

    base_url = merged["base_url"]
    if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"설정 'analysis.base_url' 은 http(s):// 로 시작해야 합니다 (현재: {base_url!r}).")
    effort = merged["effort"]
    if effort not in ANALYSIS_EFFORTS:
        raise ConfigError(
            f"설정 'analysis.effort' 는 {', '.join(ANALYSIS_EFFORTS)} 중 하나여야 합니다 (현재: {effort!r})."
        )
    poll = merged["poll_interval_seconds"]
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or poll <= 0:
        raise ConfigError(
            f"설정 'analysis.poll_interval_seconds' 는 0 보다 큰 숫자여야 합니다 (현재: {poll!r})."
        )
    return AnalysisConfig(
        base_url=base_url,
        effort=effort,
        timeout_seconds=_positive_int(merged["timeout_seconds"], "analysis.timeout_seconds"),
        poll_interval_seconds=float(poll),
    )


def load_config(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"설정 파일을 읽지 못했습니다: {path} ({exc.strerror})") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"설정 파일이 올바른 YAML 이 아닙니다: {path}") from exc
    return parse_config(raw)
