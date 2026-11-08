"""정리 요약의 차트 (SVG). 서버가 한 번만 그리고 화면과 HTML 보고서가 같은 그림을 쓴다.

DB 없이 검증한다: 저장된 요약(``StoredSummary``)을 ``summary.summarize`` 로 만들어 차트 함수에 넣는다. 그림은 XML 로 읽어서 본다
(막대에 ``data-*`` 속성을 달아 두어 폭·위치를 숫자로 비교한다).
"""

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import pytest
from test_summary import AS_OF, item

from krx_screener import summary_charts as charts
from krx_screener.repository import StoredSummary
from krx_screener.summary import summarize

NS = {"s": "http://www.w3.org/2000/svg"}
STATS = {"evaluated": 2000, "candidates": 116, "fail_each": {"avg_trading_value": 1500, "above_ma20": 800, "above_ma60": 1000}}


def stored(items, universe_stats=None) -> StoredSummary:
    themes, market = summarize(items, AS_OF, universe_stats)
    return StoredSummary(created_at=datetime(2026, 11, 8, 10, 35, tzinfo=UTC), market=market, themes=themes)


def parse(svg: str) -> ET.Element:
    return ET.fromstring(svg)  # 올바른 XML 이 아니면 여기서 실패한다


def bar(root: ET.Element, key: str) -> ET.Element:
    found = [e for e in root.iter("{http://www.w3.org/2000/svg}rect") if e.get("data-key") == key]
    assert len(found) == 1, (key, len(found))
    return found[0]


def width(root: ET.Element, key: str) -> float:
    return float(bar(root, key).get("width"))


def texts(root: ET.Element) -> str:
    return " ".join("".join(t.itertext()) for t in root.iter("{http://www.w3.org/2000/svg}text"))


SAMPLE = [
    *[item(i, 0.4, "Hold", sector=("26", "건설")) for i in (1, 2, 3)],
    *[item(i, -0.5, "Underweight", sector=("33", "항공사"), flow="weak") for i in (4, 5, 6, 7, 8, 9, 10, 11)],
    item(12, -0.2, "Underweight", sector=("21", "제약"), flow="strong"),
]


# --- 모든 차트는 올바른 SVG ---


@pytest.mark.parametrize("name", ["rating", "flow", "theme-score", "theme-ratings", "breadth"])
def test_every_chart_is_well_formed_svg_with_a_title_and_a_viewbox(name):
    svg = charts.render(name, stored(SAMPLE, STATS))
    root = parse(svg)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.get("viewBox") and root.get("role") == "img"  # 화면에서 크기를 맞추고 스크린 리더가 읽는다
    assert root.find("s:title", NS) is not None and (root.find("s:title", NS).text or "").strip()


def test_the_chart_names_are_the_ones_the_api_serves():
    assert charts.CHART_NAMES == ("rating", "flow", "breadth", "theme-score", "theme-ratings")


def test_an_unknown_chart_name_is_rejected():
    with pytest.raises(KeyError):
        charts.render("pie", stored(SAMPLE))


def test_the_same_summary_draws_the_same_picture():
    one, two = stored(SAMPLE, STATS), stored(SAMPLE, STATS)
    assert all(charts.render(n, one) == charts.render(n, two) for n in charts.CHART_NAMES)


# --- 등급 분포 ---


def test_the_rating_chart_has_a_bar_per_rating_sized_by_count():
    root = parse(charts.render("rating", stored(SAMPLE)))
    # 보유 3 · 비중 축소 9 (오버 3 + …): SAMPLE 은 Hold 3, Underweight 9
    assert width(root, "Underweight") / width(root, "Hold") == pytest.approx(9 / 3)
    assert width(root, "Buy") == width(root, "Sell") == 0  # 0 개는 폭이 0 이다 (그래도 줄은 있다)
    text = texts(root)
    for label in ("매수", "비중 확대", "보유", "비중 축소", "매도"):
        assert label in text
    assert "9개 (75.0%)" in text and "3개 (25.0%)" in text  # 개수와 비율을 막대 옆에 적는다


def test_the_rating_chart_uses_the_grade_colours_the_screen_uses():
    root = parse(charts.render("rating", stored(SAMPLE)))
    fills = {k: bar(root, k).get("fill") for k in ("Buy", "Overweight", "Hold", "Underweight", "Sell")}
    assert len(set(fills.values())) == 5  # 등급마다 색이 다르다
    assert fills["Buy"] != fills["Sell"]


