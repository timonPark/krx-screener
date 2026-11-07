"""세션을 만들 때 수급 상태를 채우는 동작 (#15). 웹 API 경계에서 검증한다.

기본 세계(종목 3개: 028050 · 207940 · 003490, 기준일 2026-11-05)에서 수급 가짜만 바꿔 가며 확인한다.
저장된 값은 같은 테스트 DB 연결(`repo`)로 읽는다.
"""

import uuid
from dataclasses import replace

import pytest
from fakes import AS_OF, FakePrices, FakeTrend, trend_rows
from helpers import falling_closes, make_history, make_listing, passing_closes

CODES = ("028050", "207940", "003490")


def _swap(client, **externals):
    deps = client.app.state.deps
    client.app.state.deps = replace(deps, externals=replace(deps.externals, **externals))


def _create(client, repo, as_of="2026-11-05"):
    response = client.post("/api/sessions", json={"as_of": as_of})
    assert response.status_code == 202, response.text
    # 응답은 만들기를 시작한 세션이다. 테스트 클라이언트는 응답한 뒤에 만들기를 끝내므로 지금 조회하면 다 만든 세션이다
    body = client.get(f"/api/sessions/{response.json()['id']}").json()["session"]
    assert body["build"]["state"] == "ready", body
    return body, {i.code: i for i in repo.items(uuid.UUID(body["id"]))}


def _flow(item):
    return (item.flow_status, item.foreign_net_5d, item.institution_net_5d)


def test_each_listed_stock_gets_its_status_and_the_five_day_sums(client, repo):
    _, items = _create(client, repo)
    assert {code: _flow(items[code]) for code in CODES} == dict.fromkeys(CODES, ("strong", 500, 250))


def test_the_signs_of_the_sums_decide_strong_partial_and_weak(client, repo):
    _swap(
        client,
        trend=FakeTrend(
            {
                "028050": trend_rows(foreign=100, institution=-50),  # 한쪽만 매수 우위
                "207940": trend_rows(foreign=-10, institution=-20),  # 둘 다 아님
                "003490": trend_rows(foreign=10, institution=20),  # 둘 다 매수 우위
            }
        ),
    )
    _, items = _create(client, repo)
    assert _flow(items["028050"]) == ("partial", 500, -250)
    assert _flow(items["207940"]) == ("weak", -50, -100)
    assert _flow(items["003490"]) == ("strong", 50, 100)


def test_unreadable_flow_is_unconfirmed_with_empty_sums_and_the_stock_stays_listed(client, repo):
    _swap(
        client,
        trend=FakeTrend(
            {"028050": trend_rows()},  # 207940 은 조회 실패, 003490 은 모르는 종목(빈 목록)
            failing=["207940"],
        ),
    )
    body, items = _create(client, repo)
    assert body["total"] == 3  # 수급을 못 읽어도 목록에서 빠지지 않는다
    assert _flow(items["028050"]) == ("strong", 500, 250)
    for code in ("207940", "003490"):
        assert _flow(items[code]) == ("unconfirmed", None, None)  # "수급 0" 이 아니라 빈 값


def test_weak_stocks_are_not_removed_even_if_the_config_says_filter(client, repo):
    _swap(client, trend=FakeTrend(dict.fromkeys(CODES, trend_rows(foreign=-1, institution=-1))))
    deps = client.app.state.deps
    client.app.state.deps = replace(deps, config=replace(deps.config, flow=replace(deps.config.flow, mode="filter")))
    body, items = _create(client, repo)
    assert body["total"] == 3
    assert {i.flow_status for i in items.values()} == {"weak"}  # 스펙: 수급은 표시만 한다


def test_the_window_comes_from_the_config(client, repo):
    deps = client.app.state.deps
    client.app.state.deps = replace(deps, config=replace(deps.config, flow=replace(deps.config.flow, window_days=3)))
    _, items = _create(client, repo)
    assert _flow(items["028050"]) == ("strong", 300, 150)  # 3거래일 합계


def test_a_date_older_than_the_flow_history_makes_every_stock_unconfirmed(client, repo):
    # 네이버 수급은 최근 10거래일만 준다. 2026-10-15 이전 구간에는 기준일 이전 데이터가 없다
    body, items = _create(client, repo, as_of="2026-10-15")
    assert body["total"] == 3
    assert {_flow(i) for i in items.values()} == {("unconfirmed", None, None)}


def test_flow_is_looked_up_only_for_stocks_that_made_the_list(client, repo):
    listing = make_listing(
        [
            {"Code": "000010", "Name": "통과"},
            {"Code": "000015", "Name": "통과우"},  # 우선주: 제외
            {"Code": "000120", "Name": "하락중"},  # 종가가 이동평균선 아래: 탈락
        ]
    )
    prices = FakePrices(
        {
            "000010": make_history(passing_closes(), 3_000_000, AS_OF),
            "000015": make_history(passing_closes(), 3_000_000, AS_OF),
            "000120": make_history(falling_closes(), 3_000_000, AS_OF),
            "005930": make_history(passing_closes(), 3_000_000, AS_OF),
        }
    )
    trend = FakeTrend({"000010": trend_rows()})
    _swap(client, listing=lambda: listing.copy(), prices=prices, trend=trend)
    body, items = _create(client, repo)
    assert list(items) == ["000010"]
    assert trend.calls == ["000010"]  # 제외되거나 탈락한 종목은 조회하지 않는다 (불필요한 외부 호출을 줄인다)


@pytest.mark.parametrize("failing_all", [True, False])
def test_a_down_flow_source_does_not_fail_the_request(client, repo, failing_all):
    trend = FakeTrend({}, failing=CODES) if failing_all else FakeTrend({})
    _swap(client, trend=trend)
    body, items = _create(client, repo)
    assert body["total"] == 3
    assert {i.flow_status for i in items.values()} == {"unconfirmed"}
