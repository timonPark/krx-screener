"""세션을 만들 때 업종 정보를 채우는 동작 (#16). 웹 API 경계에서 검증한다.

기본 세계(종목 3개, 기준일 2026-11-05)에서 업종 가짜만 바꿔 가며 확인한다.
"""

import uuid
from dataclasses import replace

from fakes import AS_OF, FakePrices, FakeSector, FakeTrend, trend_rows
from helpers import falling_closes, make_history, make_listing, passing_closes

from krx_screener.externals import SectorInfo


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


def _sector(item):
    return (item.sector_code, item.sector_name, item.description)


def test_each_listed_stock_gets_its_sector_code_name_and_description(client, repo):
    _, items = _create(client, repo)
    assert _sector(items["028050"]) == ("26", "건설", "플랜트 전 분야의 종합 솔루션을 제공하는 EPC 전문기업")
    assert _sector(items["207940"])[:2] == ("21", "제약")
    assert _sector(items["003490"])[:2] == ("33", "항공사")


def test_an_unreadable_stock_has_blanks_and_stays_listed(client, repo):
    _swap(client, sector=FakeSector({"028050": SectorInfo("26", "건설", "EPC")}))  # 나머지 둘은 읽지 못함(None)
    body, items = _create(client, repo)
    assert body["total"] == 3
    assert _sector(items["028050"]) == ("26", "건설", "EPC")
    assert _sector(items["207940"]) == ("", "", "")
    assert _sector(items["003490"]) == ("", "", "")


def test_a_failing_sector_lookup_does_not_stop_the_session(client, repo):
    calls = []

    def broken(code):
        calls.append(code)
        if code == "207940":
            raise RuntimeError("예상하지 못한 실패")
        return SectorInfo("26", "건설", "EPC")

    _swap(client, sector=broken)
    body, items = _create(client, repo)
    assert body["total"] == 3  # 한 종목의 업종 조회 실패가 세션 생성을 막지 않는다
    assert _sector(items["207940"]) == ("", "", "")
    assert _sector(items["028050"]) == ("26", "건설", "EPC")
    assert sorted(calls) == ["003490", "028050", "207940"]


def test_a_partially_known_sector_keeps_what_is_known(client, repo):
    _swap(client, sector=FakeSector({"028050": SectorInfo("", "건설", "")}))  # 코드와 설명은 비어 있다
    _, items = _create(client, repo)
    assert _sector(items["028050"]) == ("", "건설", "")


def test_sector_is_looked_up_only_for_stocks_that_made_the_list(client, repo):
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
    sector = FakeSector({"000010": SectorInfo("27", "반도체와반도체장비", "설명")})
    _swap(client, listing=lambda: listing.copy(), prices=prices, trend=FakeTrend({"000010": trend_rows()}), sector=sector)
    _, items = _create(client, repo)
    assert list(items) == ["000010"]
    assert sector.calls == ["000010"]  # 제외되거나 탈락한 종목은 조회하지 않는다


def test_each_session_stores_its_own_copy_of_the_sector_info(client, repo):
    first_body, _ = _create(client, repo)
    _swap(client, sector=FakeSector({}))  # 두 번째 세션에서는 업종을 읽지 못한다
    second_body, _ = _create(client, repo)

    first = repo.get_item(uuid.UUID(first_body["id"]), "028050")  # 두 번째 세션을 만든 뒤에 다시 읽는다
    second = repo.get_item(uuid.UUID(second_body["id"]), "028050")
    assert _sector(first)[:2] == ("26", "건설")  # 첫 세션의 값은 그대로다
    assert _sector(second) == ("", "", "")  # 두 번째는 빈 칸이다
