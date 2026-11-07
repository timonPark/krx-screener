"""세션이 목록을 뽑을 때 쓴 조건을 저장하고 내려준다 (#57).

화면의 필터 문구는 이 값으로 만든다. 설정을 바꿔도 이미 만든 세션은 자기가 쓴 조건을 그대로 가진다.
"""

from dataclasses import replace
from datetime import date

from test_session_build_api import post, swap

from krx_screener.config import EXCLUDE_KEYS
from krx_screener.universe import UniverseFetchError

# config.yaml 의 값 그대로 (테스트는 저장소의 실제 설정으로 돈다)
CONFIG_CRITERIA = {
    "filters": {
        "min_avg_trading_value_krw": 1_000_000_000,
        "avg_trading_value_window": 20,
        "price_above_ma": [20, 60],
        "rsi_max": None,
    },
    "exclude": ["preferred", "spac", "managed", "zero_volume", "new_listing", "reit"],
    "flow": {"mode": "mark", "window_days": 5},
}


def with_filters(client, **filters):
    deps = client.app.state.deps
    client.app.state.deps = replace(deps, config=replace(deps.config, filters=replace(deps.config.filters, **filters)))


def session_of(client, session_id):
    return client.get(f"/api/sessions/{session_id}").json()["session"]


def test_a_new_session_carries_the_criteria_of_the_config_it_was_made_with(client):
    created = post(client).json()
    assert created["criteria"] == CONFIG_CRITERIA  # 만들기 시작한 응답에도 있다
    assert session_of(client, created["id"])["criteria"] == CONFIG_CRITERIA  # 다 만든 뒤 조회에도 있다


def test_the_session_list_carries_the_criteria_too(client):
    post(client)
    assert [s["criteria"] for s in client.get("/api/sessions").json()] == [CONFIG_CRITERIA]


def test_a_session_made_before_a_config_change_keeps_its_own_criteria(client):
    old = post(client, "2026-11-04").json()["id"]
    with_filters(client, min_avg_trading_value_krw=2_000_000_000, price_above_ma=(5, 20), rsi_max=70.0)
    new = post(client, "2026-11-05").json()["id"]
    old_filters = session_of(client, old)["criteria"]["filters"]
    new_filters = session_of(client, new)["criteria"]["filters"]
    assert old_filters == CONFIG_CRITERIA["filters"]  # 바꾸기 전 세션은 그대로
    assert new_filters == {
        "min_avg_trading_value_krw": 2_000_000_000,
        "avg_trading_value_window": 20,
        "price_above_ma": [5, 20],
        "rsi_max": 70.0,
    }


def test_the_excluded_kinds_and_the_flow_mode_are_saved_too(client):
    deps = client.app.state.deps
    client.app.state.deps = replace(
        deps,
        config=replace(deps.config, exclude=frozenset({"reit", "caution"}), flow=replace(deps.config.flow, mode="filter", window_days=3)),
    )
    criteria = session_of(client, post(client).json()["id"])["criteria"]
    assert criteria["exclude"] == ["reit", "caution"]  # 설정 파일의 종류 순서 (EXCLUDE_KEYS), 고른 것만
    assert criteria["flow"] == {"mode": "filter", "window_days": 3}


def test_exclude_follows_the_order_of_the_known_kinds(client):
    deps = client.app.state.deps
    client.app.state.deps = replace(deps, config=replace(deps.config, exclude=frozenset(EXCLUDE_KEYS)))
    assert session_of(client, post(client).json()["id"])["criteria"]["exclude"] == list(EXCLUDE_KEYS)


def test_a_session_without_a_record_has_null_criteria(client, repo):
    session = repo.create_session(date(2026, 11, 5))  # 명령줄 세션·기존 세션과 같은 모양: 조건을 저장하지 않았다
    assert session_of(client, session.id)["criteria"] is None
    assert [s["criteria"] for s in client.get("/api/sessions").json()] == [None]


def test_a_session_that_failed_still_shows_the_criteria_it_tried(client):
    def broken():
        raise UniverseFetchError("종목 목록을 받지 못했습니다.")

    swap(client, listing=broken)
    session_id = post(client).json()["id"]
    session = session_of(client, session_id)
    assert session["build"]["state"] == "failed"
    assert session["criteria"] == CONFIG_CRITERIA
