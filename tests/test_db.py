"""PostgreSQL 연결과 스키마 (#9).

연결 오류 메시지 테스트는 DB 없이 돌고, 스키마 테스트는 테스트 전용 DB 가 있을 때만 돈다.
테스트 전용 DB 는 ``docker compose up -d`` 로 띄우고 다음 환경변수를 준다.

    KRX_SCREENER_TEST_DATABASE_URL=postgresql://krx:krx@127.0.0.1:5433/krx_screener_test
"""

import os
import uuid

import psycopg
import pytest

from krx_screener import db

TEST_URL = os.environ.get("KRX_SCREENER_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not TEST_URL, reason="KRX_SCREENER_TEST_DATABASE_URL 이 없음")


# --- DB 없이 도는 테스트 ---


def test_missing_url_explains_how_to_set_it():
    with pytest.raises(db.DatabaseError) as info:
        db.database_url({})
    message = str(info.value)
    assert db.ENV_URL in message
    assert "접속 정보가 없습니다" in message


def test_blank_url_is_treated_as_missing():
    with pytest.raises(db.DatabaseError, match="접속 정보가 없습니다"):
        db.database_url({db.ENV_URL: "   "})


def test_url_is_read_from_the_environment():
    assert db.database_url({db.ENV_URL: "postgresql://u@h/d"}) == "postgresql://u@h/d"


def test_unreachable_server_gives_korean_error_without_the_password():
    url = "postgresql://someone:s3cret-pw@127.0.0.1:1/krx"  # 1번 포트는 아무도 듣지 않는다
    with pytest.raises(db.DatabaseError) as info:
        db.connect(url, connect_timeout=2)
    message = str(info.value)
    assert "연결하지 못했습니다" in message
    assert "127.0.0.1:1/krx" in message
    assert "s3cret-pw" not in message
    assert "someone" not in message


def test_malformed_url_gives_a_format_error():
    with pytest.raises(db.DatabaseError, match="형식이 올바르지 않습니다"):
        db.connect("이건 주소가 아닙니다")


def test_cli_without_url_exits_with_an_error(monkeypatch, capsys):
    monkeypatch.delenv(db.ENV_URL, raising=False)
    assert db.main(["init"]) == 1
    assert "접속 정보가 없습니다" in capsys.readouterr().err


def test_cli_rejects_unknown_arguments(capsys):
    assert db.main(["drop"]) == 2
    assert "사용법" in capsys.readouterr().err


# --- 테스트 전용 DB 가 있을 때만 도는 테스트 ---


@pytest.fixture
def conn():
    """깨끗한 테스트 DB 연결. 테스트 전용 DB 라서 매번 스키마를 비운다."""
    connection = db.connect(TEST_URL)
    connection.autocommit = True
    connection.execute("DROP SCHEMA public CASCADE")
    connection.execute("CREATE SCHEMA public")
    yield connection
    connection.execute("DROP SCHEMA public CASCADE")
    connection.execute("CREATE SCHEMA public")
    connection.close()


def _tables(conn):
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    return {r[0] for r in rows}


def _columns(conn, table):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    )
    return {r[0] for r in rows}


def _session(conn) -> uuid.UUID:
    sid = uuid.uuid4()
    conn.execute("INSERT INTO sessions (id, as_of) VALUES (%s, '2026-11-05')", (sid,))
    return sid


def _item(conn, sid, seq=1, code="028050", **extra):
    columns = {"session_id": sid, "seq": seq, "code": code, "name": "삼성E&A", "market": "KOSPI", **extra}
    names = ", ".join(columns)
    marks = ", ".join(["%s"] * len(columns))
    conn.execute(f"INSERT INTO session_items ({names}) VALUES ({marks})", tuple(columns.values()))


@needs_db
def test_init_creates_the_schema_on_an_empty_database(conn):
    assert db.init_schema(conn) == [1, 2, 3, 4]
    assert {"sessions", "session_items", "item_agent_ratings", "schema_migrations"} <= _tables(conn)


@needs_db
def test_init_is_safe_to_repeat(conn):
    db.init_schema(conn)
    assert db.init_schema(conn) == []
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions == [1, 2, 3, 4]


@needs_db
def test_tables_hold_everything_the_api_contract_needs(conn):
    db.init_schema(conn)
    assert _columns(conn, "sessions") == {
        "id", "as_of", "created_at", "ranked_at", "source",
        "build_state", "build_stage", "build_done", "build_total", "build_error", "criteria",
    }
    assert {
        "session_id", "seq", "code", "name", "market", "close", "avg_trading_value", "rsi",
        "flow_status", "foreign_net_5d", "institution_net_5d", "sector_code", "sector_name",
        "description", "is_caution", "is_foreign", "state", "analyzed_at", "error_message",
        "report_id", "decision", "score", "agents_rated", "agents_total", "rank", "archived", "elapsed_seconds",
    } == _columns(conn, "session_items")
    assert _columns(conn, "item_agent_ratings") == {"session_id", "code", "agent", "rating"}


@needs_db
def test_no_report_body_column_exists(conn):
    db.init_schema(conn)
    for table in ("sessions", "session_items", "item_agent_ratings"):
        assert not {c for c in _columns(conn, table) if "body" in c or "content" in c or "markdown" in c}


@needs_db
def test_item_defaults(conn):
    db.init_schema(conn)
    sid = _session(conn)
    _item(conn, sid)
    row = conn.execute(
        "SELECT state, flow_status, decision, score, rank, report_id, error_message, is_caution, is_foreign "
        "FROM session_items"
    ).fetchone()
    assert row == ("pending", "unconfirmed", "", None, None, "", "", False, False)