def test_with_nothing_scored_the_charts_say_so_instead_of_drawing_empty_bars():
    empty = stored([item(1, None, "REVIEW"), item(2, None, "", state="failed")])
    for name in ("rating", "flow", "theme-score", "theme-ratings"):
        assert "점수가 있는 후보가 없" in texts(parse(charts.render(name, empty))), name


# --- 수급 상태 ---


def test_the_flow_chart_counts_each_status():
    root = parse(charts.render("flow", stored(SAMPLE)))
    assert width(root, "strong") > 0 and width(root, "weak") > width(root, "strong")  # weak 8, strong 1
    assert width(root, "partial") > 0 and width(root, "unconfirmed") == 0
    for label in ("양호", "부분", "약함", "미확인"):
        assert label in texts(root)


# --- 전종목 시장 폭 ---


def test_the_breadth_chart_shows_the_share_above_each_moving_average():
    root = parse(charts.render("breadth", stored(SAMPLE, STATS)))
    text = texts(root)
    assert "20일선 위" in text and "60.0%" in text
    assert "60일선 위" in text and "50.0%" in text
    assert "후보 조건 통과" in text and "5.8%" in text
    assert width(root, "above_ma20") == pytest.approx(width(root, "above_ma60") * 60 / 50)  # 폭은 비율에 비례한다


def test_without_whole_market_numbers_the_breadth_chart_is_not_drawn():
    assert charts.render("breadth", stored(SAMPLE, None)) is None
    assert charts.render("breadth", stored(SAMPLE, {"evaluated": 0})) is None


# --- 업종별 평균 평가점수 ---


def zero_axis(root: ET.Element) -> float:
    lines = [e for e in root.iter("{http://www.w3.org/2000/svg}line") if e.get("data-axis") == "zero"]
    assert len(lines) == 1
    return float(lines[0].get("x1"))


def test_negative_scores_grow_to_the_left_of_the_zero_line_and_positive_to_the_right():
    root = parse(charts.render("theme-score", stored(SAMPLE)))
    axis = zero_axis(root)
    plus, minus = bar(root, "theme:26"), bar(root, "theme:33")  # 건설 +0.4, 항공사 −0.5
    assert float(plus.get("x")) == pytest.approx(axis)  # 오른쪽: 0 선에서 시작
    assert float(minus.get("x")) + float(minus.get("width")) == pytest.approx(axis)  # 왼쪽: 0 선에서 끝남
    assert plus.get("fill") != minus.get("fill")  # 국내 관례: 양수 빨강, 음수 파랑


def test_the_longest_bar_leaves_room_for_its_value_label():
    """가장 큰 점수의 막대가 눈금 끝까지 닿으면 값 글이 업종 이름과 겹친다 (실제 11-05 데이터에서 식품 −0.60 이 그랬다)."""
    root = parse(charts.render("theme-score", stored([item(i, -0.6, "Underweight", sector=("1", "식품")) for i in (1, 2, 3)])))
    axis = zero_axis(root)
    edge = min(float(e.get("x1")) for e in root.iter("{http://www.w3.org/2000/svg}line") if e.get("data-axis") == "edge")
    assert width(root, "theme:1") <= 0.8 * (axis - edge)


def test_the_bar_lengths_are_proportional_to_the_scores():
    root = parse(charts.render("theme-score", stored(SAMPLE)))
    assert width(root, "theme:33") / width(root, "theme:26") == pytest.approx(0.5 / 0.4)


def test_each_theme_row_shows_its_name_count_and_signed_score():
    text = texts(parse(charts.render("theme-score", stored(SAMPLE))))
    assert "건설 (3)" in text and "항공사 (8)" in text and "제약 (1)" in text
    assert "+0.40" in text and "-0.50" in text


def test_only_the_themes_with_the_most_stocks_are_drawn_when_there_are_many():
    many = [item(i, 0.1, sector=(f"{i:02d}", f"업종{i}")) for i in range(1, 21)]
    many += [item(100 + i, 0.1, sector=("01", "업종1")) for i in range(1, 4)]  # 업종1 이 가장 크다
    root = parse(charts.render("theme-score", stored(many)))
    drawn = [e for e in root.iter("{http://www.w3.org/2000/svg}rect") if (e.get("data-key") or "").startswith("theme:")]
    assert len(drawn) == charts.MAX_THEMES == 15
    assert bar(root, "theme:01") is not None  # 가장 큰 업종은 들어 있다
    assert "상위 15개 업종" in texts(root)


