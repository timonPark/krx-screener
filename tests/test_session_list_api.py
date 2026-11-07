"""세션을 만들 때 목록을 채우는 동작 (#14). 웹 API 경계에서 검증한다.

세계: 2026-11-05 기준. 통과하는 종목 5개와, 서로 다른 이유로 빠지는 종목들이 섞여 있다.
저장된 종목은 같은 테스트 DB 연결(`repo`)로 확인한다.
"""

import uuid
from dataclasses import replace

import pytest
from fakes import AS_OF, FakePrices, make_externals
from helpers import falling_closes, make_history, make_listing, passing_closes

CAUTION = "투자주의환기종목(소속부없음)"
FOREIGN = "외국기업(소속부없음)"


def _history(volume, closes=None):
    return make_history(closes or passing_closes(), volume, AS_OF)


def _world():
    listing = make_listing(
        [
            # 통과하는 종목 (평균 거래대금 큰 순서는 아래 기대값 참고)
            {"Code": "000010", "Name": "통과A"},
            {"Code": "000020", "Name": "통과B"},
            {"Code": "000030", "Name": "환기종목", "Dept": CAUTION},
            {"Code": "000040", "Name": "외국기업", "Dept": FOREIGN, "Market": "KOSDAQ"},
            {"Code": "000050", "Name": "과열"},
            # 종류 때문에 빠지는 종목 (시세는 통과해도 빠진다)
            {"Code": "000015", "Name": "통과A우"},  # 우선주: 코드 끝자리가 0 이 아님
            {"Code": "000060", "Name": "스팩", "Dept": "SPAC(소속부없음)"},
            {"Code": "000070", "Name": "관리", "Dept": "관리종목(소속부없음)"},
            {"Code": "000080", "Name": "대신밸류리츠"},
            {"Code": "000090", "Name": "신규", "ListingDate": "2026-10-20"},
            # 시세 때문에 빠지는 종목
            {"Code": "000100", "Name": "거래정지"},
            {"Code": "000110", "Name": "거래부진"},
            {"Code": "000120", "Name": "하락중"},
            {"Code": "000130", "Name": "시세없음"},
        ]
    )
    rising = [1000.0 + 10 * i for i in range(80)]  # 매일 상승 → RSI 100 (과열)
    histories = {
        "000010": _history(3_000_000),
        "000020": _history(2_000_000),
        "000030": _history(1_500_000),
        "000040": _history(1_200_000),
        "000050": _history(2_500_000, rising),
        "000015": _history(3_000_000),
        "000060": _history(3_000_000),
        "000070": _history(3_000_000),
        "000080": _history(3_000_000),
        "000090": _history(3_000_000),
        "000100": make_history(passing_closes(), [3_000_000.0] * 79 + [0.0], AS_OF),  # 기준일 거래량 0
        "000110": _history(10),  # 평균 거래대금이 하한(10억 원) 미만
        "000120": _history(3_000_000, falling_closes()),  # 종가가 20·60일선 아래
        "005930": _history(3_000_000),  # 거래일 확인용
    }  # 000130 은 일부러 없다: 시세 조회 실패
    return listing, histories


@pytest.fixture
def externals(tmp_path):
    listing, histories = _world()
    return replace(
        make_externals(tmp_path),
        listing=lambda: listing.copy(),
        prices=FakePrices(histories),
    )


def _create(client, repo):
    response = client.post("/api/sessions", json={"as_of": "2026-11-05"})
    assert response.status_code == 202, response.text
    # 응답은 만들기를 시작한 세션이다. 테스트 클라이언트는 응답한 뒤에 만들기를 끝내므로 지금 조회하면 다 만든 세션이다
    body = client.get(f"/api/sessions/{response.json()['id']}").json()["session"]
    assert body["build"]["state"] == "ready", body
    return body, repo.items(uuid.UUID(body["id"]))


def test_candidates_are_numbered_by_trading_value_largest_first(client, repo):
    body, items = _create(client, repo)
    # 평균 거래대금: 과열(상승 중이라 종가가 높다) > 통과A > 통과B > 환기종목 > 외국기업
    assert [(i.seq, i.code) for i in items] == [
        (1, "000050"), (2, "000010"), (3, "000020"), (4, "000030"), (5, "000040"),
    ]
    assert body["total"] == 5
    assert client.get("/api/sessions").json()[0]["total"] == 5