@needs_db
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("state", "waiting"),
        ("flow_status", "good"),
        ("decision", "StrongBuy"),
        ("seq", 0),
        ("rank", 0),
        ("agents_rated", -1),
    ],
)
def test_invalid_values_are_rejected_by_the_database(conn, column, value):
    db.init_schema(conn)
    sid = _session(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _item(conn, sid, **{column: value})


@needs_db
def test_same_code_twice_in_one_session_is_rejected_but_allowed_in_another(conn):
    db.init_schema(conn)
    first, second = _session(conn), _session(conn)
    _item(conn, first, seq=1, code="028050")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _item(conn, first, seq=2, code="028050")
    _item(conn, second, seq=1, code="028050")  # 다른 세션에서는 같은 종목이 들어갈 수 있다


@needs_db
def test_item_needs_an_existing_session(conn):
    db.init_schema(conn)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _item(conn, uuid.uuid4())


@needs_db
def test_agent_rating_needs_its_item_and_a_valid_rating(conn):
    db.init_schema(conn)
    sid = _session(conn)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO item_agent_ratings (session_id, code, agent, rating) VALUES (%s, '028050', 'bull', 'Hold')",
            (sid,),
        )
    _item(conn, sid)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO item_agent_ratings (session_id, code, agent, rating) VALUES (%s, '028050', 'bull', 'Maybe')",
            (sid,),
        )
    conn.execute(
        "INSERT INTO item_agent_ratings (session_id, code, agent, rating) VALUES (%s, '028050', 'bull', 'Hold')",
        (sid,),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):  # 한 종목의 한 에이전트는 등급이 하나
        conn.execute(
            "INSERT INTO item_agent_ratings (session_id, code, agent, rating) VALUES (%s, '028050', 'bull', 'Sell')",
            (sid,),
        )


@needs_db
def test_deleting_a_session_removes_its_items_and_ratings_only(conn):
    db.init_schema(conn)
    keep, drop = _session(conn), _session(conn)
    for sid in (keep, drop):
        _item(conn, sid)
        conn.execute(
            "INSERT INTO item_agent_ratings (session_id, code, agent, rating) VALUES (%s, '028050', 'bull', 'Hold')",
            (sid,),
        )
    conn.execute("DELETE FROM sessions WHERE id = %s", (drop,))
    assert conn.execute("SELECT count(*) FROM session_items").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM item_agent_ratings").fetchone()[0] == 1
    assert conn.execute("SELECT session_id FROM session_items").fetchone()[0] == keep


@needs_db
def test_failed_migration_leaves_no_partial_schema(conn, monkeypatch):
    broken = [(1, "깨진 변경", "CREATE TABLE half_done (id int); SELECT * FROM table_that_does_not_exist;")]
    monkeypatch.setattr(db, "MIGRATIONS", broken)
    with pytest.raises(psycopg.errors.UndefinedTable):
        db.init_schema(conn)
    assert "half_done" not in _tables(conn)
    assert "schema_migrations" not in _tables(conn) or conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0


@needs_db
def test_cli_init_creates_the_schema(monkeypatch, capsys, conn):
    monkeypatch.setenv(db.ENV_URL, TEST_URL)
    assert db.main(["init"]) == 0
    assert "스키마를 만들었습니다" in capsys.readouterr().out
    assert db.main(["init"]) == 0
    assert "이미 최신" in capsys.readouterr().out


@needs_db
def test_sessions_are_ready_by_default_so_existing_ones_stay_valid(conn):
    db.init_schema(conn)
    sid = _session(conn)
    row = conn.execute("SELECT build_state, build_stage, build_done, build_total, build_error FROM sessions WHERE id = %s", (sid,)).fetchone()
    assert row == ("ready", "", 0, 0, "")


@needs_db
def test_only_one_session_per_date_can_be_building_at_a_time(conn):
    db.init_schema(conn)
    conn.execute("INSERT INTO sessions (id, as_of, build_state) VALUES (gen_random_uuid(), '2026-11-05', 'building')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO sessions (id, as_of, build_state) VALUES (gen_random_uuid(), '2026-11-05', 'building')")
    conn.execute("INSERT INTO sessions (id, as_of, build_state) VALUES (gen_random_uuid(), '2026-11-04', 'building')")  # 다른 기준일
    _session(conn)  # 같은 기준일의 완성된 세션은 여러 개여도 된다
    _session(conn)


@needs_db
def test_an_unknown_build_state_is_rejected(conn):
    db.init_schema(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO sessions (id, as_of, build_state) VALUES (gen_random_uuid(), '2026-11-05', 'interrupted')")  # 중단됨은 저장하지 않고 계산한다


@needs_db
def test_criteria_is_empty_for_a_session_that_has_none(conn):
    db.init_schema(conn)
    sid = _session(conn)
    assert conn.execute("SELECT criteria FROM sessions WHERE id = %s", (sid,)).fetchone()[0] is None  # 기록 없음


@needs_db
def test_criteria_keeps_a_json_document(conn):
    db.init_schema(conn)
    sid = _session(conn)
    conn.execute("UPDATE sessions SET criteria = '{\"flow\": {\"mode\": \"mark\"}}' WHERE id = %s", (sid,))
    assert conn.execute("SELECT criteria FROM sessions WHERE id = %s", (sid,)).fetchone()[0] == {"flow": {"mode": "mark"}}
