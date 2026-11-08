"""정리 요약의 차트를 SVG 로 그린다 (``GET /api/sessions/{id}/charts/{이름}.svg``).

차트를 그리는 코드는 **여기 하나**다. 화면(``<img>``)과 HTML 보고서(인라인)가 같은 그림을 쓰므로 둘이 어긋나지 않는다. 라이브러리 없이 문자열로 만든다.

- 입력은 정리할 때 저장한 요약(``StoredSummary``)이고, 같은 입력이면 같은 그림이다 (모델도 외부 조회도 쓰지 않는다).
- 막대에는 ``data-key`` 같은 속성을 달아 테스트가 폭·위치를 숫자로 비교할 수 있게 한다.
- 색은 화면의 규칙을 따른다: 국내 관례로 매수 쪽 빨강, 매도 쪽 파랑 (``frontend/styles.css`` 의 ``--up``/``--down``).
- 글꼴은 이름만 적는다. ``<img>`` 로 불리면 웹 글꼴을 못 받아 시스템 글꼴로 보이고, HTML 보고서에 인라인되면 문서의 글꼴을 쓴다.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Callable
from xml.sax.saxutils import escape, quoteattr

from krx_screener.analysis import RATING_LABELS, REVIEW
from krx_screener.flow import STATUS_LABELS
from krx_screener.repository import StoredSummary
from krx_screener.summary import FLOW_ORDER, RATING_ORDER, ThemeSummary

CHART_NAMES = ("rating", "flow", "breadth", "theme-score", "theme-ratings")

# 업종별 차트에 그리는 업종 수의 상한. 후보가 많은 업종부터 그린다 (그 이상은 글자가 겹쳐 읽을 수 없다).
MAX_THEMES = 15
NAME_MAX = 14  # 업종 이름 한 줄의 최대 글자 수. 넘으면 … 로 줄인다

FONT = "'IBM Plex Sans KR', 'Apple SD Gothic Neo', 'Malgun Gothic', 'Noto Sans KR', sans-serif"
INK, MUTED, GRID = "#14171C", "#5B6370", "#DDE1E7"
RATING_COLORS = {
    "Buy": "#C8323C", "Overweight": "#E58A90", "Hold": "#B5BBC5", "Underweight": "#7FA2D9", "Sell": "#2A5DB0", REVIEW: "#8A919C",
}
FLOW_COLORS = {"strong": "#1E7A4F", "partial": "#B07A00", "weak": "#8A919C", "unconfirmed": "#D5D9E0"}
UP, DOWN = "#C8323C", "#2A5DB0"  # 평가점수 양수(빨강)·음수(파랑)

ROW_H = 26
BAR_H = 16
TOP = 44  # 제목과 부제를 위한 위쪽 여백


def _num(value: float) -> str:
    return f"{value:.4f}"


def _text(x: float, y: float, content: str, *, size: int = 13, fill: str = INK, anchor: str = "start", weight: str = "normal") -> str:
    return (
        f'<text x="{_num(x)}" y="{_num(y)}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
        f'font-weight="{weight}">{escape(content)}</text>'
    )


def _rect(x: float, y: float, w: float, h: float, fill: str, key: str) -> str:
    return f'<rect data-key={quoteattr(key)} x="{_num(x)}" y="{_num(y)}" width="{_num(w)}" height="{h}" rx="2" fill="{fill}"/>'


def _svg(width: int, height: int, title: str, subtitle: str, body: list[str]) -> str:
    head = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" font-family="{FONT}">',
        f"<title>{escape(title)}</title>",
        f'<rect width="{width}" height="{height}" fill="#FFFFFF"/>',
        _text(0, 20, title, size=15, weight="bold"),
    ]
    if subtitle:
        head.append(_text(0, 38, subtitle, size=12, fill=MUTED))
    return "".join([*head, *body, "</svg>"])


def _message(title: str, message: str) -> str:
    return _svg(560, 80, title, "", [_text(0, 56, message, fill=MUTED)])


def _short(name: str) -> str:
    return name if len(name) <= NAME_MAX else name[: NAME_MAX - 1] + "…"


def _themes(stored: StoredSummary) -> tuple[list[ThemeSummary], str]:
    """그릴 업종(후보가 많은 순 상위 ``MAX_THEMES`` 개)과 잘렸을 때의 부제."""
    themes = stored.themes[:MAX_THEMES]
    note = ""
    if len(stored.themes) > MAX_THEMES:
        note = f"후보가 많은 상위 {MAX_THEMES}개 업종만 그렸습니다 (전체 {len(stored.themes)}개)"
    return themes, note


def _hbars(title: str, subtitle: str, rows: list[tuple[str, str, float, str, str]], scale: float, width: int = 560) -> str:
    """가로 막대 차트. ``rows`` 는 (키, 이름, 값, 색, 막대 옆 글) 이고 ``scale`` 은 막대가 가득 찰 때의 값이다."""
    label_w, bar_max = 90, 260
    body: list[str] = []
    for i, (key, label, value, color, note) in enumerate(rows):
        y = TOP + i * ROW_H
        w = bar_max * value / scale if scale else 0.0
        body += [
            _text(label_w - 8, y + 13, label, anchor="end"),
            _rect(label_w, y, w, BAR_H, color, key),
            _text(label_w + w + 8, y + 13, note, fill=MUTED),
        ]
    return _svg(width, TOP + len(rows) * ROW_H + 8, title, subtitle, body)


def rating_chart(stored: StoredSummary) -> str:
    market = stored.market
    title = f"등급 분포 (후보 {market.scored_count}개)"
    if market.scored_count == 0:
        return _message(title, "점수가 있는 후보가 없어 그릴 수 없습니다.")
    codes = [*RATING_ORDER, *([REVIEW] if market.rating_counts.get(REVIEW) else [])]
    counts = {code: market.rating_counts.get(code, 0) for code in codes}
    rows = [
        (code, RATING_LABELS.get(code, code), n, RATING_COLORS[code], f"{n}개 ({100 * n / market.scored_count:.1f}%)")
        for code, n in counts.items()
    ]
    return _hbars(title, "순위에 들어간 후보의 최종 등급", rows, max(counts.values()))


def flow_chart(stored: StoredSummary) -> str:
    market = stored.market
    title = f"수급 상태 분포 (후보 {market.scored_count}개)"
    if market.scored_count == 0:
        return _message(title, "점수가 있는 후보가 없어 그릴 수 없습니다.")
    counts = {status: market.flow_counts.get(status, 0) for status in FLOW_ORDER}
    rows = [
        (status, STATUS_LABELS[status], n, FLOW_COLORS[status], f"{n}개 ({100 * n / market.scored_count:.1f}%)")
        for status, n in counts.items()
    ]
    return _hbars(title, "최근 5거래일 외국인·기관 순매수 기준", rows, max(counts.values()))


def breadth_chart(stored: StoredSummary) -> str | None:
    """전종목 중 이동평균선 위에 있는 종목의 비율. 목록을 만들 때 전종목 수치를 저장하지 않았으면 그리지 않는다 (None)."""
    stats = stored.market.universe_stats
    if not stats or not stats.get("evaluated"):
        return None
    evaluated = int(stats["evaluated"])
    fail_each = stats.get("fail_each") or {}
    rows: list[tuple[str, str, float, str, str]] = []
    for key in sorted((k for k in fail_each if k.startswith("above_ma")), key=lambda k: (len(k), k)):
        pct = 100 * (evaluated - fail_each[key]) / evaluated
        rows.append((key, f"{key.removeprefix('above_ma')}일선 위", pct, MUTED, f"{pct:.1f}%"))
    candidates = int(stats.get("candidates", 0))
    pct = 100 * candidates / evaluated
    rows.append(("candidates", "후보 조건 통과", pct, INK, f"{pct:.1f}%"))
    return _hbars(f"전종목 시장 폭 (시세를 확인한 {evaluated:,}개 기준)", "목록을 만들 때 센 전종목 수치", rows, 100.0)


def _score_limit(themes: list[ThemeSummary]) -> float:
    """눈금 끝의 점수: 가장 큰 막대가 눈금의 80% 까지만 가도록 0.2 단위로 올림한다 (값이 작아도 막대가 보이고, 남는 20% 에 값 글이 들어간다)."""
    return max(0.5, math.ceil(max(abs(t.avg_score) for t in themes) / 0.8 * 5) / 5)


def theme_score_chart(stored: StoredSummary) -> str:
    """업종별 평균 평가점수. 0 선을 가운데 두고 음수는 왼쪽(파랑), 양수는 오른쪽(빨강)으로 그린다."""
    title = "업종별 평균 평가점수"
    themes, note = _themes(stored)
    if not themes:
        return _message(title, "점수가 있는 후보가 없어 그릴 수 없습니다.")
    label_w, half, side = 170, 205, 60
    width = label_w + 2 * half + side
    axis = label_w + half
    limit = _score_limit(themes)
    body: list[str] = []
    for i, t in enumerate(themes):
        y = TOP + i * ROW_H
        w = half * abs(t.avg_score) / limit
        positive = t.avg_score >= 0
        shown = f"{round(t.avg_score, 2) + 0.0:+.2f}"
        body.append(_text(label_w - 8, y + 13, f"{_short(t.name)} ({t.item_count})", anchor="end"))
        key = f"theme:{t.code}"
        if positive:
            body += [_rect(axis, y, w, BAR_H, UP, key), _text(axis + w + 6, y + 13, shown, fill=MUTED)]
        else:
            body += [_rect(axis - w, y, w, BAR_H, DOWN, key), _text(axis - w - 6, y + 13, shown, fill=MUTED, anchor="end")]
    bottom = TOP + len(themes) * ROW_H
    body += [
        f'<line data-axis="edge" x1="{_num(axis - half)}" x2="{_num(axis - half)}" y1="{TOP - 4}" y2="{bottom}" stroke="{GRID}"/>',
        f'<line data-axis="edge" x1="{_num(axis + half)}" x2="{_num(axis + half)}" y1="{TOP - 4}" y2="{bottom}" stroke="{GRID}"/>',
        f'<line data-axis="zero" x1="{_num(axis)}" x2="{_num(axis)}" y1="{TOP - 4}" y2="{bottom}" stroke="{MUTED}"/>',
        _text(axis - half, bottom + 16, f"-{limit:.1f}", size=11, fill=MUTED, anchor="middle"),
        _text(axis, bottom + 16, "0", size=11, fill=MUTED, anchor="middle"),
        _text(axis + half, bottom + 16, f"+{limit:.1f}", size=11, fill=MUTED, anchor="middle"),
    ]
    subtitle = note or "저장값 (−2 ~ +2). 괄호는 후보 수"
    return _svg(width, bottom + 28, title, subtitle, body)


def theme_ratings_chart(stored: StoredSummary) -> str:
    """업종별 등급 구성. 업종마다 한 줄로, 등급별 후보 수를 이어 붙인 막대다 (전체 길이는 후보 수에 비례)."""
    title = "업종별 등급 구성"
    themes, note = _themes(stored)
    if not themes:
        return _message(title, "점수가 있는 후보가 없어 그릴 수 없습니다.")
    label_w, bar_max, side = 170, 330, 60
    width = label_w + bar_max + side
    top = TOP + 22  # 범례 줄을 위해 더 내린다
    biggest = max(t.item_count for t in themes)
    body: list[str] = []
    for i, code in enumerate(RATING_ORDER):  # 범례: 다섯 등급
        x = label_w + i * 82
        body += [f'<rect x="{x}" y="{TOP - 6}" width="10" height="10" rx="2" fill="{RATING_COLORS[code]}"/>', _text(x + 14, TOP + 3, RATING_LABELS[code], size=11, fill=MUTED)]
    for i, t in enumerate(themes):
        y = top + i * ROW_H
        body.append(_text(label_w - 8, y + 13, f"{_short(t.name)} ({t.item_count})", anchor="end"))
        x = float(label_w)
        for code in RATING_ORDER:
            n = t.rating_counts.get(code, 0)
            if not n:
                continue  # 0 개인 등급 조각은 그리지 않는다
            w = bar_max * n / biggest
            body.append(_rect(x, y, w, BAR_H, RATING_COLORS[code], f"theme:{t.code}:{code}"))
            x += w
        body.append(_text(x + 6, y + 13, f"{t.item_count}개", fill=MUTED))
    return _svg(width, top + len(themes) * ROW_H + 8, title, note, body)


# ---------- 마크다운용 텍스트 막대그래프 ----------
# 마크다운은 그림 차트를 담을 수 없어서(그림은 HTML 보고서에 있다) 같은 내용을 글자 막대로 그린다. 코드 블록 안에서 모양이 맞게 한글은 두 칸으로 센다.

TEXT_BAR = 30  # 막대가 가득 찰 때의 글자 수
TEXT_HALF = 12  # 점수 막대의 한쪽 길이


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _text_bar(value: float, scale: float, width: int = TEXT_BAR) -> str:
    """0 이면 막대가 없고, 0 보다 크면 아무리 작아도 한 칸은 그린다."""
    return "█" * max(1, round(width * value / scale)) if value > 0 and scale else ""


def _text_rows(title: str, rows: list[tuple[str, float, str]], scale: float, *, label_width: int = 10) -> list[str]:
    """(이름, 값, 막대 옆 글) 줄들. 값이 0 인 줄은 막대와 옆 글이 없다."""
    lines = [title]
    for label, value, note in rows:
        bar = _text_bar(value, scale)
        lines.append((f"  {_pad(label, label_width)}{note[0]:>8}" + (f"  {bar} {note[1]}" if bar else "")).rstrip())
    return lines


def render_text(stored: StoredSummary) -> str:
    """마크다운 보고서에 넣는 텍스트 막대그래프 (코드 블록 안에 그대로 놓는다). 차트마다 빈 줄로 나뉜다."""
    market = stored.market
    if market.scored_count == 0:
        return "점수가 있는 후보가 없어 차트가 없습니다."
    total = market.scored_count
    blocks: list[list[str]] = []

    counts = {code: market.rating_counts.get(code, 0) for code in RATING_ORDER}
    blocks.append(_text_rows(
        f"등급 분포 (후보 {total}개)",
        [(RATING_LABELS[c], n, (f"{n}", f"{100 * n / total:.1f}%")) for c, n in counts.items()],
        max(counts.values()),
    ))
    flows = {s: market.flow_counts.get(s, 0) for s in FLOW_ORDER}
    blocks.append(_text_rows(
        f"수급 상태 분포 (후보 {total}개)",
        [(STATUS_LABELS[s], n, (f"{n}", f"{100 * n / total:.1f}%")) for s, n in flows.items()],
        max(flows.values()),
    ))
    stats = market.universe_stats
    if stats and stats.get("evaluated"):
        evaluated = int(stats["evaluated"])
        fail_each = stats.get("fail_each") or {}
        rows = [
            (f"{k.removeprefix('above_ma')}일선 위", 100 * (evaluated - fail_each[k]) / evaluated)
            for k in sorted((k for k in fail_each if k.startswith("above_ma")), key=lambda k: (len(k), k))
        ]
        rows.append(("후보 조건 통과", 100 * int(stats.get("candidates", 0)) / evaluated))
        blocks.append(_text_rows(
            f"전종목 시장 폭 (시세를 확인한 {evaluated:,}개 기준)", [(label, pct, (f"{pct:.1f}%", "")) for label, pct in rows], 100.0, label_width=16,
        ))
    themes, note = _themes(stored)
    if themes:
        limit = _score_limit(themes)
        lines = [f"업종별 평균 평가점수 (저장값 −2 ~ +2, 괄호는 후보 수{'; ' + note if note else ''})"]
        for t in themes:
            r = 0 if t.avg_score == 0 else max(1, round(TEXT_HALF * abs(t.avg_score) / limit))
            neg, pos = ("▒" * r, "") if t.avg_score < 0 else ("", "█" * r)
            shown = f"{round(t.avg_score, 2) + 0.0:+.2f}"
            lines.append(f"  {_pad(f'{_short(t.name)} ({t.item_count})', 34)}{shown}  {neg:>{TEXT_HALF}}|{pos}")
        blocks.append(lines)
    return "\n\n".join("\n".join(b) for b in blocks)


_DRAW: dict[str, Callable[[StoredSummary], str | None]] = {
    "rating": rating_chart,
    "flow": flow_chart,
    "breadth": breadth_chart,
    "theme-score": theme_score_chart,
    "theme-ratings": theme_ratings_chart,
}
assert tuple(_DRAW) == CHART_NAMES


def render(name: str, stored: StoredSummary) -> str | None:
    """이름의 차트를 SVG 글로 그린다. 그릴 수 없으면 None (전종목 수치가 없을 때의 ``breadth``). 모르는 이름은 ``KeyError``."""
    return _DRAW[name](stored)
