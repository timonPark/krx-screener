"""세션·종목·에이전트 등급 저장소 계층 (#10). 테스트 전용 DB 가 있을 때만 돈다 (conftest 참고)."""

import uuid
from datetime import UTC, date, datetime

import psycopg
import pytest

from krx_screener.repository import Item, NotFoundError

AS_OF = date(2026, 11, 5)


def item(seq: int, code: str, **extra) -> Item:
    return Item(seq=seq, code=code, name=f"종목{code}", market="KOSPI", **extra)


def seeded(repo, codes=("028050", "207940", "003490")):
    session = repo.create_session(AS_OF)
    repo.add_items(session.id, [item(i, c) for i, c in enumerate(codes, start=1)])
    return session


# --- 세션 ---


def test_new_session_has_its_own_uuid_and_the_date(repo):
    first, second = repo.create_session(AS_OF), repo.create_session(AS_OF)  # 같은 날 두 번
    assert first.id != second.id
    assert isinstance(first.id, uuid.UUID)
    assert (first.as_of, first.ranked_at, first.total) == (AS_OF, None, 0)
    assert first.created_at is not None


def test_latest_session_is_the_most_recent_one(repo):
    assert repo.latest_session() is None
    older = repo.create_session(date(2026, 11, 1))
    newer = repo.create_session(date(2026, 11, 5))
    assert repo.latest_session().id == newer.id
    assert [s.id for s in repo.list_sessions()] == [newer.id, older.id]  # 최근 순


def test_unknown_session_is_none(repo):
    assert repo.get_session(uuid.uuid4()) is None


def test_session_total_counts_its_items(repo):
    session = seeded(repo)
    assert repo.get_session(session.id).total == 3
    assert repo.latest_session().total == 3


# --- 종목 ---


def test_items_come_back_in_order_with_defaults(repo):
    session = repo.create_session(AS_OF)
    repo.add_items(session.id, [item(2, "207940"), item(1, "028050")])  # 순서를 뒤집어 넣어도
    items = repo.items(session.id)
    assert [i.seq for i in items] == [1, 2]
    first = items[0]
    assert (first.state, first.flow_status, first.decision, first.score, first.rank) == (
        "pending", "unconfirmed", "", None, None,
    )
    assert (first.is_caution, first.is_foreign, first.error_message, first.report_id) == (False, False, "", "")


def test_listed_values_round_trip_including_missing_ones(repo):
    session = repo.create_session(AS_OF)
    full = item(
        1, "028050", close=64800, avg_trading_value=201_712_536_740, rsi=67.2, flow_status="partial",
        foreign_net_5d=-624_606, institution_net_5d=645_940, sector_code="26", sector_name="건설",
        description="플랜트 EPC", is_caution=True, is_foreign=False,
    )
    unread = item(2, "207940", flow_status="unconfirmed")  # 수급을 읽지 못함: 합계는 0 이 아니라 빈 값
    repo.add_items(session.id, [full, unread])
    stored_full, stored_unread = repo.items(session.id)
    assert stored_full == full
    assert (stored_unread.foreign_net_5d, stored_unread.institution_net_5d) == (None, None)


def test_a_bad_row_leaves_nothing_behind(repo):
    session = repo.create_session(AS_OF)
    with pytest.raises(psycopg.errors.UniqueViolation):
        repo.add_items(session.id, [item(1, "028050"), item(2, "028050")])  # 같은 종목 두 번
    assert repo.items(session.id) == []


def test_adding_items_to_an_unknown_session_is_a_korean_error(repo):
    with pytest.raises(NotFoundError, match="세션을 찾지 못했습니다"):
        repo.add_items(uuid.uuid4(), [item(1, "028050")])


def test_the_same_code_can_live_in_different_sessions(repo):
    first, second = seeded(repo), seeded(repo)
    assert len(repo.items(first.id)) == len(repo.items(second.id)) == 3


def test_get_item(repo):
    session = seeded(repo)
    assert repo.get_item(session.id, "207940").seq == 2
    assert repo.get_item(session.id, "999999") is None


def test_counts_fill_every_state_with_zero(repo):
    session = seeded(repo)
    assert repo.counts(session.id) == {"pending": 3, "running": 0, "done": 0, "failed": 0}
    assert repo.counts(repo.create_session(AS_OF).id) == {"pending": 0, "running": 0, "done": 0, "failed": 0}


# --- 처리 상태와 결과 ---


