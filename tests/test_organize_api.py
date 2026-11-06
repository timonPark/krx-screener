"""정리 API (#24): ``POST /api/sessions/{id}/organize``. 웹 API 경계에서 검증한다.

세션은 저장소로 직접 만든다 (점수를 정확히 정해 두고 순위를 본다). 순번 1~5 = 종목코드 000010~000050.
"""

import uuid
from datetime import date

import pytest
from test_process_api import CODES, seed

from krx_screener.repository import Item


def organize(client, session_id):
    return client.post(f"/api/sessions/{session_id}/organize")


def finish(repo, session, code, score, rated=10):
    """종목을 점수와 함께 끝난 상태로 만든다 (``score=None`` 이면 에이전트 등급을 못 읽은 종목)."""
    repo.mark_done(session.id, code, decision="Hold", score=score, agents_rated=rated if score is not None else 0, agents_total=10)


def finish_all(repo, session, scores):
    for code, score in zip(CODES, scores, strict=True):
        finish(repo, session, code, score)


def ranks(repo, session):
    return {i.code: i.rank for i in repo.items(session.id)}


def view(client, session):
    return client.get(f"/api/sessions/{session.id}").json()


# --- 순위 ---


def test_stocks_are_ranked_by_score_from_the_highest(client, repo):
    session = seed(repo)
    finish_all(repo, session, [0.1, 0.9, -0.4, 0.5, -0.8])
    response = organize(client, session.id)
    assert (response.status_code, response.json()) == (200, {"ranked": 5, "unscored": 0})
    assert ranks(repo, session) == {"000020": 1, "000040": 2, "000010": 3, "000030": 4, "000050": 5}


def test_a_tie_is_broken_by_the_earlier_sequence_number(client, repo):
    session = seed(repo)
    finish_all(repo, session, [0.5, -0.2, 0.5, 0.5, -0.2])  # 순번 1·3·4 가 동점, 2·5 가 동점
    organize(client, session.id)
    assert ranks(repo, session) == {"000010": 1, "000030": 2, "000040": 3, "000020": 4, "000050": 5}
    # 동점 묶기는 없다: 순위는 1부터 빠짐없이 이어진다
    assert sorted(ranks(repo, session).values()) == [1, 2, 3, 4, 5]


def test_the_same_average_from_a_different_number_of_agents_is_a_tie(client, repo):
    from krx_screener.scoring import score_ratings

    # 읽은 2개의 합 2 → 1.0, 읽은 4개의 합 4 → 1.0. 정확히 같은 수여야 순번 순서가 지켜진다 (#22 의 계산 방식)
    two = score_ratings({"market": "Buy", "news": "Hold"}).score
    four = score_ratings(dict.fromkeys(["market", "news", "bull", "bear"], "Overweight")).score
    assert two == four == 1.0
    session = seed(repo)
    for code, score in zip(CODES, [0.0, four, two, -1.0, -2.0], strict=True):
        finish(repo, session, code, score)
    organize(client, session.id)
    assert ranks(repo, session)["000020"] == 1 and ranks(repo, session)["000030"] == 2  # 순번이 앞선 쪽이 위


def test_the_response_view_shows_the_ranks_and_when_the_ranking_was_made(client, repo):
    session = seed(repo)
    assert view(client, session)["ranked_at"] is None and all(i["rank"] is None for i in view(client, session)["items"])
    finish_all(repo, session, [0.1, 0.9, -0.4, 0.5, -0.8])
    organize(client, session.id)
    body = view(client, session)
    assert body["ranked_at"] is not None and body["ranked_at"].endswith("Z")
    assert [i["rank"] for i in body["items"]] == [3, 1, 4, 2, 5]
    assert body["items"][0]["score"] == 0.1  # 순위를 매겨도 점수는 그대로


# --- 게이팅 ---


def test_when_stocks_remain_nothing_is_ranked_and_the_remaining_count_is_returned(client, repo):
    session = seed(repo)
    finish(repo, session, "000010", 0.5)
    finish(repo, session, "000020", 0.1)
    repo.mark_running(session.id, "000030")  # 처리 중도 남은 것이다. 000040·000050 은 대기
    response = organize(client, session.id)
    assert response.status_code == 409
    assert response.json() == {"detail": "남은 3개가 끝나야 정리할 수 있습니다.", "remaining": 3}
    assert set(ranks(repo, session).values()) == {None}
    assert view(client, session)["ranked_at"] is None


def test_one_remaining_stock_is_enough_to_refuse(client, repo):
    session = seed(repo)
    for code in CODES[:4]:
        finish(repo, session, code, 0.0)
    response = organize(client, session.id)
    assert (response.status_code, response.json()["remaining"]) == (409, 1)