@pytest.mark.parametrize(
    ("code", "why"),
    [
        ("000015", "우선주"),
        ("000060", "스팩"),
        ("000070", "관리종목"),
        ("000080", "리츠"),
        ("000090", "신규상장 60거래일 미만"),
        ("000100", "거래정지 의심 (기준일 거래량 0)"),
        ("000110", "평균 거래대금 10억 원 미만"),
        ("000120", "종가가 20·60일선 아래"),
        ("000130", "시세를 받지 못함 (지어내지 않는다)"),
    ],
)
def test_stocks_that_fail_a_rule_are_not_listed(client, repo, code, why):
    _, items = _create(client, repo)
    assert code not in {i.code for i in items}, why


def test_caution_and_foreign_stay_in_the_list_with_a_mark(client, repo):
    _, items = _create(client, repo)
    by_code = {i.code: i for i in items}
    assert (by_code["000030"].is_caution, by_code["000030"].is_foreign) == (True, False)
    assert (by_code["000040"].is_caution, by_code["000040"].is_foreign) == (False, True)
    assert by_code["000040"].market == "KOSDAQ"
    assert (by_code["000010"].is_caution, by_code["000010"].is_foreign) == (False, False)


def test_a_high_rsi_does_not_remove_a_stock_but_the_value_is_kept(client, repo):
    _, items = _create(client, repo)
    overheated = next(i for i in items if i.code == "000050")
    assert overheated.rsi == 100.0  # 매일 오르는 종목: 과열이지만 목록에 있다


def test_listed_values_are_stored_and_the_later_steps_are_still_empty(client, repo):
    _, items = _create(client, repo)
    a = next(i for i in items if i.code == "000010")
    assert (a.name, a.market) == ("통과A", "KOSPI")
    assert a.close == int(passing_closes()[-1])
    assert isinstance(a.avg_trading_value, int) and a.avg_trading_value >= 1_000_000_000
    assert 0 <= a.rsi <= 100
    # 이 세계의 수급 가짜는 이 종목을 모른다(빈 응답) → 미확인. 업종(#16)과 처리 결과는 아직 비어 있다
    assert (a.flow_status, a.foreign_net_5d, a.sector_name, a.description) == ("unconfirmed", None, "", "")
    assert (a.state, a.decision, a.score, a.report_id, a.rank) == ("pending", "", None, "", None)


def test_the_trading_value_floor_comes_from_the_config(client, repo):
    _, items = _create(client, repo)
    assert "000110" not in {i.code for i in items}  # 기본 설정(하한 10억 원)에서는 거래부진 종목이 빠진다

    deps = client.app.state.deps
    low_floor = replace(deps.config, filters=replace(deps.config.filters, min_avg_trading_value_krw=1))
    client.app.state.deps = replace(deps, config=low_floor)
    _, items = _create(client, repo)
    codes = {i.code for i in items}
    assert "000110" in codes  # 하한을 낮추면 들어온다
    assert "000100" not in codes  # 다른 규칙(거래정지 의심 제외)은 그대로다


def test_each_session_gets_its_own_copy_of_the_list(client, repo):
    first, first_items = _create(client, repo)
    second, second_items = _create(client, repo)
    assert first["id"] != second["id"]
    assert len(first_items) == len(second_items) == 5


def test_no_candidates_still_makes_a_session_with_an_empty_list(client, repo, externals):
    listing = make_listing([{"Code": "000120", "Name": "하락중"}])
    only_falling = replace(
        externals,
        listing=lambda: listing.copy(),
        prices=FakePrices({"000120": _history(3_000_000, falling_closes()), "005930": _history(3_000_000)}),
    )
    client.app.state.deps = replace(client.app.state.deps, externals=only_falling)
    body, items = _create(client, repo)
    assert (body["total"], items) == (0, [])


def test_if_the_items_cannot_be_saved_none_are_kept_and_the_session_is_failed(client, pg_conn, externals):
    duplicated = make_listing([{"Code": "000010", "Name": "통과A"}, {"Code": "000010", "Name": "통과A 중복"}])
    broken = replace(externals, listing=lambda: duplicated.copy())
    client.app.state.deps = replace(client.app.state.deps, externals=broken)
    created = client.post("/api/sessions", json={"as_of": "2026-11-05"})
    assert created.status_code == 202  # 만들기를 시작한 뒤에 저장이 실패한다
    assert pg_conn.execute("SELECT count(*) FROM session_items").fetchone()[0] == 0  # 종목은 한꺼번에 저장되거나 하나도 저장되지 않는다
    build = client.get(f"/api/sessions/{created.json()['id']}").json()["session"]["build"]
    # 세션은 실패로 남아서 화면이 사유를 보여 준다 (이전에는 세션도 남지 않았다)
    assert (build["state"], build["error"]) == ("failed", "목록을 만드는 중 오류가 났습니다 (UniqueViolation).")
