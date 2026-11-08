"""보고서의 종목별 결과 표: 순위 순으로 종목명·종목코드·최종 등급·평가점수를 싣는다 (마크다운과 HTML).

표는 정리할 때 매긴 순위(``rank``)를 따른다. 순위가 없는 종목(실패, 점수 없음, 정리 뒤에 끝난 종목)은 표에서 빼고 몇 개 뺐는지 적는다.
"""

import re

from test_organize_summary_api import ROWS, STATS, make, organize
from test_summary_charts_api import report

from krx_screener.repository import Item

# 순번과 점수가 다르게: 순위는 점수가 높은 순이라 2번(0.9) → 1번(0.1) → 3번(−0.4) 이다
SCRAMBLED = [(1, "26", "건설", 0.1), (2, "26", "건설", 0.9), (3, "33", "항공사", -0.4)]


def stock_rows(text: str) -> list[list[str]]:
    """마크다운 표에서 `종목별 결과` 절의 데이터 행 (칸 목록)."""
    section = text.split("## 종목별 결과")[1]
    rows = [line for line in section.splitlines() if line.startswith("|") and not line.startswith("|---") and "순위" not in line]
    return [[c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))] for line in rows]


def test_the_markdown_report_lists_every_ranked_stock_in_rank_order(client, repo):
    session = make(repo, SCRAMBLED, STATS)
    organize(client, session.id)
    rows = stock_rows(report(client, session.id).text)

    # 순위 · 종목명 · 종목코드 · 최종 등급 · 평가점수 (순번 순이 아니라 순위 순)
    assert rows == [
        ["1", "종목2", "000002", "보유", "+0.90"],
        ["2", "종목1", "000001", "보유", "+0.10"],
        ["3", "종목3", "000003", "비중 축소", "-0.40"],
    ]


def test_the_table_has_the_requested_columns_and_states_the_score_scale(client, repo):
    session = make(repo, SCRAMBLED, STATS)
    organize(client, session.id)
    body = report(client, session.id).text
    assert "| 순위 | 종목 | 코드 | 최종 등급 | 평가점수 |" in body
    section = body.split("## 종목별 결과")[1]
    assert "저장값(−2 ~ +2)" in section  # 화면은 100 배로 보이므로 보고서의 스케일을 밝힌다


def test_a_failed_stock_is_left_out_and_the_report_says_how_many(client, repo):
    session = make(repo, ROWS, STATS)  # 5번 종목은 실패
    organize(client, session.id)
    body = report(client, session.id).text
    assert [r[1] for r in stock_rows(body)] == ["종목1", "종목2", "종목3", "종목4"]
    assert "종목5" not in body.split("## 종목별 결과")[1]
    assert "순위에 들어가지 않은 종목 1개" in body


def test_a_stock_finished_after_organizing_is_not_in_the_table_until_organized_again(client, repo):
    session = make(repo, ROWS[:2], STATS)
    organize(client, session.id)
    repo.add_items(session.id, [Item(seq=9, code="000009", name="새종목", market="KOSPI")])
    repo.mark_done(session.id, "000009", decision="Hold", score=0.5, agents_rated=10, agents_total=10)
    body = report(client, session.id).text
    assert "새종목" not in body  # 아직 순위가 없다
    assert "낡았" in body  # 보고서는 그렇다고 알린다
    organize(client, session.id)
    assert "새종목" in report(client, session.id).text


def test_a_pipe_in_a_stock_name_does_not_break_the_table(client, repo):
    session = make(repo, [(1, "26", "건설", 0.4)], STATS)
    repo._conn.execute("UPDATE session_items SET name = '가|나' WHERE session_id = %s", (session.id,))
    organize(client, session.id)
    assert "가\\|나" in report(client, session.id).text


def test_with_no_ranked_stock_the_report_says_so(client, repo):
    session = make(repo, [(1, "26", "건설", None), (2, "33", "항공사", None)])
    organize(client, session.id)
    section = report(client, session.id).text.split("## 종목별 결과")[1]
    assert "순위에 들어간 종목이 없습니다" in section


def test_the_decision_shows_in_korean_and_an_unreadable_one_stays_review(client, repo):
    session = make(repo, [(1, "26", "건설", 0.4), (2, "26", "건설", 0.2)], STATS)
    repo.mark_done(session.id, "000002", decision="REVIEW", score=0.2, agents_rated=10, agents_total=10)  # 서버가 등급을 못 읽음
    organize(client, session.id)
    rows = stock_rows(report(client, session.id).text)
    assert [r[3] for r in rows] == ["보유", "REVIEW"]


# --- HTML 보고서 ---


def test_the_html_report_has_the_same_table(client, repo):
    session = make(repo, SCRAMBLED, STATS)
    organize(client, session.id)
    body = report(client, session.id, format="html").text
    section = body.split("종목별 결과")[1]

    names = re.findall(r"<td>(종목\d)</td>", section)
    assert names == ["종목2", "종목1", "종목3"]  # 순위 순
    assert "+0.90" in section and "-0.40" in section and "000002" in section
    assert "보유" in section and "비중 축소" in section
    # 등급은 화면과 같은 색 배지로 보인다
    assert 'class="grade g-hold"' in section and 'class="grade g-underweight"' in section


def test_a_stock_name_with_markup_cannot_inject_html_into_the_table(client, repo):
    session = make(repo, [(1, "26", "건설", 0.4)], STATS)
    repo._conn.execute("UPDATE session_items SET name = '<script>alert(1)</script>' WHERE session_id = %s", (session.id,))
    organize(client, session.id)
    body = report(client, session.id, format="html").text
    assert "<script" not in body
    assert "&lt;script&gt;" in body.split("종목별 결과")[1]