def test_a_long_theme_name_is_shortened_and_markup_in_a_name_is_escaped():
    odd = [item(i, 0.2, sector=("1", "가" * 40)) for i in (1, 2, 3)] + [item(i, 0.2, sector=("2", "A&B <b>x</b>")) for i in (4, 5, 6)]
    svg = charts.render("theme-score", stored(odd))
    root = parse(svg)  # < & 가 이스케이프되지 않았다면 XML 이 깨진다
    assert "…" in texts(root) and "가" * 40 not in texts(root)
    assert "A&B <b>x</b>" in texts(root)  # 읽어 보면 원래 글이 그대로다
    assert "<b>" not in svg  # 글자 안의 태그가 진짜 태그로 새지 않는다


# --- 업종별 등급 구성 ---


def test_the_theme_ratings_chart_stacks_one_segment_per_rating_present():
    root = parse(charts.render("theme-ratings", stored(SAMPLE)))
    assert width(root, "theme:33:Underweight") > 0
    # 항공사는 비중 축소만 8 개: 다른 등급 조각은 없다
    assert not [e for e in root.iter("{http://www.w3.org/2000/svg}rect") if (e.get("data-key") or "").startswith("theme:33:") and e.get("data-key") != "theme:33:Underweight"]
    # 전체 길이는 업종의 후보 수에 비례한다 (항공사 8 : 건설 3)
    assert width(root, "theme:33:Underweight") / width(root, "theme:26:Hold") == pytest.approx(8 / 3)


def test_the_theme_ratings_chart_has_a_legend_of_the_five_ratings():
    text = texts(parse(charts.render("theme-ratings", stored(SAMPLE))))
    for label in ("매수", "비중 확대", "보유", "비중 축소", "매도"):
        assert label in text


# --- 마크다운용 텍스트 막대그래프 ---


def block_lines(text: str, title: str) -> list[str]:
    """``text`` 에서 ``title`` 로 시작하는 차트 한 덩어리 (다음 빈 줄까지)."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(title))
    end = next((i for i in range(start, len(lines)) if not lines[i].strip()), len(lines))
    return lines[start:end]


def test_the_text_charts_cover_the_same_charts_as_the_svg_ones():
    text = charts.render_text(stored(SAMPLE, STATS))
    for title in ("등급 분포 (후보 12개)", "수급 상태 분포", "전종목 시장 폭", "업종별 평균 평가점수"):
        assert title in text


def test_a_text_bar_has_a_length_proportional_to_its_count_and_none_when_zero():
    lines = block_lines(charts.render_text(stored(SAMPLE)), "등급 분포")
    by_label = {re.split(r"\s{2,}", line.strip(), maxsplit=1)[0]: line for line in lines[1:]}  # 라벨은 두 칸 이상의 공백 앞까지 ("비중 축소" 처럼 공백이 든 것도)
    assert by_label["보유"].count("█") * 3 == by_label["비중 축소"].count("█")  # 3 : 9
    assert "█" not in by_label["매수"] and "█" not in by_label["매도"]  # 0 개는 막대가 없다
    assert "75.0%" in by_label["비중 축소"]


def test_the_text_score_chart_puts_negative_bars_left_of_the_axis_and_positive_right():
    lines = block_lines(charts.render_text(stored(SAMPLE)), "업종별 평균 평가점수")
    plus = next(line for line in lines if line.strip().startswith("건설"))
    minus = next(line for line in lines if line.strip().startswith("항공사"))
    assert plus.index("|") < plus.index("█") and "▒" not in plus  # 양수: 축 오른쪽
    assert minus.index("▒") < minus.index("|") and "█" not in minus  # 음수: 축 왼쪽
    assert "+0.40" in plus and "-0.50" in minus


def test_the_text_charts_align_korean_labels_by_display_width():
    """한글은 두 칸을 차지한다. 줄마다 막대가 시작하는 칸이 같아야 세로로 맞는다."""
    import unicodedata

    def columns_before_bar(line: str) -> int:
        head = line[: line.index("█")]
        return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in head)

    lines = block_lines(charts.render_text(stored(SAMPLE)), "등급 분포")
    assert len({columns_before_bar(line) for line in lines if "█" in line}) == 1


def test_the_text_charts_skip_the_whole_market_chart_when_it_was_not_saved():
    assert "전종목 시장 폭" not in charts.render_text(stored(SAMPLE, None))
