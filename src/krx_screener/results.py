"""실행 결과 저장: ``results/<기준일>/candidates.csv`` 와 ``run.json``."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from krx_screener import __version__
from krx_screener.analysis import DONE, AnalysisOutcome, rating_label
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


ANALYSIS_FILE = "analysis.csv"
_ANALYSIS_COLUMNS = [
    "code", "name", "status", "decision", "decision_label", "is_target",
    "report_id", "elapsed_seconds", "message",
]


def write_analysis(out_dir: Path, outcomes: list[AnalysisOutcome]) -> Path:
    """3단계 결과를 쓴다 (종목마다 호출해 중간에 끊겨도 지금까지의 결과가 남게 한다)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ANALYSIS_FILE
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_ANALYSIS_COLUMNS)
        writer.writeheader()
        for o in outcomes:
            writer.writerow(
                {
                    "code": o.code,
                    "name": o.name,
                    "status": o.status,
                    "decision": o.decision or "",
                    "decision_label": rating_label(o.decision),
                    "is_target": o.is_target,
                    "report_id": o.report_id,  # 서버의 실행 번호(run_id)는 서버 메모리에만 있어 저장하지 않는다
                    "elapsed_seconds": round(o.elapsed_seconds, 1),
                    "message": o.message,
                }
            )
    return path


def load_done_analysis(out_dir: Path) -> dict[str, AnalysisOutcome]:
    """이전 실행에서 분석이 끝난 종목만 읽는다 (다시 보내 쿼터를 쓰지 않으려는 용도)."""
    path = out_dir / ANALYSIS_FILE
    if not path.exists():
        return {}
    done: dict[str, AnalysisOutcome] = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("status") != DONE or not row.get("decision"):
                    continue
                done[row["code"]] = AnalysisOutcome(
                    code=row["code"],
                    name=row.get("name", ""),
                    status=DONE,
                    decision=row["decision"],
                    report_id=row.get("report_id", ""),  # 이전 파일의 run_id 열은 읽지 않는다
                    elapsed_seconds=float(row.get("elapsed_seconds") or 0),
                    message=row.get("message", ""),
                )
    except (OSError, KeyError, ValueError, csv.Error):
        return {}  # 읽을 수 없는 파일은 이어받지 않는다 (처음부터)
    return done