def test_a_session_nobody_has_processed_refuses_with_every_stock_remaining(client, repo):
    session = seed(repo)
    assert organize(client, session.id).json()["remaining"] == 5


# --- 실패 ---


def test_failed_stocks_are_left_out_of_the_ranking_and_do_not_block_it(client, repo):
    session = seed(repo)
    finish_all(repo, session, [0.1, 0.9, -0.4, 0.5, -0.8])
    repo.mark_failed(session.id, "000020", "가짜 서버 오류")  # 점수가 가장 높았지만 실패한 종목
    repo.mark_failed(session.id, "000050", "시간 초과")
    response = organize(client, session.id)
    assert (response.status_code, response.json()) == (200, {"ranked": 3, "unscored": 0})
    assert ranks(repo, session) == {"000040": 1, "000010": 2, "000030": 3, "000020": None, "000050": None}


def test_a_failed_stock_that_still_has_an_old_score_is_not_ranked(client, repo):
    session = seed(repo)
    finish_all(repo, session, [0.1, 0.2, 0.3, 0.4, 0.5])
    repo.mark_failed(session.id, "000050", "재분석 실패")  # 옛 점수 0.5 가 남아 있지만 실패 상태다 (#19)
    organize(client, session.id)
    assert repo.get_item(session.id, "000050").score == 0.5
    assert repo.get_item(session.id, "000050").rank is None
    assert repo.get_item(session.id, "000040").rank == 1


def test_when_every_stock_failed_the_ranking_is_empty_and_still_succeeds(client, repo):
    session = seed(repo)
    for code in CODES:
        repo.mark_failed(session.id, code, "실패")
    response = organize(client, session.id)
    assert (response.status_code, response.json()) == (200, {"ranked": 0, "unscored": 0})
    assert view(client, session)["ranked_at"] is not None


# --- 점수가 없는 종목 ---


def test_a_finished_stock_without_a_score_is_not_ranked_and_is_reported(client, repo):
    session = seed(repo)
    finish_all(repo, session, [0.1, None, 0.3, None, -0.5])  # 에이전트 등급을 하나도 못 읽은 종목 2개
    response = organize(client, session.id)
    assert (response.status_code, response.json()) == (200, {"ranked": 3, "unscored": 2})
    assert ranks(repo, session) == {"000030": 1, "000010": 2, "000050": 3, "000020": None, "000040": None}  # 점수를 0 으로 지어내지 않는다


# --- 다시 정리 ---


def test_organizing_again_makes_the_ranking_from_scratch(client, repo):
    session = seed(repo)
    finish_all(repo, session, [0.1, 0.9, -0.4, 0.5, -0.8])
    organize(client, session.id)
    first = repo.get_session(session.id).ranked_at  # 응답은 초 단위라 저장된 시각으로 비교한다
    # 그 사이에 재분석: 순번 1 이 최고점이 되고, 순번 2 는 실패했다
    finish(repo, session, "000010", 1.5)
    repo.mark_failed(session.id, "000020", "재분석 실패")
    response = organize(client, session.id)
    assert response.json() == {"ranked": 4, "unscored": 0}
    assert ranks(repo, session) == {"000010": 1, "000040": 2, "000030": 3, "000050": 4, "000020": None}  # 실패한 종목의 옛 순위는 지워진다
    assert repo.get_session(session.id).ranked_at > first  # 정리 시각이 새로 적힌다


def test_organizing_twice_without_changes_gives_the_same_ranking(client, repo):
    session = seed(repo)
    finish_all(repo, session, [0.5, 0.5, 0.1, 0.1, 0.1])
    organize(client, session.id)
    before = ranks(repo, session)
    organize(client, session.id)
    assert ranks(repo, session) == before


def test_other_sessions_are_not_touched(client, repo):
    older, newer = seed(repo, date(2026, 11, 4)), seed(repo, date(2026, 11, 5))
    finish_all(repo, older, [0.1, 0.2, 0.3, 0.4, 0.5])
    finish_all(repo, newer, [0.5, 0.4, 0.3, 0.2, 0.1])
    organize(client, newer.id)
    assert set(ranks(repo, older).values()) == {None}
    assert repo.get_session(older.id).ranked_at is None


# --- 처리와의 관계 ---