def test_running_then_done_records_the_result(repo):
    session = seeded(repo)
    repo.mark_running(session.id, "028050")
    assert repo.get_item(session.id, "028050").state == "running"

    when = datetime(2026, 11, 6, 1, 20, tzinfo=UTC)
    repo.mark_done(
        session.id, "028050", decision="Hold", score=0.3, agents_rated=10, agents_total=10,
        report_id="028050_20261106_172435", analyzed_at=when,
    )
    done = repo.get_item(session.id, "028050")
    assert (done.state, done.decision, done.score, done.agents_rated, done.agents_total) == ("done", "Hold", 0.3, 10, 10)
    assert (done.report_id, done.analyzed_at) == ("028050_20261106_172435", when)
    assert repo.counts(session.id) == {"pending": 2, "running": 0, "done": 1, "failed": 0}


def test_done_without_a_time_uses_now(repo):
    session = seeded(repo)
    repo.mark_done(session.id, "028050", decision="Buy", score=None, agents_rated=0, agents_total=10)
    done = repo.get_item(session.id, "028050")
    assert done.analyzed_at is not None
    assert done.score is None  # 점수를 계산하지 못했으면 빈 값


def test_failed_keeps_the_reason_and_a_retry_clears_it(repo):
    session = seeded(repo)
    repo.mark_failed(session.id, "028050", "시간 초과 (900초)")
    failed = repo.get_item(session.id, "028050")
    assert (failed.state, failed.error_message) == ("failed", "시간 초과 (900초)")
    assert failed.analyzed_at is not None

    repo.mark_running(session.id, "028050")  # 재시도하면 사유가 지워진다
    assert repo.get_item(session.id, "028050").error_message == ""
    repo.mark_done(session.id, "028050", decision="Hold", score=0.0, agents_rated=10, agents_total=10)
    assert repo.get_item(session.id, "028050").error_message == ""


def test_failing_does_not_erase_a_stored_rating(repo):
    session = seeded(repo)
    repo.mark_done(session.id, "028050", decision="Buy", score=1.2, agents_rated=10, agents_total=10)
    repo.mark_failed(session.id, "028050", "다시 분석하다 실패")
    kept = repo.get_item(session.id, "028050")
    assert (kept.decision, kept.score) == ("Buy", 1.2)


def test_updates_stay_inside_their_session(repo):
    first, second = seeded(repo), seeded(repo)
    repo.mark_done(first.id, "028050", decision="Sell", score=-2.0, agents_rated=10, agents_total=10)
    assert repo.get_item(second.id, "028050").state == "pending"
    assert repo.counts(second.id)["done"] == 0


def test_updating_an_unknown_item_is_a_korean_error(repo):
    session = seeded(repo)
    for call in (
        lambda: repo.mark_running(session.id, "999999"),
        lambda: repo.mark_failed(session.id, "999999", "x"),
        lambda: repo.mark_done(session.id, "999999", decision="Hold", score=0, agents_rated=1, agents_total=1),
    ):
        with pytest.raises(NotFoundError, match="세션에 없는 종목입니다: 999999"):
            call()


def test_the_database_rejects_an_unknown_decision(repo):
    session = seeded(repo)
    with pytest.raises(psycopg.errors.CheckViolation):
        repo.mark_done(session.id, "028050", decision="StrongBuy", score=0, agents_rated=1, agents_total=1)


# --- 에이전트별 등급 ---


def test_agent_ratings_round_trip(repo):
    session = seeded(repo)
    repo.save_agent_ratings(session.id, "028050", {"bull": "Overweight", "bear": "Underweight", "pm": "Hold"})
    assert repo.agent_ratings(session.id, "028050") == {"bear": "Underweight", "bull": "Overweight", "pm": "Hold"}
    assert repo.agent_ratings(session.id, "207940") == {}  # 다른 종목은 비어 있다


def test_saving_again_replaces_the_previous_ratings(repo):
    session = seeded(repo)
    repo.save_agent_ratings(session.id, "028050", {"bull": "Buy", "bear": "Sell"})
    repo.save_agent_ratings(session.id, "028050", {"pm": "Hold"})
    assert repo.agent_ratings(session.id, "028050") == {"pm": "Hold"}


def test_a_bad_rating_keeps_the_previous_ones(repo):
    session = seeded(repo)
    repo.save_agent_ratings(session.id, "028050", {"bull": "Buy", "pm": "Hold"})
    with pytest.raises(psycopg.errors.CheckViolation):
        repo.save_agent_ratings(session.id, "028050", {"bull": "Sell", "bear": "Maybe"})
    assert repo.agent_ratings(session.id, "028050") == {"bull": "Buy", "pm": "Hold"}


def test_ratings_for_an_unknown_item_are_a_korean_error(repo):
    session = seeded(repo)
    with pytest.raises(NotFoundError, match="세션에 없는 종목입니다: 999999"):
        repo.save_agent_ratings(session.id, "999999", {"bull": "Buy"})


def test_ratings_stay_inside_their_session(repo):
    first, second = seeded(repo), seeded(repo)
    repo.save_agent_ratings(first.id, "028050", {"bull": "Buy"})
    assert repo.agent_ratings(second.id, "028050") == {}
