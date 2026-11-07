"""웹 API 테스트 기반 (#11).

1. API 틀이 동작한다: 요청을 보내고 DB 상태를 확인한다 (테스트 전용 DB 가 있을 때만).
2. 외부 경계의 가짜들이 실제 소비 코드에서 그대로 쓰인다 (DB 없이 돈다).
"""

import itertools
import json
from dataclasses import replace
from datetime import date

import pytest
from fakes import (
    AGENT_FIELDS,
    AS_OF,
    CODES,
    FakeAnalysisServer,
    FakePrices,
    FakeSector,
    FakeTrend,
    make_externals,
    repo_config,
    trend_rows,
    write_state_log,
)
from fastapi.testclient import TestClient

from krx_screener import flow, universe
from krx_screener.analysis import DONE, FAILED, run_one
from krx_screener.api import Dependencies, create_app
from krx_screener.db import DatabaseError
from krx_screener.externals import SectorInfo
from krx_screener.prices import PriceFetchError, PriceStore

# --- API 틀 (테스트 DB 필요) ---


def test_health_reports_the_schema_version_and_the_db_agrees(client, pg_conn):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok", "schema_version": 3}
    # 응답이 말한 버전이 DB 에 실제로 있다
    assert pg_conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 3


def _downgrade_to_version_1(conn):
    """스키마를 버전 1 로 되돌린다 (코드는 새 버전인데 DB 에 `db init` 을 안 한 상황)."""
    conn.execute("DELETE FROM schema_migrations WHERE version IN (2, 3)")
    conn.execute("DROP INDEX sessions_building_as_of_idx")
    conn.execute("DROP INDEX sessions_cli_as_of_idx")
    conn.execute(
        "ALTER TABLE sessions DROP COLUMN source, DROP COLUMN build_state, DROP COLUMN build_stage, "
        "DROP COLUMN build_done, DROP COLUMN build_total, DROP COLUMN build_error"
    )
    conn.execute("ALTER TABLE session_items DROP COLUMN archived, DROP COLUMN elapsed_seconds")


STALE = "DB 스키마가 최신이 아닙니다. `python -m krx_screener.db init` 을 실행하세요."


def test_health_with_an_old_schema_tells_to_run_init(client, pg_conn):
    _downgrade_to_version_1(pg_conn)
    response = client.get("/api/health")
    assert (response.status_code, response.json()) == (503, {"detail": STALE})


def test_every_address_with_an_old_schema_is_the_same_korean_503_not_a_server_error(client, pg_conn):
    _downgrade_to_version_1(pg_conn)
    for method, path in (("get", "/api/sessions"), ("get", "/api/sessions/latest")):
        response = getattr(client, method)(path)
        assert (response.status_code, response.json()) == (503, {"detail": STALE}), path


def test_health_without_a_schema_is_a_korean_503(client, pg_conn):
    pg_conn.execute("DROP TABLE schema_migrations")
    response = client.get("/api/health")
    assert response.status_code == 503
    assert response.json() == {"detail": "DB 스키마가 없습니다. `python -m krx_screener.db init` 을 실행하세요."}


def test_the_app_uses_the_externals_it_was_given(client, externals):
    assert client.app.state.deps.externals is externals


# --- API 틀 (DB 없이 돈다) ---


def test_health_when_the_database_is_down_is_a_korean_503(tmp_path):
    def broken():
        raise DatabaseError("PostgreSQL 에 연결하지 못했습니다 (127.0.0.1:1/krx).")

    app = create_app(Dependencies(connect=broken, externals=make_externals(tmp_path), config=repo_config()))
    response = TestClient(app).get("/api/health")
    assert response.status_code == 503
    assert response.json() == {"detail": "PostgreSQL 에 연결하지 못했습니다 (127.0.0.1:1/krx)."}


def test_unknown_address_is_a_404(tmp_path):
    app = create_app(Dependencies(connect=lambda: None, externals=make_externals(tmp_path), config=repo_config()))
    assert TestClient(app).get("/api/nope").status_code == 404


# --- 가짜가 실제 소비 코드에서 그대로 쓰인다 ---


def _run(server, code="028050", polls_timeout=100):
    ticks = itertools.count()
    return run_one(server, code, "삼성E&A", "2026-11-05", "low", polls_timeout, 1, lambda s: None, lambda: next(ticks))


def test_fake_analysis_server_finishes_with_the_scripted_decision():
    server = FakeAnalysisServer(decisions={"028050": "Overweight"})
    outcome = _run(server)
    assert (outcome.status, outcome.decision, outcome.is_target) == (DONE, "Overweight", True)
    assert server.started == [("028050", "2026-11-05", "low")]


