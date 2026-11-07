"""목록 뽑기를 뒤에서 처리하고 진행을 보여준다 (#56): ``POST /api/sessions`` 는 바로 ``202`` 로 응답하고, 세션의 ``build`` 로 진행을 본다.

시세·수급·업종 경계를 불리는 순간에 가로채서(``spy``) 그때의 DB 상태를 본다. 테스트 클라이언트는 응답한 뒤에 뒤의 처리를 끝내므로
요청이 돌아온 뒤에는 이미 끝난 상태다. 그래서 "만드는 중"의 모습은 경계가 불리는 도중에 본다.
"""

import threading
import uuid
from dataclasses import replace
from datetime import date

from krx_screener.universe import UniverseFetchError

AS_OF = "2026-11-05"
BUILDING = {"state": "building", "stage": "", "done": 0, "total": 0, "error": ""}


def post(client, as_of=AS_OF):
    return client.post("/api/sessions", json={"as_of": as_of})


def swap(client, **boundaries):
    """외부 경계 몇 개만 바꿔 끼운다."""
    deps = client.app.state.deps
    client.app.state.deps = replace(deps, externals=replace(deps.externals, **boundaries))


def spy(inner, before):
    """경계가 불릴 때마다 ``before()`` 를 먼저 부르는 래퍼."""

    def call(*args, **kwargs):
        before()
        return inner(*args, **kwargs)

    return call


def once(fn):
    """여러 스레드가 동시에 불러도 처음 한 번만 ``fn`` 을 실행한다 (수급·시세 경계는 종목마다 병렬로 불린다)."""
    gate = threading.Lock()

    def run(*args, **kwargs):
        if gate.acquire(blocking=False):
            return fn(*args, **kwargs)

    return run


