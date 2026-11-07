"""서버 조립과 ``serve`` 명령 (#26): 실제 경계를 이어 붙이고 uvicorn 으로 띄운다.

테스트는 uvicorn 을 실제로 띄우지 않고 (포트를 쓰지 않도록) ``uvicorn.run`` 을 가로채서 넘기는 값을 본다.
"""

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from krx_screener import cli, db, flow, prices, sector, server, universe
from krx_screener.analysis import AnalysisClient
from krx_screener.config import ConfigError

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


# --- 의존성 조립 ---


def test_the_real_boundaries_are_wired(tmp_path, monkeypatch):
    made = []

    class RecordingClient(AnalysisClient):
        def __init__(self, base_url, *args, **kwargs):
            made.append(base_url)
            super().__init__(base_url, *args, **kwargs)

    monkeypatch.setattr(server, "AnalysisClient", RecordingClient)
    deps = server.build_dependencies(CONFIG, cache_dir=tmp_path / "cache", frontend_dir=FRONTEND, env={})
    ext = deps.externals
    assert ext.listing is universe.fetch_listing
    assert ext.prices is prices.fdr_fetcher
    assert ext.trend is flow.naver_trend
    assert ext.sector is sector.naver_sector
    assert isinstance(ext.analysis, AnalysisClient)
    assert made == [deps.config.analysis.base_url]  # config.yaml 의 분석 서버 주소
    assert deps.cache_dir == tmp_path / "cache"
    assert deps.frontend_dir == FRONTEND


def test_building_does_not_touch_the_database_or_the_network(tmp_path):
    deps = server.build_dependencies(CONFIG, cache_dir=tmp_path, frontend_dir=FRONTEND, env={})
    with pytest.raises(db.DatabaseError, match="KRX_SCREENER_DATABASE_URL"):  # 연결은 요청이 올 때 만든다
        deps.connect()


def test_the_database_address_comes_from_the_environment(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(server.db, "connect", lambda url=None: seen.append(url) or "연결")
    deps = server.build_dependencies(CONFIG, cache_dir=tmp_path, frontend_dir=FRONTEND, env={})
    assert deps.connect() == "연결" and seen == [None]  # 주소는 db.connect 가 환경변수에서 읽는다 (코드에 넣지 않는다)


def test_state_logs_are_read_from_the_analysis_servers_log_folder(tmp_path):
    home = Path("/홈")
    default = server.default_state_log_root({}, home=home)
    assert default == home / ".tradingagents" / "logs"  # TradingAgents-KR 서버의 기본 폴더
    assert server.default_state_log_root({"TRADINGAGENTS_RESULTS_DIR": str(tmp_path)}, home=home) == tmp_path  # 서버와 같은 환경변수
    deps = server.build_dependencies(CONFIG, cache_dir=tmp_path, frontend_dir=FRONTEND, env={"TRADINGAGENTS_RESULTS_DIR": str(tmp_path)})
    assert deps.externals.state_log_root == tmp_path


def test_a_bad_config_is_a_korean_error(tmp_path):
    with pytest.raises(ConfigError, match="설정 파일을 읽지 못했습니다"):
        server.build_dependencies(tmp_path / "없음.yaml", cache_dir=tmp_path, frontend_dir=FRONTEND, env={})


def test_the_default_screen_folder_is_the_one_in_the_repository():
    assert server.default_frontend_dir() == FRONTEND
    assert (server.default_frontend_dir() / "index.html").is_file()


# --- serve 명령 ---


@pytest.fixture
def run_calls(monkeypatch, frontend_dist):  # serve 는 빌드된 화면(frontend/dist)이 있어야 뜬다 (#65)
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    return calls


def serve(*args):
    return CliRunner().invoke(cli.app, ["serve", "--config", str(CONFIG), *args])


def test_serve_starts_the_app_on_the_local_address_by_default(run_calls):
    result = serve()
    assert result.exit_code == 0, result.output
    app, kwargs = run_calls[0]
    assert (kwargs["host"], kwargs["port"]) == ("127.0.0.1", 8100)  # 로그인이 없으니 이 컴퓨터에서만 연다
    assert app.title == "krx-screener"
    assert "http://127.0.0.1:8100" in result.output  # 열 주소를 알려준다


def test_serve_can_use_another_port(run_calls):
    assert serve("--port", "9123").exit_code == 0
    assert run_calls[0][1]["port"] == 9123


def test_serve_serves_the_screen_and_the_api_from_one_app(run_calls):
    from fastapi.testclient import TestClient

    serve()
    client = TestClient(run_calls[0][0])
    assert "KRX Screener" in client.get("/").text
    assert client.get("/api/health").status_code in (200, 503)  # DB 가 꺼져 있어도 앱은 떠 있고 한국어로 알려준다


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.0.10", "::"])  # noqa: S104
def test_serve_refuses_a_network_address_unless_asked_because_there_is_no_login(run_calls, host):
    result = serve("--host", host)
    assert result.exit_code == 2
    assert "로그인" in result.output and "--allow-remote" in result.output
    assert run_calls == []


def test_serve_allows_a_network_address_when_asked_explicitly(run_calls):
    result = serve("--host", "0.0.0.0", "--allow-remote")  # noqa: S104
    assert result.exit_code == 0, result.output
    assert run_calls[0][1]["host"] == "0.0.0.0"  # noqa: S104
    assert "로그인" in result.output  # 열어 두는 위험을 한 번 더 알린다


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_serve_accepts_the_local_addresses(run_calls, host):
    assert serve("--host", host).exit_code == 0


def test_serve_without_the_screen_folder_is_a_korean_error(run_calls, tmp_path):
    result = serve("--frontend", str(tmp_path / "없는-폴더"))
    assert result.exit_code == 1
    assert "화면 폴더를 찾지 못했습니다" in result.output
    assert run_calls == []


def test_serve_without_a_built_screen_tells_how_to_build_it(run_calls, tmp_path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")  # 화면 폴더는 있지만 dist/app.js 가 없다
    result = serve("--frontend", str(tmp_path))
    assert result.exit_code == 1
    assert "화면이 빌드되지 않았습니다" in result.output and "npm ci && npm run build" in result.output
    assert run_calls == []


def test_serve_with_a_missing_config_is_a_korean_error(run_calls, tmp_path):
    result = CliRunner().invoke(cli.app, ["serve", "--config", str(tmp_path / "없음.yaml")])
    assert result.exit_code == 1
    assert "설정 파일을 읽지 못했습니다" in result.output
    assert run_calls == []


def test_serve_starts_even_when_the_database_is_not_ready(run_calls, monkeypatch):
    monkeypatch.delenv(db.ENV_URL, raising=False)
    assert serve().exit_code == 0  # DB 가 없어도 뜨고, 화면이 한국어 오류로 알려준다 (/api/health)
    assert os.environ.get(db.ENV_URL) is None