def test_a_session_that_is_being_processed_cannot_be_organized(client, repo, hold_lock):
    session = seed(repo)
    finish_all(repo, session, [0.1, 0.2, 0.3, 0.4, 0.5])  # 남은 것은 없지만 처리 연결이 아직 열려 있다
    holder = hold_lock(session.id)
    response = organize(client, session.id)
    assert (response.status_code, response.json()) == (409, {"detail": "이미 처리 중입니다.", "remaining": 0})
    assert set(ranks(repo, session).values()) == {None}
    holder.close()
    assert organize(client, session.id).status_code == 200  # 처리가 끝나면 정리할 수 있다


def test_organizing_leaves_no_lock_behind(client, repo):
    session = seed(repo)
    finish_all(repo, session, [0.1, 0.2, 0.3, 0.4, 0.5])
    organize(client, session.id)
    assert repo.is_locked(session.id) is False
    assert view(client, session)["processing"] is False
    organize(client, session.id)  # 잠금이 남아 있으면 두 번째가 409 가 된다
    assert repo.is_locked(session.id) is False


def test_a_refused_organize_leaves_no_lock_behind(client, repo):
    session = seed(repo)
    assert organize(client, session.id).status_code == 409
    assert repo.is_locked(session.id) is False


# --- 세션 찾기와 입력 ---


def test_latest_means_the_most_recent_session(client, repo):
    older, newer = seed(repo, date(2026, 11, 4)), seed(repo, date(2026, 11, 5))
    finish_all(repo, newer, [0.1, 0.2, 0.3, 0.4, 0.5])
    assert organize(client, "latest").json() == {"ranked": 5, "unscored": 0}
    assert set(ranks(repo, older).values()) == {None}


@pytest.mark.parametrize("session_id", [str(uuid.uuid4()), "abc"])
def test_an_unknown_session_is_a_korean_404(client, session_id):
    response = organize(client, session_id)
    assert (response.status_code, response.json()) == (404, {"detail": "세션을 찾지 못했습니다."})


def test_there_is_no_latest_session_when_none_exist(client):
    assert organize(client, "latest").status_code == 404


def test_a_session_with_no_stocks_is_ranked_as_empty(client, repo):
    session = repo.create_session(date(2026, 11, 5))
    assert organize(client, session.id).json() == {"ranked": 0, "unscored": 0}


def test_a_session_with_one_waiting_stock_is_refused(client, repo):
    session = repo.create_session(date(2026, 11, 5))
    repo.add_items(session.id, [Item(seq=1, code="000010", name="가", market="KOSPI")])
    assert organize(client, session.id).status_code == 409  # 대기 1개


def test_when_the_database_is_down_it_is_a_korean_503(tmp_path):
    from fakes import make_externals, repo_config
    from fastapi.testclient import TestClient

    from krx_screener.api import Dependencies, create_app
    from krx_screener.db import DatabaseError

    def broken():
        raise DatabaseError("PostgreSQL 에 연결하지 못했습니다 (127.0.0.1:1/krx).")

    deps = Dependencies(connect=broken, externals=make_externals(tmp_path), config=repo_config())
    response = TestClient(create_app(deps)).post(f"/api/sessions/{uuid.uuid4()}/organize")
    assert (response.status_code, response.json()) == (503, {"detail": "PostgreSQL 에 연결하지 못했습니다 (127.0.0.1:1/krx)."})


# --- 처리부터 정리까지 ---


def test_from_processing_to_ranking_through_the_api(client, repo, tmp_path):
    from fakes import FakeAnalysisServer
    from test_process_api import post, swap_server

    session = seed(repo)
    swap_server(
        client,
        FakeAnalysisServer(
            errors=["000030"],  # 실패
            agent_ratings={
                "000010": {"market": "Buy", "news": "Buy"},  # 0.4
                "000020": {"market": "Sell"},  # −0.2
                "000040": dict.fromkeys(["market", "news", "bull", "bear"], "Overweight"),  # 0.4 (순번 1 과 동점)
            },
        ),
    )
    assert organize(client, session.id).status_code == 409  # 처리 전에는 정리할 수 없다
    post(client, session.id, offset=0, count=4)
    assert organize(client, session.id).json()["remaining"] == 1  # 순번 5 는 아직 대기
    post(client, session.id, offset=4, count=1)
    response = organize(client, session.id)
    assert (response.status_code, response.json()) == (200, {"ranked": 4, "unscored": 0})
    assert ranks(repo, session) == {"000010": 1, "000040": 2, "000050": 3, "000020": 4, "000030": None}
    items = view(client, session)["items"]
    assert [(i["code"], i["state"], i["rank"]) for i in items] == [
        ("000010", "done", 1), ("000020", "done", 4), ("000030", "failed", None), ("000040", "done", 2), ("000050", "done", 3),
    ]
