"""정리 요약을 보고서로 만든다 (``GET /api/sessions/{id}/report``): 마크다운(기본)과 HTML.

정리할 때 DB 에 저장한 요약(``StoredSummary``)을 그대로 싣는다. 문장을 다시 만들지 않으므로 화면과 보고서가 같은 문장을 보여 주고,
저장한 뒤 문장 틀이 바뀌어도 보고서는 정리했을 때의 문장이다. 모델도 외부 조회도 쓰지 않는다.

- **마크다운**은 그림을 담을 수 없어서 차트를 텍스트 막대그래프(코드 블록)로 싣는다 (``summary_charts.render_text``).
- **HTML**은 화면과 같은 SVG 차트(``summary_charts.render``)를 문서 안에 그대로 넣는다. 외부 파일·스크립트·주소 없이 혼자 열린다.
  글 속의 업종 이름 같은 값은 모두 이스케이프한다.

평가점수는 저장값(−2 ~ +2)으로 적는다. 화면은 100 을 곱해 보여 주므로 보고서에 그 사실을 적는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from html import escape
from zoneinfo import ZoneInfo

from krx_screener import summary_charts
from krx_screener.analysis import RATING_LABELS, REVIEW
from krx_screener.flow import STATUS_LABELS
from krx_screener.repository import Item, StoredSummary

KST = ZoneInfo("Asia/Seoul")

THEME_NOTE = (
    "테마는 네이버 업종 기준입니다. 순위에 들어간 후보(분석이 끝나고 점수가 있는 종목)만 셉니다. "
    "평가점수는 −2 ~ +2 이고 화면에는 100 을 곱한 값으로 보입니다."
)
STALE_NOTE = "※ 정리 이후 다시 분석된 종목이 있어 낡았을 수 있습니다. 화면에서 정리를 다시 실행하면 최신이 됩니다."
DISCLAIMER = "등급과 이 요약은 분석 결과이고 투자 권유가 아닙니다."
THEME_HEAD = ("업종", "후보", "평균 평가점수", "등급", "수급 상태", "평균 RSI", "외국인 5일(주)", "기관 5일(주)")
THEME_RIGHT = {1, 2, 5, 6, 7}  # 숫자 칸 (오른쪽 정렬)
STOCK_HEAD = ("순위", "종목", "코드", "최종 등급", "평가점수")
STOCK_RIGHT = {0, 4}
GRADE_CLASS = {"Buy": "g-buy", "Overweight": "g-overweight", "Hold": "g-hold", "Underweight": "g-underweight", "Sell": "g-sell"}


def _cell(text: object) -> str:
    """마크다운 표 칸에 넣을 글. 칸을 가르는 ``|`` 와 줄바꿈을 막는다."""
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


def _universe_rows(stats: Mapping | None) -> list[tuple[str, str]]:
    """목록을 만들 때 저장한 전종목 수치의 (항목, 값). 저장하지 않았으면 비어 있다 (종합 평가 문장이 없다고 말한다)."""
    if not stats or not stats.get("evaluated"):
        return []
    evaluated = int(stats["evaluated"])
    rows = [("시세를 확인한 종목", f"{evaluated:,}개")]
    fail_each = stats.get("fail_each") or {}
    for key in sorted(fail_each, key=lambda k: (len(k), k)):
        if key.startswith("above_ma"):
            share = 100 * (evaluated - fail_each[key]) / evaluated
            rows.append((f"{key.removeprefix('above_ma')}일선 위", f"{share:.1f}%"))
    candidates = int(stats.get("candidates", 0))
    rows.append(("후보 조건을 모두 통과한 종목", f"{candidates:,}개 ({100 * candidates / evaluated:.1f}%)"))
    return rows


def _stat_rows(stored: StoredSummary) -> list[tuple[str, str]]:
    """종합 평가의 (항목, 값) 표. 마크다운과 HTML 이 같이 쓴다."""
    market = stored.market
    return [
        ("점수가 있는 후보", f"{market.scored_count}개"),
        ("평균 평가점수", _score(market.avg_score)),
        ("등급", _ratings(market.rating_counts, zeros=True)),
        ("수급 상태", _flows(market.flow_counts)),
        *_universe_rows(market.universe_stats),
    ]


def _theme_cells(t) -> list[str]:
    rsi = "—" if t.avg_rsi is None else f"{t.avg_rsi:.0f}"
    return [
        t.name, str(t.item_count), _score(t.avg_score), _ratings(t.rating_counts, zeros=False), _flows(t.flow_counts),
        rsi, _signed_int(t.foreign_net_5d), _signed_int(t.institution_net_5d),
    ]


def _ranked(items: Sequence[Item]) -> tuple[list[Item], int]:
    """정리할 때 순위를 받은 종목(순위 순)과, 받지 못한 종목 수. 순위가 없는 종목은 실패했거나 점수가 없거나 정리 뒤에 끝난 종목이다."""
    ranked = sorted((i for i in items if i.rank is not None), key=lambda i: i.rank)
    return ranked, len(items) - len(ranked)


def _grade(item: Item) -> str:
    """최종 등급의 한국어 표기. 서버가 등급을 읽지 못한 종목(``REVIEW``)은 그대로 둔다."""
    return RATING_LABELS.get(item.decision, item.decision) or "—"


def _stock_cells(item: Item) -> list[str]:
    return [str(item.rank), item.name, item.code, _grade(item), _score(item.score)]


def _stock_note(ranked: int, unranked: int) -> str:
    if ranked == 0:
        text = "순위에 들어간 종목이 없습니다."
    else:
        text = f"순위에 들어간 종목 {ranked}개의 최종 등급과 평가점수를 순위 순으로 적었습니다. 평가점수는 저장값(−2 ~ +2)입니다."
    if unranked:
        text += f" 순위에 들어가지 않은 종목 {unranked}개(실패·점수 없음·정리 이후에 끝난 종목)는 뺐습니다."
    return text


def _header(as_of: date, counts: Mapping[str, int], stored: StoredSummary) -> list[tuple[str, str]]:
    return [
        ("기준일", as_of.isoformat()),
        ("정리 시각", f"{stored.created_at.astimezone(KST):%Y-%m-%d %H:%M} (한국 시각)"),
        ("분석", f"완료 {counts.get('done', 0)}개 · 실패 {counts.get('failed', 0)}개"),
    ]


def render_markdown(as_of: date, counts: Mapping[str, int], stale: bool, stored: StoredSummary, items: Sequence[Item]) -> str:
    """``as_of`` 기준 세션의 정리 요약 보고서(마크다운). ``counts`` 는 처리 상태별 개수, ``stale`` 은 정리 뒤에 다시 분석된 종목이 있는가.

    ``items`` 는 세션의 종목들이다. 순위를 받은 종목만 종목별 결과 표에 순위 순으로 싣는다.
    """
    market = stored.market
    lines = [
        f"# KRX Screener 정리 요약 — {as_of.isoformat()}",
        "",
        f"- 기준일: {as_of.isoformat()}",
        f"- 정리 시각: {stored.created_at.astimezone(KST):%Y-%m-%d %H:%M} (한국 시각)",
        f"- 분석 완료 {counts.get('done', 0)}개 · 실패 {counts.get('failed', 0)}개",
    ]
    if stale:
        lines += ["", f"> {STALE_NOTE}"]

    lines += [
        "",
        "## 종합 평가",
        "",
        market.assessment,
        "",
        "| 항목 | 값 |",
        "|---|---|",
        *[f"| {_cell(k)} | {_cell(v)} |" for k, v in _stat_rows(stored)],
        "",
        "## 차트",
        "",
        "텍스트 막대그래프입니다. 그림 차트는 HTML 보고서(`?format=html`)와 화면의 정리 요약 카드에 있습니다.",
        "",
        "```text",
        summary_charts.render_text(stored),
        "```",
        "",
        "## 업종(테마)별 해석",
        "",
        THEME_NOTE,
        "",
    ]
    if not stored.themes:
        lines += ["점수가 있는 후보가 없어 업종별 해석이 없습니다."]
    else:
        lines += [
            "| " + " | ".join(THEME_HEAD) + " |",
            "|---|---:|---:|---|---|---:|---:|---:|",
        ]
        lines += ["| " + " | ".join(_cell(c) for c in _theme_cells(t)) + " |" for t in stored.themes]
        for t in stored.themes:
            lines += ["", f"### {t.name}", "", t.interpretation]

    ranked, unranked = _ranked(items)
    lines += ["", "## 종목별 결과", "", _stock_note(len(ranked), unranked)]
    if ranked:
        lines += ["", "| " + " | ".join(STOCK_HEAD) + " |", "|---:|---|---|---|---:|"]
        lines += ["| " + " | ".join(_cell(c) for c in _stock_cells(i)) + " |" for i in ranked]

    lines += ["", "---", "", DISCLAIMER, ""]
    return "\n".join(lines)


_CSS = """
body { margin: 0; background: #F4F5F7; color: #14171C; font-family: 'IBM Plex Sans KR', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif; line-height: 1.6; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 8px; } h2 { font-size: 17px; margin: 32px 0 8px; } h3 { font-size: 14px; margin: 18px 0 2px; }
ul.meta { list-style: none; padding: 0; margin: 0; color: #5B6370; font-size: 13px; }
.notice { margin: 16px 0; padding: 10px 14px; border-radius: 8px; background: #FFF6E5; border: 1px solid #F0D49A; color: #6B4700; font-size: 13px; }
table { border-collapse: collapse; width: 100%; background: #fff; font-size: 13px; }
th, td { padding: 8px 10px; border-bottom: 1px solid #ECEEF2; text-align: left; vertical-align: top; }
th { background: #F8F9FB; color: #4B5360; font-weight: 600; white-space: nowrap; }
td.r, th.r { text-align: right; font-variant-numeric: tabular-nums; }
table.stats th { width: 220px; }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(460px, 1fr)); gap: 16px; margin: 12px 0; }
figure { margin: 0; padding: 12px; background: #fff; border: 1px solid #DDE1E7; border-radius: 10px; overflow-x: auto; }
figure svg { max-width: 100%; height: auto; display: block; }
p.note { color: #5B6370; font-size: 13px; }
footer { margin-top: 40px; padding-top: 12px; border-top: 1px solid #DDE1E7; color: #5B6370; font-size: 12px; }
.grade { display: inline-block; padding: 2px 10px; border-radius: 6px; font-size: 12px; font-weight: 600; }
.g-buy { background: #FBE3E4; color: #A3212B; } .g-overweight { background: #FDF0F0; color: #A8343D; } .g-hold { background: #EEF0F3; color: #3F4652; }
.g-underweight { background: #E9EFF9; color: #2A5596; } .g-sell { background: #DCE6F7; color: #1D4A8F; }
@media print { body { background: #fff; } figure, table { break-inside: avoid; } }
""".strip()


def _figures(names: list[str], stored: StoredSummary) -> str:
    """차트 SVG 를 ``<figure>`` 로 묶는다. 그릴 수 없는 차트(전종목 수치가 없을 때의 시장 폭)는 뺀다."""
    drawn = [svg for name in names if (svg := summary_charts.render(name, stored)) is not None]
    return '<div class="charts">' + "".join(f"<figure>{svg}</figure>" for svg in drawn) + "</div>"


def render_html(as_of: date, counts: Mapping[str, int], stale: bool, stored: StoredSummary, items: Sequence[Item]) -> str:
    """같은 보고서를 HTML 한 문서로 만든다. 차트는 SVG 를 그대로 넣고, 그 밖의 글은 모두 이스케이프한다."""
    e = escape
    title = f"KRX Screener 정리 요약 — {as_of.isoformat()}"
    out = [
        "<!doctype html>",
        '<html lang="ko">',
        f'<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(title)}</title><style>{_CSS}</style></head>',
        "<body><main>",
        f"<h1>{e(title)}</h1>",
        '<ul class="meta">' + "".join(f"<li>{e(k)}: {e(v)}</li>" for k, v in _header(as_of, counts, stored)) + "</ul>",
    ]
    if stale:
        out.append(f'<p class="notice">{e(STALE_NOTE)}</p>')
    out += [
        "<h2>종합 평가</h2>",
        f"<p>{e(stored.market.assessment)}</p>",
        '<table class="stats">' + "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in _stat_rows(stored)) + "</table>",
        "<h2>차트</h2>",
        _figures(["rating", "flow", "breadth"], stored),
        "<h2>업종(테마)별 해석</h2>",
        f'<p class="note">{e(THEME_NOTE)}</p>',
    ]
    if not stored.themes:
        out.append("<p>점수가 있는 후보가 없어 업종별 해석이 없습니다.</p>")
    else:
        out.append(_figures(["theme-score", "theme-ratings"], stored))
        head = "".join(f'<th class="r">{e(h)}</th>' if i in THEME_RIGHT else f"<th>{e(h)}</th>" for i, h in enumerate(THEME_HEAD))
        rows = "".join(
            "<tr>" + "".join(f'<td class="r">{e(c)}</td>' if i in THEME_RIGHT else f"<td>{e(c)}</td>" for i, c in enumerate(_theme_cells(t))) + "</tr>"
            for t in stored.themes
        )
        out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
        for t in stored.themes:
            out.append(f"<h3>{e(t.name)}</h3><p>{e(t.interpretation)}</p>")
    ranked, unranked = _ranked(items)
    out += ["<h2>종목별 결과</h2>", f'<p class="note">{e(_stock_note(len(ranked), unranked))}</p>']
    if ranked:
        head = "".join(f'<th class="r">{e(h)}</th>' if i in STOCK_RIGHT else f"<th>{e(h)}</th>" for i, h in enumerate(STOCK_HEAD))
        rows = []
        for item in ranked:
            rank, name, code, grade, score = _stock_cells(item)
            badge = f'<span class="grade {GRADE_CLASS.get(item.decision, "g-hold")}">{e(grade)}</span>'
            rows.append(f'<tr><td class="r">{e(rank)}</td><td>{e(name)}</td><td>{e(code)}</td><td>{badge}</td><td class="r">{e(score)}</td></tr>')
        out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>")
    out += [f"<footer>{e(DISCLAIMER)}</footer>", "</main></body></html>"]
    return "\n".join(out) + "\n"
