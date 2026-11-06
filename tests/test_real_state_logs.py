"""이 컴퓨터에 쌓인 실제 분석 상태 로그로 판독기를 확인한다 (#21 '다른 로그에서도 같은 구조인지').

``~/.tradingagents/logs`` 에 로그가 없으면 건너뛴다. 값 자체가 아니라 구조를 본다: 오류 없이 읽히고, 읽힌 값은 5단계 등급이며,
포트폴리오 매니저 칸의 등급은 로그의 ``final_rating`` 과 같다.
"""

import json
import os
from datetime import date
from pathlib import Path

import pytest

from krx_screener.agent_ratings import AGENT_FIELDS, read_agent_ratings

ROOT = Path(os.environ.get("TRADINGAGENTS_RESULTS_DIR") or Path.home() / ".tradingagents" / "logs")
LOGS = sorted(ROOT.glob("*/TradingAgentsStrategy_logs/full_states_log_*.json"))

pytestmark = pytest.mark.skipif(not LOGS, reason=f"{ROOT} 에 분석 상태 로그가 없음")


@pytest.mark.parametrize("path", LOGS, ids=lambda p: f"{p.parts[-3]}-{p.stem.removeprefix('full_states_log_')}")
def test_a_real_log_is_read_without_error_and_agrees_with_its_final_rating(path):
    code = path.parts[-3]
    as_of = date.fromisoformat(path.stem.removeprefix("full_states_log_"))
    ratings = read_agent_ratings(ROOT, code, as_of)
    assert set(ratings) <= set(AGENT_FIELDS)
    assert set(ratings.values()) <= {"Buy", "Overweight", "Hold", "Underweight", "Sell"}
    final = json.loads(path.read_text(encoding="utf-8")).get("final_rating")
    if "portfolio_manager" in ratings and final:
        assert ratings["portfolio_manager"] == final
