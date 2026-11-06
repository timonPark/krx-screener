"""에이전트별 등급 판독기 (#21): 분석 상태 로그의 에이전트별 칸에서 등급을 읽는다.

샘플의 모양은 실제 로그(``~/.tradingagents/logs/<종목>/TradingAgentsStrategy_logs/full_states_log_<기준일>.json``)에서
확인한 것이다. 읽지 못한 칸은 오류가 아니라 그 에이전트만 빈다.
"""

import json
from datetime import date
from pathlib import Path

import pytest
from fakes import AGENT_FIELDS as FAKE_AGENT_FIELDS
from fakes import write_state_log

from krx_screener.agent_ratings import (
    AGENT_FIELDS,
    AGENT_TOTAL,
    log_path,
    parse_rating,
    read_agent_ratings,
)

AS_OF = date(2026, 11, 5)


# --- 한 칸의 글에서 등급 읽기 ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 실제 로그에서 본 표기들
        ("## 요약\n\n**등급**: 매수\n\n본문", "Buy"),
        ("**등급**: 비중 확대", "Overweight"),
        ("**등급**: 비중확대", "Overweight"),
        ("**등급**: 보유", "Hold"),
        ("- **등급**: 비중 축소", "Underweight"),
        ("**등급**: 매도", "Sell"),
        ("**등급**: HOLD", "Hold"),  # 영어 대문자
        ("**등급**: SELL", "Sell"),
        ("**등급**: Underweight", "Underweight"),
        ("**등급**: Overweight", "Overweight"),
        ("**Rating**: Buy", "Buy"),  # 영어 라벨
        ("Aggressive Analyst: **등급**: 보유", "Hold"),  # 앞에 말하는 사람이 붙음
        ("**전략적 조치**: **등급**: 비중 축소 (`069620`, KOSPI)", "Underweight"),
        ("**투자 논리**: **등급: 보유.** 세 분석가의 최종 결론이 모두 보유로 수렴했습니다.", "Hold"),
        ("| **등급** | **보유** | 33,400원 이상 종가 안착 시 상향 |", "Hold"),  # 표
        ("| **등급** | HOLD |", "Hold"),
        ("| 종합 | 등급 | 보유 | - |", "Hold"),
        ("**등급**: 보유\n\n본문\n\n**등급**: 보유", "Hold"),  # 같은 값이 여러 번
        ("## 등급: 비중 확대", "Overweight"),  # 제목
        ("1. **등급**: 매도", "Sell"),  # 번호 목록
        ("**등급**: 비중 축소\n**최종 거래 제안**: 매도", "Underweight"),  # 3단계 제안은 등급이 아니다
    ],
)
def test_the_grade_is_read_from_the_forms_seen_in_real_logs(text, expected):
    assert parse_rating(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "등급 없이 끝나는 글입니다.",
        "**최종 거래 제안**: 매수",  # 3단계 제안만 있는 칸은 5단계 등급으로 바꿀 수 없다
        "| 최종 거래 제안 | **보유** | 근거 | — |",
        "**등급**: REDUCE",  # 5단계에 없는 값은 지어내지 않는다
        "| **등급** | REDUCE |",
        "**등급**: 보유\n\n... 조건이 바뀌면 ...\n\n**등급**: 매수",  # 서로 다른 값 = 어느 쪽인지 모른다
        "아래 등급은 펀더멘털만 반영한 것입니다.",  # 문장 속의 '등급'
        "| 등급 하향 (매도) | 종가 205,000원 이탈 시 하향 |",
        "**등급 판단 근거**\n- 근거 1",
        "제 등급은 비중 축소이고, 실행은 매도입니다.",  # 콜론 없는 문장
        "**등급**: 보유분을 줄입니다",  # 값 뒤에 글자가 붙으면 다른 낱말
        "**등급**:\n매수",  # 다음 줄의 값은 이 줄의 값이 아니다
        "당신이 제시한 프롬프트는 특정 거래 방향(SELL)에 대한 옹호를 요구합니다.",  # 역할극을 거부한 응답
        "애널리스트 컨센서스 등급: 매수 유지",  # 다른 사람의 의견
        "현재 등급: 보유 (조건 충족 시 상향)",  # 문장 속 설명
        "Operating rating: Buy",
    ],
)
def test_a_cell_without_a_readable_grade_gives_none(text):
    assert parse_rating(text) is None


def test_the_research_managers_recommendation_label_is_read_only_for_that_agent(tmp_path):
    # 리서치 매니저는 `**추천**: …` 으로 시작하는 글을 쓴다. 다른 에이전트의 '추천' 은 등급이 아니다
    manager = "**추천**: 비중 축소\n\n**근거**: **추천: 비중 축소 (Underweight)**\n\n본문"
    analyst = "애널리스트 추천: 매수 유지\n\n**등급**: 보유"
    out = write_raw_log(tmp_path, {"investment_plan": manager, "news_report": analyst})
    assert out["research_manager"] == "Underweight"
    assert out["news"] == "Hold"  # '추천: 매수' 가 충돌로 읽히지 않는다


