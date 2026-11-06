"""화면(frontend/) 정적 서빙 (#26): 백엔드가 같은 주소에서 화면과 API 를 함께 서빙한다.

진짜 ``frontend/`` 폴더를 서빙해서 확인한다. 화면은 ``apiBase: ''`` 라서 같은 주소의 ``/api/...`` 를 부른다 (CORS 불필요).
"""

import os
import re
from pathlib import Path

import pytest
from fakes import make_externals, repo_config
from fastapi.testclient import TestClient

from krx_screener import db
from krx_screener.api import Dependencies, create_app

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
TEST_URL_ENV = "KRX_SCREENER_TEST_DATABASE_URL"


def _no_db():
    raise db.DatabaseError("정적 파일 테스트는 DB 를 쓰지 않는다.")


@pytest.fixture
def static_client(tmp_path):
    """DB 없이 정적 파일만 본다 (화면 파일을 읽는 데는 DB 가 필요 없다)."""
    deps = Dependencies(connect=_no_db, externals=make_externals(tmp_path), config=repo_config(), frontend_dir=FRONTEND)
    return TestClient(create_app(deps))


@pytest.fixture
def web(pg_conn, tmp_path):
    """진짜 테스트 DB + 화면 서빙."""
    url = os.environ[TEST_URL_ENV]
    deps = Dependencies(
        connect=lambda: db.connect(url),
        externals=make_externals(tmp_path),
        config=repo_config(),
        frontend_dir=FRONTEND,
    )
    return TestClient(create_app(deps))


# --- 화면 파일 ---


def test_the_screen_opens_at_the_root_address(static_client):
    response = static_client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "KRX Screener" in response.text


def test_the_mock_mode_address_serves_the_same_page(static_client):
    # ?mock=1 은 브라우저 안에서 화면이 읽는다. 서버는 같은 페이지를 준다
    response = static_client.get("/?mock=1")
    assert (response.status_code, response.text) == (200, static_client.get("/").text)


def test_every_file_the_page_loads_is_served_with_a_type_the_browser_accepts(static_client):
    """index.html 이 부르는 파일과 JS 모듈이 import 하는 파일이 모두 열린다 (JS 는 모듈로 실행될 수 있는 형식으로)."""
    seen: set[str] = set()
    pending = ["index.html"]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        response = static_client.get("/" + name)
        assert response.status_code == 200, f"{name} 을 열지 못한다"
        kind = response.headers["content-type"]
        if name.endswith(".js"):
            assert kind.startswith(("text/javascript", "application/javascript")), f"{name}: {kind}"  # 모듈은 JS 형식이어야 실행된다
            pending += re.findall(r"""from\s+['"]\./([\w.-]+)['"]""", response.text)
        elif name.endswith(".css"):
            assert kind.startswith("text/css"), f"{name}: {kind}"
        elif name.endswith(".html"):
            pending += re.findall(r"""(?:src|href)=["']\./([\w.-]+)["']""", response.text)
    # 화면을 이루는 파일이 모두 따라왔는지 (파일을 새로 만들었는데 서빙에서 빠지는 일이 없도록)
    assert {"index.html", "app.js", "api.js", "config.js", "mock.js", "styles.css"} <= seen


def test_the_screen_calls_the_api_on_the_same_address(static_client):
    config = static_client.get("/config.js").text
    assert re.search(r"apiBase:\s*''", config)  # 빈 값이라 같은 주소의 /api 를 부른다 → CORS 가 필요 없다


def test_the_mock_data_files_are_served_so_the_example_mode_keeps_working(static_client):
    assert static_client.get("/mock.js").status_code == 200
    assert "createMockApi" in static_client.get("/mock.js").text


# --- 화면과 API 가 한 주소 ---


def test_the_api_still_answers_next_to_the_screen(web):
    health = web.get("/api/health")
    assert (health.status_code, health.json()["status"]) == (200, "ok")
    assert web.get("/api/sessions").json() == []
    assert "KRX Screener" in web.get("/").text  # 같은 앱이 화면도 준다


def test_a_session_can_be_created_and_read_through_the_same_app_that_serves_the_screen(web):
    created = web.post("/api/sessions", json={"as_of": "2026-11-05"})
    assert created.status_code == 201
    session_id = created.json()["id"]
    assert web.get(f"/api/sessions/{session_id}").json()["session"]["id"] == session_id
    assert web.get("/api/sessions/latest").status_code == 200


def test_an_unknown_api_address_is_a_404_not_the_screen(static_client):
    response = static_client.get("/api/없는-주소")
    assert response.status_code == 404
    assert "KRX Screener" not in response.text  # 화면으로 대신 답하지 않는다


def test_an_unknown_file_is_a_404(static_client):
    assert static_client.get("/없는-파일.js").status_code == 404


def test_files_outside_the_screen_folder_are_not_served(static_client):
    for path in ("/../pyproject.toml", "/%2e%2e/pyproject.toml", "/..%2fpyproject.toml", "/../config.yaml", "/../.env"):
        response = static_client.get(path)
        assert response.status_code in (400, 404), path
        assert "krx-screener" not in response.text, path


def test_without_a_screen_folder_the_app_is_api_only(tmp_path):
    deps = Dependencies(connect=_no_db, externals=make_externals(tmp_path), config=repo_config())
    client = TestClient(create_app(deps))
    assert client.get("/").status_code == 404
