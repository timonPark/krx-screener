"""순위 낡음 표시 (#25): 정리 뒤에 종목이 다시 분석되면 조회의 ``rank_stale`` 이 true 가 된다.

낡음은 "정리 시각 뒤에 분석한 종목이 있어서 지금 정리하면 순위가 달라질 수 있다" 는 뜻이다.
순위가 달라질 수 없는 재시도(실패했던 종목이 또 실패)는 낡음이 아니다.
"""

from datetime import date

from fakes import FakeAnalysisServer
from test_organize_api import finish, finish_all, organize, ranks
from test_process_api import post_failed, seed, swap_server


def stale(client, session):
    body = client.get(f"/api/sessions/{session.id}").json()
    return body["rank_stale"]


def organized(client, repo, scores=(0.1, 0.9, -0.4, 0.5, -0.8)):
    session = seed(repo)
    finish_all(repo, session, scores)
    assert organize(client, session.id).status_code == 200
    return session


# --- 이슈의 완료 조건 ---


def test_it_is_false_before_a_ranking_and_right_after_one(client, repo):
    session = seed(repo)
    assert stale(client, session) is False  # 정리 전
    finish_all(repo, session, [0.1, 0.9, -0.4, 0.5, -0.8])
    assert stale(client, session) is False  # 정리 전에는 분석이 있어도 낡을 순위가 없다
    organize(client, session.id)
    assert stale(client, session) is False  # 정리 직후


def test_reanalyzing_after_the_ranking_makes_it_stale_and_organizing_again_makes_it_fresh(client, repo):
    session = seed(repo)
    finish_all(repo, session, [0.1, 0.9, -0.4, 0.5, -0.8])
    repo.mark_failed(session.id, "000030", "첫 분석 실패")  # 정리할 때 이 종목은 실패로 빠져 있었다
    organize(client, session.id)
    assert stale(client, session) is False
    swap_server(client, FakeAnalysisServer(agent_ratings={"000030": {"market": "Buy"}}))
    assert post_failed(client, session.id).json() == {"started": True, "queued": 1}  # 실패한 종목을 다시 처리
    assert stale(client, session) is True  # 정리 뒤에 다시 분석됐다
    assert organize(client, session.id).json() == {"ranked": 5, "unscored": 0}
    assert stale(client, session) is False  # 다시 정리하면 최신
    assert ranks(repo, session)["000030"] is not None  # 이제 순위에 들어 있다


# --- 순위가 달라질 수 있는 재분석 ---


def test_a_ranked_stock_that_is_analyzed_again_makes_it_stale_whether_it_succeeds_or_fails(client, repo):
    session = organized(client, repo)
    finish(repo, session, "000010", 1.5)  # 점수가 바뀐 재분석
    assert stale(client, session) is True
    organize(client, session.id)
    assert stale(client, session) is False
    repo.mark_failed(session.id, "000020", "재분석 실패")  # 순위에 들어 있던 종목이 실패 → 비교에서 빠져야 한다
    assert stale(client, session) is True


def test_an_unranked_stock_that_becomes_rankable_makes_it_stale(client, repo):
    session = seed(repo)
    finish_all(repo, session, [0.1, None, 0.3, 0.4, 0.5])  # 순번 2 는 점수가 없어 순위에서 빠진다
    organize(client, session.id)
    finish(repo, session, "000020", 0.9)  # 이제 점수가 있다
    assert stale(client, session) is True


# --- 순위가 달라질 수 없는 재시도는 낡음이 아니다 ---


def test_a_failed_stock_that_fails_again_does_not_make_it_stale(client, repo):
    session = seed(repo)
    finish_all(repo, session, [0.1, 0.9, -0.4, 0.5, -0.8])
    repo.mark_failed(session.id, "000030", "첫 분석 실패")
    organize(client, session.id)
    swap_server(client, FakeAnalysisServer(errors=["000030"]))
    post_failed(client, session.id)  # 다시 처리했지만 또 실패
    assert repo.get_item(session.id, "000030").state == "failed"
    assert stale(client, session) is False  # 실패는 전에도 비교에서 빠져 있었다. 다시 정리해도 같다
    before = ranks(repo, session)
    organize(client, session.id)
    assert ranks(repo, session) == before


def test_a_stock_without_a_score_that_still_has_none_does_not_make_it_stale(client, repo):
    session = seed(repo)
    finish_all(repo, session, [0.1, None, 0.3, 0.4, 0.5])
    organize(client, session.id)
    finish(repo, session, "000020", None)  # 다시 분석했지만 여전히 점수가 없다
    assert stale(client, session) is False


def test_a_stock_without_a_score_that_then_fails_does_not_make_it_stale(client, repo):
    session = seed(repo)
    finish_all(repo, session, [0.1, None, 0.3, 0.4, 0.5])
    organize(client, session.id)
    repo.mark_failed(session.id, "000020", "재분석 실패")
    assert stale(client, session) is False


# --- 그 밖에 ---


def test_the_flag_stays_while_nothing_is_organized_again(client, repo):
    session = organized(client, repo)
    finish(repo, session, "000010", 1.5)
    assert stale(client, session) is True
    assert stale(client, session) is True  # 조회는 낡음을 바꾸지 않는다


def test_another_session_is_not_made_stale(client, repo):
    older = seed(repo, date(2026, 11, 4))
    finish_all(repo, older, [0.1, 0.2, 0.3, 0.4, 0.5])
    organize(client, older.id)
    newer = seed(repo, date(2026, 11, 5))
    finish_all(repo, newer, [0.5, 0.4, 0.3, 0.2, 0.1])
    organize(client, newer.id)
    finish(repo, newer, "000010", 2.0)
    assert stale(client, newer) is True
    assert stale(client, older) is False


def test_latest_shows_the_flag_too(client, repo):
    session = organized(client, repo)
    finish(repo, session, "000010", 1.5)
    assert client.get("/api/sessions/latest").json()["rank_stale"] is True


def test_a_session_with_only_failed_stocks_goes_stale_when_one_succeeds_later(client, repo):
    session = seed(repo)
    for code in ("000010", "000020", "000030", "000040", "000050"):
        repo.mark_failed(session.id, code, "실패")
    organize(client, session.id)  # 순위가 빈 정리
    assert stale(client, session) is False
    swap_server(client, FakeAnalysisServer(errors=["000020", "000030", "000040", "000050"]))
    post_failed(client, session.id)  # 하나(000010)만 성공 → 순위에 들어가야 한다
    assert stale(client, session) is True