def write_raw_log(root, fields, code="028050", as_of=AS_OF, **extra):
    """칸 이름 → 글 로 직접 쓴 상태 로그를 읽는다 (``investment_debate_state`` 같은 중첩 칸은 점으로 잇는다)."""
    log = {"company_of_interest": code, "trade_date": as_of.isoformat()}
    for key, value in fields.items():
        node = log
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    log.update(extra)
    path = log_path(root, code, as_of)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, ensure_ascii=False), encoding="utf-8")
    return read_agent_ratings(root, code, as_of)


# --- 로그 파일 읽기 ---


def test_the_agent_fields_match_the_shape_of_a_real_log():
    assert AGENT_FIELDS == FAKE_AGENT_FIELDS
    assert AGENT_TOTAL == 10  # 시세·뉴스·재무 분석가, 강세·약세, 리서치 매니저, 공격적·보수적·중립, 포트폴리오 매니저


def test_the_log_is_found_by_ticker_and_date():
    assert log_path(Path("/logs"), "028050", AS_OF) == Path(
        "/logs/028050/TradingAgentsStrategy_logs/full_states_log_2026-11-05.json"
    )


def test_every_agent_is_read_from_its_own_cell(tmp_path):
    ratings = {
        "market": "Buy", "news": "Overweight", "fundamentals": "Hold", "bull": "Overweight", "bear": "Underweight",
        "research_manager": "Hold", "aggressive": "Buy", "conservative": "Sell", "neutral": "Hold",
        "portfolio_manager": "Underweight",
    }
    write_state_log(tmp_path, "028050", AS_OF, ratings)
    assert read_agent_ratings(tmp_path, "028050", AS_OF) == ratings  # 칸이 에이전트를 정한다


def test_sentiment_analyst_and_trader_are_not_agents_to_read(tmp_path):
    out = write_raw_log(
        tmp_path,
        {"sentiment_report": "**등급**: 매수", "trader_investment_plan": "**등급**: 매도", "market_report": "**등급**: 보유"},
    )
    assert out == {"market": "Hold"}


def test_an_empty_or_unreadable_cell_leaves_only_that_agent_empty(tmp_path):
    ratings = {"market": "Buy", "news": None, "bull": "Overweight", "neutral": None}  # None = 칸이 비어 있음
    write_state_log(tmp_path, "028050", AS_OF, ratings)
    out = read_agent_ratings(tmp_path, "028050", AS_OF)
    assert "news" not in out and "neutral" not in out
    assert (out["market"], out["bull"]) == ("Buy", "Overweight")
    assert len(out) == 8  # 나머지 8개는 기본 Hold 로 읽힌다


@pytest.mark.parametrize(
    "fields",
    [
        {"market_report": None},  # 칸이 null
        {"market_report": 123},  # 문자열이 아님
        {"market_report": ["**등급**: 매수"]},
        {"investment_debate_state": "펼쳐지지 않은 글"},  # 중첩 칸 자리에 문자열
        {"investment_debate_state": None},
        {"risk_debate_state": ["aggressive_history"]},
    ],
)
def test_a_cell_in_an_unexpected_shape_is_skipped_not_an_error(tmp_path, fields):
    out = write_raw_log(tmp_path, {"news_report": "**등급**: 보유", **fields})
    assert out == {"news": "Hold"}


@pytest.mark.parametrize("content", ["", "{깨진", "[]", "null", '"문자열"', "123"])
def test_a_log_that_is_not_a_json_object_gives_nothing(tmp_path, content):
    path = log_path(tmp_path, "028050", AS_OF)
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    assert read_agent_ratings(tmp_path, "028050", AS_OF) == {}


def test_a_missing_log_gives_nothing(tmp_path):
    assert read_agent_ratings(tmp_path, "028050", AS_OF) == {}  # 서버가 다른 컴퓨터에 있거나 폴더가 다르다
    assert read_agent_ratings(tmp_path / "없는-폴더", "028050", AS_OF) == {}


def test_a_log_that_is_not_binary_text_gives_nothing(tmp_path):
    path = log_path(tmp_path, "028050", AS_OF)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00\x80")
    assert read_agent_ratings(tmp_path, "028050", AS_OF) == {}


def write_log_file(root, code, as_of, content):
    path = log_path(root, code, as_of)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")


def test_a_log_of_another_ticker_or_date_is_not_read(tmp_path):
    cell = {"market_report": "**등급**: 매수"}
    write_log_file(tmp_path, "028050", AS_OF, {"company_of_interest": "207940", "trade_date": "2026-11-05", **cell})
    assert read_agent_ratings(tmp_path, "028050", AS_OF) == {}  # 안의 종목코드가 다르다
    write_log_file(tmp_path, "028050", AS_OF, {"company_of_interest": "028050", "trade_date": "2026-11-04", **cell})
    assert read_agent_ratings(tmp_path, "028050", AS_OF) == {}  # 안의 기준일이 다르다
    write_log_file(tmp_path, "028050", AS_OF, {"company_of_interest": "028050", "trade_date": "2026-11-05", **cell})
    assert read_agent_ratings(tmp_path, "028050", AS_OF) == {"market": "Buy"}  # 맞으면 읽힌다


def test_a_log_without_the_identity_fields_is_still_read(tmp_path):
    path = log_path(tmp_path, "028050", AS_OF)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"market_report": "**등급**: 매수"}), encoding="utf-8")
    assert read_agent_ratings(tmp_path, "028050", AS_OF) == {"market": "Buy"}