def test_fake_analysis_server_can_report_running_first_failures_and_a_dead_server():
    assert _run(FakeAnalysisServer(polls_before_done=3)).status == DONE
    failed = _run(FakeAnalysisServer(errors=["028050"]))
    assert (failed.status, "가짜 서버 오류" in failed.message) == (FAILED, True)
    with pytest.raises(Exception, match="서버"):
        FakeAnalysisServer(health_error="TradingAgents-KR 서버에 연결하지 못했습니다").check_health()


def test_fake_trend_feeds_the_real_flow_reader():
    strong = flow.fetch_flow("028050", AS_OF, 5, FakeTrend({"028050": trend_rows()}), retry_wait=0)
    assert (strong.status, strong.foreign, strong.institution) == (flow.STRONG, 500, 250)

    down = flow.fetch_flow("028050", AS_OF, 5, FakeTrend({}, failing=["028050"]), retry_wait=0)
    assert (down.status, down.reason_code) == (flow.UNCONFIRMED, flow.FETCH_FAILED)

    unknown = flow.fetch_flow("999999", AS_OF, 5, FakeTrend({}), retry_wait=0)  # 실제 서버처럼 빈 목록
    assert (unknown.status, unknown.reason_code) == (flow.UNCONFIRMED, flow.NO_DATA)


def test_fake_prices_feed_the_real_price_store():
    prices = FakePrices(make_externals_prices())
    store = PriceStore(prices, AS_OF, 100, None, today=date(2026, 11, 9), retry_wait=0)
    assert not store.get("028050").empty
    broken = PriceStore(FakePrices({}, failing=["028050"]), AS_OF, 100, None, today=date(2026, 11, 9), retry_wait=0)
    with pytest.raises(PriceFetchError):
        broken.get("028050")


def make_externals_prices():
    from fakes import passing_closes
    from helpers import make_history

    return {c: make_history(passing_closes(), 3_000_000, AS_OF) for c in CODES}


def test_default_listing_is_classifiable_and_a_fresh_copy_each_time(tmp_path):
    externals = make_externals(tmp_path)
    first = externals.listing()
    first.loc[0, "Name"] = "바뀜"
    classified = universe.classify(externals.listing(), AS_OF)
    assert list(classified["Code"]) == list(CODES)
    assert "바뀜" not in set(classified["Name"])  # 한 호출의 수정이 다음 호출에 새지 않는다


def test_fake_sector_returns_none_for_an_unreadable_stock():
    sector = FakeSector({"028050": SectorInfo("26", "건설", "EPC")})
    assert sector("028050") == SectorInfo("26", "건설", "EPC")
    assert sector("999999") is None


def test_overriding_one_boundary_keeps_the_others(tmp_path):
    base = make_externals(tmp_path)
    swapped = replace(base, sector=FakeSector({}))
    assert swapped.sector("028050") is None
    assert swapped.prices is base.prices and swapped.analysis is base.analysis


# --- 분석 상태 로그 파일 ---


def test_state_log_is_written_in_the_real_layout(tmp_path):
    path = write_state_log(tmp_path, "028050", AS_OF)
    assert path == tmp_path / "028050" / "TradingAgentsStrategy_logs" / "full_states_log_2026-11-05.json"
    log = json.loads(path.read_text(encoding="utf-8"))
    assert log["run_settings"]["effort"] == "low"
    assert log["final_rating"] == "Hold"
    assert log["trade_date"] == "2026-11-05"


def test_state_log_has_ten_rated_agent_fields_and_two_without_a_rating(tmp_path):
    log = json.loads(write_state_log(tmp_path, "028050", AS_OF).read_text(encoding="utf-8"))
    rated = 0
    for path in AGENT_FIELDS.values():
        node = log
        for key in path:
            node = node[key]
        rated += "**등급**" in node
    assert rated == len(AGENT_FIELDS) == 10
    assert "등급" not in log["sentiment_report"]
    assert "등급" not in log["trader_investment_plan"]


def test_state_log_ratings_can_be_set_and_blanked(tmp_path):
    ratings = {"bull": "Overweight", "bear": "Underweight", "portfolio_manager": "Sell", "news": None}
    log = json.loads(write_state_log(tmp_path, "028050", AS_OF, ratings).read_text(encoding="utf-8"))
    assert "**등급**: 비중 확대" in log["investment_debate_state"]["bull_history"]
    assert "**등급**: 비중 축소" in log["investment_debate_state"]["bear_history"]
    assert "**등급**: 매도" in log["final_trade_decision"]
    assert log["final_rating"] == "Sell"
    assert log["news_report"] == ""  # 읽지 못한 칸
