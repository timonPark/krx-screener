"""정리 요약을 마크다운 보고서로 만든다 (``GET /api/sessions/{id}/report``).

정리할 때 DB 에 저장한 요약(``StoredSummary``)을 그대로 싣는다. 문장을 다시 만들지 않으므로 화면과 보고서가 같은 문장을 보여 주고,
저장한 뒤 문장 틀이 바뀌어도 보고서는 정리했을 때의 문장이다. 모델도 외부 조회도 쓰지 않는다.

평가점수는 저장값(−2 ~ +2)으로 적는다. 화면은 100 을 곱해 보여 주므로 보고서에 그 사실을 적는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from zoneinfo import ZoneInfo

from krx_screener.analysis import RATING_LABELS, REVIEW
from krx_screener.flow import STATUS_LABELS
from krx_screener.repository import StoredSummary

KST = ZoneInfo("Asia/Seoul")


def _cell(text: object) -> str:
    """표 칸에 넣을 글. 칸을 가르는 ``|`` 와 줄바꿈을 막는다."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _score(value: float | None) -> str:
    """부호를 붙여 소수 둘째 자리까지. 0 에 가까운 값이 ``-0.00`` 으로 보이지 않게 한다 (``summary`` 의 문장과 같은 표기)."""
    return "—" if value is None else f"{round(value, 2) + 0.0:+.2f}"


def _signed_int(value: int | None) -> str:
    return "—" if value is None else f"{value:+,}"


def _ratings(counts: Mapping[str, int], *, zeros: bool) -> str:
    """등급 분포 글. 순서는 매수 → 매도 로 고정이다 (DB 의 jsonb 는 키 순서를 지키지 않는다). ``zeros`` 면 0 인 등급도 적는다."""
    parts = []
    for code in (*RATING_LABELS, REVIEW):
        n = counts.get(code, 0)
        if n or (zeros and code != REVIEW):
            parts.append(f"{RATING_LABELS.get(code, code)} {n}")
    return " · ".join(parts) or "—"


def _flows(counts: Mapping[str, int]) -> str:
    return " · ".join(f"{STATUS_LABELS[status]} {counts.get(status, 0)}" for status in STATUS_LABELS)


def _universe_rows(stats: Mapping | None) -> list[str]:
    """목록을 만들 때 저장한 전종목 수치의 표 줄. 저장하지 않았으면 비어 있다 (종합 평가 문장이 없다고 말한다)."""
    if not stats or not stats.get("evaluated"):
        return []
    evaluated = int(stats["evaluated"])
    rows = [f"| 시세를 확인한 종목 | {evaluated:,}개 |"]
    fail_each = stats.get("fail_each") or {}
    for key in sorted(fail_each, key=lambda k: (len(k), k)):
        if key.startswith("above_ma"):
            share = 100 * (evaluated - fail_each[key]) / evaluated
            rows.append(f"| {key.removeprefix('above_ma')}일선 위 | {share:.1f}% |")
    candidates = int(stats.get("candidates", 0))
    rows.append(f"| 후보 조건을 모두 통과한 종목 | {candidates:,}개 ({100 * candidates / evaluated:.1f}%) |")
    return rows


def render_markdown(as_of: date, counts: Mapping[str, int], stale: bool, stored: StoredSummary) -> str:
    """``as_of`` 기준 세션의 정리 요약 보고서. ``counts`` 는 처리 상태별 개수, ``stale`` 은 정리 뒤에 다시 분석된 종목이 있는가."""
    market = stored.market
    lines = [
        f"# KRX Screener 정리 요약 — {as_of.isoformat()}",
        "",
        f"- 기준일: {as_of.isoformat()}",
        f"- 정리 시각: {stored.created_at.astimezone(KST):%Y-%m-%d %H:%M} (한국 시각)",
        f"- 분석 완료 {counts.get('done', 0)}개 · 실패 {counts.get('failed', 0)}개",
    ]
    if stale:
        lines += ["", "> ※ 정리 이후 다시 분석된 종목이 있어 낡았을 수 있습니다. 화면에서 정리를 다시 실행하면 최신이 됩니다."]

    lines += [
        "",
        "## 종합 평가",
        "",
        market.assessment,
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| 점수가 있는 후보 | {market.scored_count}개 |",
        f"| 평균 평가점수 | {_score(market.avg_score)} |",
        f"| 등급 | {_ratings(market.rating_counts, zeros=True)} |",
        f"| 수급 상태 | {_flows(market.flow_counts)} |",
        *_universe_rows(market.universe_stats),
        "",
        "## 업종(테마)별 해석",
        "",
        "테마는 네이버 업종 기준입니다. 순위에 들어간 후보(분석이 끝나고 점수가 있는 종목)만 셉니다. "
        "평가점수는 −2 ~ +2 이고 화면에는 100 을 곱한 값으로 보입니다.",
        "",
    ]
    if not stored.themes:
        lines += ["점수가 있는 후보가 없어 업종별 해석이 없습니다."]
    else:
        lines += [
            "| 업종 | 후보 | 평균 평가점수 | 등급 | 수급 상태 | 평균 RSI | 외국인 5일(주) | 기관 5일(주) |",
            "|---|---:|---:|---|---|---:|---:|---:|",
        ]
        for t in stored.themes:
            rsi = "—" if t.avg_rsi is None else f"{t.avg_rsi:.0f}"
            lines.append(
                f"| {_cell(t.name)} | {t.item_count} | {_score(t.avg_score)} | {_ratings(t.rating_counts, zeros=False)} "
                f"| {_flows(t.flow_counts)} | {rsi} | {_signed_int(t.foreign_net_5d)} | {_signed_int(t.institution_net_5d)} |"
            )
        for t in stored.themes:
            lines += ["", f"### {t.name}", "", t.interpretation]

    lines += ["", "---", "", "등급과 이 요약은 분석 결과이고 투자 권유가 아닙니다.", ""]
    return "\n".join(lines)
