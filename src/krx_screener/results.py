"""실행 결과 저장: ``results/<기준일>/candidates.csv`` 와 ``run.json``."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from krx_screener import __version__
from krx_screener.config import Config
from krx_screener.flow import FlowStats
from krx_screener.screen import ScreenResult

APPROXIMATIONS = {
    "trading_value": "거래대금은 종가 × 거래량으로 근사한 값이며 실제 거래대금이 아닙니다.",
    "universe_kinds": "종목 종류(소속부·상장일)는 조회 시점 기준이며 기준일 당시의 값이 아닙니다.",
    "new_listing": "신규상장 판정은 공휴일을 반영하지 않고 평일 수로 센 근사값입니다.",
    "flow": (
        "수급은 네이버 증권(비공식 경로, 최근 10거래일, 수량(주) 기준)이며 금액·기관 세부 "
        "구분이 아닙니다. 읽지 못한 종목은 '미확인'이고 수급 0 이 아닙니다."
    ),
}


def _flow_info(flow: FlowStats) -> dict:
    return {
        "source": "네이버 증권 (비공식, 로그인 불필요)",
        "mode": flow.mode,
        "window_days": flow.window_days,
        "checked": flow.stage1_candidates,
        "status_counts": flow.status_counts,
        "unconfirmed_by_reason": flow.unconfirmed_by_reason,
        "dropped_by_filter": flow.dropped_by_filter,
        "unconfirmed": flow.unconfirmed,
    }


def build_run_info(result: ScreenResult, config: Config, generated_at: datetime) -> dict:
    info: dict = {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "as_of": result.as_of.isoformat(),
        "version": __version__,
        "approximations": APPROXIMATIONS,
        "config": config.raw,
        "funnel": {
            "listing_total": result.listing_total,
            "after_static_exclusion": result.after_static,
            "zero_volume_excluded": result.zero_volume_excluded,
            "undetermined": len(result.undetermined),
            "evaluated": result.evaluated,
            "candidates": len(result.candidates),
        },
        "static_excluded_by_kind": result.static_excluded_by_kind,
        "dropped_by_first_failed_condition": result.drop_first,
        "unmet_by_condition": result.fail_each,
        "undetermined": result.undetermined,
    }
    if result.flow is not None:
        info["funnel"]["stage1_candidates"] = result.flow.stage1_candidates
        info["flow"] = _flow_info(result.flow)
    return info


def write_results(
    result: ScreenResult, config: Config, out_root: Path, generated_at: datetime
) -> Path:
    """결과를 쓰고 폴더 경로를 돌려준다. 같은 기준일 폴더는 덮어쓴다."""
    out_dir = out_root / result.as_of.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: 엑셀에서 한글 종목명이 깨지지 않도록 BOM 을 붙인다.
    result.candidates.to_csv(out_dir / "candidates.csv", index=False, encoding="utf-8-sig")
    info = build_run_info(result, config, generated_at)
    (out_dir / "run.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return out_dir