def row(pg_conn):
    """가장 최근 세션의 만들기 상태와 저장된 종목 수 (없으면 None)."""
    found = pg_conn.execute(
        "SELECT build_state, build_stage, build_done, build_total, build_error, "
        "(SELECT count(*) FROM session_items i WHERE i.session_id = s.id) FROM sessions s ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return None if found is None else dict(zip(("state", "stage", "done", "total", "error", "items"), found, strict=True))


def detail(client, session_id="latest"):
    return client.get(f"/api/sessions/{session_id}").json()


# --- 바로 응답하고 뒤에서 만든다 ---


def test_creating_a_list_answers_at_once_with_a_session_that_is_being_built(client):
    response = post(client)
    assert response.status_code == 202
    body = response.json()
    assert (body["as_of"], body["total"]) == (AS_OF, 0)  # 아직 종목이 없다
    assert body["build"] == BUILDING  # 응답은 만들기 시작한 모습이다
    assert uuid.UUID(body["id"])


def test_when_the_build_is_over_the_session_is_ready_with_its_stocks(client):
    created = post(client).json()
    body = detail(client, created["id"])
    assert (body["session"]["build"]["state"], body["session"]["total"], len(body["items"])) == ("ready", 3, 3)
    assert body["session"]["build"]["done"] == body["session"]["build"]["total"] == 3
    assert [s["build"]["state"] for s in client.get("/api/sessions").json()] == ["ready"]


def test_the_session_exists_while_it_is_being_built_and_has_no_stocks_yet(client, externals, pg_conn):
    seen = []
    swap(client, trend=spy(externals.trend, lambda: seen.append((row(pg_conn), detail(client)))))
    post(client)
    state, body = seen[0]
    assert (state["state"], state["items"]) == ("building", 0)  # 종목은 다 만든 뒤에 한꺼번에 저장된다
    assert body["session"]["build"]["state"] == "building" and body["items"] == []
    assert body["counts"] == {"pending": 0, "running": 0, "done": 0, "failed": 0}
    assert (body["processing"], body["rank_stale"], body["ranked_at"]) == (False, False, None)  # 만드는 잠금은 '처리 중'이 아니다


def test_latest_is_the_session_being_built(client, externals, pg_conn):
    seen = []
    swap(client, sector=spy(externals.sector, lambda: seen.append(detail(client, "latest")["session"]["build"]["state"])))
    post(client)
    assert set(seen) == {"building"}
    assert detail(client)["session"]["build"]["state"] == "ready"


# --- 진행 ---


def test_the_progress_goes_from_prices_to_flow_to_sector(client, externals, pg_conn):
    seen = {"prices": [], "flow": [], "sector": []}
    for stage, name in (("prices", "prices"), ("flow", "trend"), ("sector", "sector")):
        swap(client, **{name: spy(getattr(client.app.state.deps.externals, name), lambda s=stage: seen[s].append(row(pg_conn)))})
    post(client)
    assert all(r["state"] == "building" for rows in seen.values() for r in rows if r)
    assert {r["stage"] for r in seen["prices"] if r} == {"prices"}
    assert {(r["stage"], r["done"]) for r in seen["flow"]} <= {("flow", 0), ("flow", 1), ("flow", 2), ("flow", 3)}
    assert all(r["total"] == 3 and r["stage"] == "flow" for r in seen["flow"])  # 수급은 통과한 3종목에만
    assert all(r["total"] == 3 and r["stage"] == "sector" for r in seen["sector"])
    assert row(pg_conn)["state"] == "ready"


def test_the_price_progress_counts_up_stock_by_stock(client, externals, pg_conn):
    done_values = []
    swap(client, prices=spy(externals.prices, lambda: done_values.append((row(pg_conn) or {}).get("done"))))
    post(client)
    seen = [d for d in done_values if d is not None]
    assert seen == sorted(seen) and max(seen) <= 3  # 줄어들지 않는다


def test_progress_is_written_to_the_database_at_most_once_in_a_while(client, externals, pg_conn):
    client.app.state.deps = replace(client.app.state.deps, progress_interval=1000)  # 사실상 단계가 바뀔 때만 적는다
    prices_rows = []
    swap(client, prices=spy(externals.prices, lambda: prices_rows.append(row(pg_conn))))
    post(client)
    assert {r["done"] for r in prices_rows if r} == {0}  # 종목마다 적지 않는다
    assert row(pg_conn)["state"] == "ready" and row(pg_conn)["done"] == 3  # 끝나면 마지막 숫자는 맞다


# --- 만드는 동안에는 처리·정리를 못 한다 ---


def test_a_session_that_is_being_built_cannot_be_processed_or_organized(client, externals, pg_conn):
    answers = []

    def try_both():
        answers.append(client.post("/api/sessions/latest/process", json={"offset": 0, "count": 1}))
        answers.append(client.post("/api/sessions/latest/organize"))

    swap(client, trend=spy(externals.trend, once(try_both)))
    post(client)
    process, organize = answers
    assert (process.status_code, process.json()) == (409, {"detail": "목록을 만드는 중입니다. 끝난 뒤에 처리할 수 있습니다."})
    assert (organize.status_code, organize.json()) == (409, {"detail": "목록을 만드는 중입니다. 끝난 뒤에 정리할 수 있습니다.", "remaining": 0})


# --- 같은 기준일을 두 번 만들지 않는다 ---


def test_asking_again_for_the_same_date_while_it_is_being_built_is_a_409_with_the_running_session(client, externals, pg_conn):
    asked = []
    swap(client, trend=spy(externals.trend, once(lambda: asked.append(post(client)))))  # 수급 단계에서 한 번만 다시 묻는다
    first = post(client)
    second = asked[0]
    assert second.status_code == 409
    assert second.json()["session_id"] == first.json()["id"]
    assert "이미" in second.json()["detail"] and AS_OF in second.json()["detail"]
    assert len(client.get("/api/sessions").json()) == 1  # 중복으로 만들지 않았다


def test_another_date_can_be_built_at_the_same_time(client, externals, pg_conn):
    asked = []
    swap(client, trend=spy(externals.trend, once(lambda: asked.append(post(client, "2026-11-04")))))
    post(client)
    assert asked[0].status_code == 202  # 다른 기준일은 같이 만들 수 있다


def test_a_date_that_already_has_a_finished_list_can_be_made_again_as_a_separate_session(client):
    first, second = post(client).json(), post(client).json()
    assert first["id"] != second["id"]
    assert [s["build"]["state"] for s in client.get("/api/sessions").json()] == ["ready", "ready"]


def test_the_lock_is_released_when_the_build_is_over(client, repo):
    created = post(client).json()
    assert repo.is_locked(uuid.UUID(created["id"])) is False
    assert detail(client, created["id"])["processing"] is False


# --- 중간에 멈춘 목록 ---


def test_a_list_whose_server_stopped_halfway_is_shown_as_interrupted(client, repo):
    stale = repo.create_building_session(uuid.uuid4(), date(2026, 11, 5))  # 만들던 서버가 죽어서 잠금이 없다
    body = detail(client, str(stale.id))
    assert body["session"]["build"]["state"] == "interrupted"
    assert client.get("/api/sessions").json()[0]["build"]["state"] == "interrupted"
    response = client.post(f"/api/sessions/{stale.id}/process", json={"offset": 0, "count": 1})
    assert (response.status_code, response.json()) == (409, {"detail": "목록을 만들다 서버가 멈춰 완성되지 않았습니다. 같은 기준일로 다시 뽑으세요."})


def test_a_list_that_is_really_being_built_is_not_taken_for_interrupted(client, repo, hold_lock):
    live = repo.create_building_session(uuid.uuid4(), date(2026, 11, 5))
    hold_lock(live.id)  # 만드는 연결이 잠금을 쥐고 있다
    assert detail(client, str(live.id))["session"]["build"]["state"] == "building"
    assert post(client).status_code == 409  # 같은 기준일은 새로 만들 수 없다


def test_asking_again_removes_the_interrupted_list_of_that_date_only(client, repo):
    stale = repo.create_building_session(uuid.uuid4(), date(2026, 11, 5))
    other = repo.create_building_session(uuid.uuid4(), date(2026, 11, 4))  # 다른 기준일의 중단된 목록은 건드리지 않는다
    created = post(client).json()
    ids = {s["id"] for s in client.get("/api/sessions").json()}
    assert str(stale.id) not in ids and created["id"] in ids and str(other.id) in ids


# --- 실패 ---


def test_when_the_listing_cannot_be_received_the_session_is_failed_with_a_korean_reason(client, pg_conn):
    def boom():
        raise UniverseFetchError("종목 목록을 받지 못했습니다 (ConnectionError: 연결 실패)")

    swap(client, listing=boom)
    created = post(client)
    assert created.status_code == 202  # 응답은 이미 나갔다
    build = detail(client, created.json()["id"])["session"]["build"]
    assert build == {"state": "failed", "stage": "", "done": 0, "total": 0, "error": "종목 목록을 받지 못했습니다 (ConnectionError: 연결 실패)"}
    assert row(pg_conn)["items"] == 0


def test_an_unexpected_error_is_a_generic_korean_reason_and_the_lock_is_released(client, repo):
    def boom():
        raise RuntimeError("내부 오류 세부")

    swap(client, listing=boom)
    created = post(client).json()
    build = detail(client, created["id"])["session"]["build"]
    assert (build["state"], build["error"]) == ("failed", "목록을 만드는 중 오류가 났습니다 (RuntimeError).")  # 세부는 사용자에게 보이지 않는다
    assert repo.is_locked(uuid.UUID(created["id"])) is False


def test_a_failed_list_cannot_be_processed_and_asking_again_replaces_it(client, externals):
    original = externals.listing
    swap(client, listing=lambda: (_ for _ in ()).throw(UniverseFetchError("받지 못했습니다")))
    failed = post(client).json()
    response = client.post(f"/api/sessions/{failed['id']}/process", json={"offset": 0, "count": 1})
    assert (response.status_code, response.json()) == (409, {"detail": "목록을 만들지 못했습니다: 받지 못했습니다"})
    swap(client, listing=original)
    again = post(client).json()
    ids = [s["id"] for s in client.get("/api/sessions").json()]
    assert ids == [again["id"]]  # 실패한 목록은 같은 기준일로 다시 뽑으면 지워진다
    assert detail(client, again["id"])["session"]["build"]["state"] == "ready"


# --- 요청을 받는 자리에서 거르는 것은 그대로 ---


def test_a_bad_date_is_still_refused_at_once_and_no_session_is_made(client, pg_conn):
    response = post(client, "2026-11-08")  # 일요일
    assert response.status_code == 422
    assert pg_conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_a_date_whose_trading_day_cannot_be_checked_is_a_503_and_no_session_is_made(client, externals, pg_conn):
    from krx_screener.prices import PriceFetchError

    def down(*args):
        raise PriceFetchError("시세 서버 오류")

    swap(client, prices=down)
    assert post(client).status_code == 503
    assert pg_conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
