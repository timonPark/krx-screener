"""실제 브라우저로 화면을 조작해서 본다 (#58).

헤드리스 Chrome 을 CDP 로 구동한다 (`tests/browser/`). 서버는 실제 백엔드 + 테스트 PostgreSQL + 실제 `frontend/` 이고
시세·수급·분석 서버만 가짜다 (`tests/browser_harness.py`). 서버 안의 가짜는 `POST /__ctl` 로 실행 중에 조작한다.

Node 22.18 이상(`.ts` 를 타입 지우고 바로 실행), Chrome(macOS 기본 경로 또는 환경변수 `KRX_SCREENER_CHROME`), 테스트 DB 가 모두 있어야 돈다. 없으면 건너뛴다.
화면의 규칙(오류 알림이 사라지는 때, 정리 막힘, 진행 표시)을 `app.js` 가 실제로 연결했는지는 여기서만 지켜진다.
"""

import json
import os
import shutil
import socket
import subprocess
import uuid
from pathlib import Path

import pytest
from browser_harness import BrowserServer

BROWSER = Path(__file__).resolve().parent / "browser"
MAC_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def _chrome() -> str | None:
    env = os.environ.get("KRX_SCREENER_CHROME")
    if env:
        return env if Path(env).exists() else None
    return str(MAC_CHROME) if MAC_CHROME.exists() else None


def _node_version() -> tuple[int, int]:
    """(주, 부) 버전. Node 가 없거나 읽지 못하면 (0, 0)."""
    node = shutil.which("node")
    if not node:
        return (0, 0)
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, check=True).stdout
        major, minor, *_ = out.strip().lstrip("v").split(".")
        return (int(major), int(minor))
    except (OSError, subprocess.SubprocessError, ValueError):
        return (0, 0)


# `.ts` 를 바로 실행하는 타입 제거(type stripping)는 22.18 부터 기본으로 켜져 있다
pytestmark = pytest.mark.skipif(
    _node_version() < (22, 18) or _chrome() is None, reason="Node 22.18 이상과 Chrome(KRX_SCREENER_CHROME)이 필요함"
)


@pytest.fixture
def server(pg_conn, tmp_path, frontend_dist):
    srv = BrowserServer(os.environ["KRX_SCREENER_TEST_DATABASE_URL"], tmp_path / "state_logs")
    srv.start()
    yield srv
    srv.stop()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def scenario(server, tmp_path):
    """시나리오 하나를 새 Chrome 으로 돌리고 화면에서 본 사실(JSON)을 돌려준다. 콘솔 오류가 있으면 실패한다."""

    def run(name: str) -> dict:
        result = subprocess.run(
            [
                shutil.which("node"), str(BROWSER / "scenarios.ts"), name, server.url, str(_free_port()),
                str(tmp_path / f"chrome-{name}"),
            ],
            capture_output=True, text=True, timeout=180, env={**os.environ, "KRX_SCREENER_CHROME": _chrome()},
        )
        assert result.returncode == 0, result.stderr
        facts = json.loads(result.stdout)
        assert facts["consoleErrors"] == [], facts["consoleErrors"]
        return facts

    return run


def test_a_new_list_shows_its_progress_then_the_table(scenario):
    facts = scenario("newList")
    assert facts["firstSession"] == "세션 없음 — 새 목록을 뽑으세요"
    building = facts["building"]
    assert (building["title"], building["detail"], building["bar"]) == ("목록을 만드는 중입니다", "시세 조회 3 / 8", "37.5%")
    assert building["total"] == "—" and building["rows"] == 0  # 종목은 끝난 뒤에 채워진다
    assert building["session"].endswith("· 만드는 중 (최근)") and "목록 만드는 중" in building["poll"]
    assert building["startDisabled"] and building["organizeDisabled"]
    assert "만드는 중" in building["startHint"]
    # 같은 기준일을 또 뽑으면 새 세션이 생기지 않고 만드는 중인 세션에 그대로 머문다
    assert facts["duplicate"] == {"error": "이미 2026-11-05 목록을 뽑는 중입니다.", "session": building["session"], "sessions": 1}
    ready = facts["ready"]
    assert (ready["total"], ready["rows"], ready["buildCardShown"], ready["startDisabled"]) == ("8", 8, False, False)
    assert ready["filter"] == "20일 평균 거래대금 10억↑ · 종가 > 20일선 · 종가 > 60일선"  # 세션이 뽑을 때 쓴 조건
    assert ready["extra"].startswith("제외: 우선주·스팩·관리종목·거래정지 의심·신규상장·리츠 · 수급: 표시만")
    # 동작 오류(409)는 이어서 부른 조회가 성공해도 남는다 (다음 동작이 성공하거나 다른 세션을 고를 때까지)
    assert facts["errorAfter"] == "이미 2026-11-05 목록을 뽑는 중입니다."


def test_processing_updates_the_screen_and_organizing_is_gated_until_everything_is_done(scenario):
    facts = scenario("processAndOrganize")
    assert facts["running"]["startDisabled"] and facts["running"]["offsetDisabled"]  # 처리 중에는 새로고침 없이 막힌다
    assert facts["running"]["poll"].startswith("처리 중")
    first = facts["afterFirst"]
    assert (first["progress"], first["done"], first["failed"], first["pending"]) == ("4 / 8", "3", "1", "4")
    assert first["rows"][:4] == ["1:완료", "2:완료", "3:완료", "4:실패"] and first["rows"][4] == "5:대기"
    # 실패한 종목은 구간 처리에서 빠지고, 버튼으로만 다시 처리한다 (#77)
    assert facts["retry"] == {
        "label": "실패한 종목 다시 처리 (1개)", "disabled": False,
        "hint": "실패한 1개를 다시 처리합니다. 서버에 이미 끝난 분석이 있으면 쿼터 없이 가져오고, 없는 종목만 새로 분석합니다 (새로 분석하면 약 5분, 분석 쿼터 최대 1번).",
    }
    assert facts["failedOnlyRange"] == {
        "startDisabled": True, "hint": "이 구간의 종목은 모두 끝났거나 실패했습니다. 다른 구간을 고르세요 (실패한 종목은 아래 버튼으로 다시 처리합니다).",
        "eta": "0분",
    }
    assert facts["doneAndFailedRange"]["startDisabled"] is True
    assert facts["gated"]["disabled"] and "4개" in facts["gated"]["hint"]  # 남은 종목이 있으면 정리를 못 누른다
    assert facts["gatedApi"] == 409  # 화면 밖에서 눌러도 서버가 막는다
    assert (facts["allProcessed"]["done"], facts["allProcessed"]["failed"]) == ("7", "1")
    assert facts["allProcessed"]["organizeDisabled"] is False
    # 점수는 저장값(−2 ~ +2)에 100 을 곱한 정수로 보인다. 부호는 붙이고 0 은 부호가 없다 (#76)
    assert facts["scores"] == ["1:+60", "2:−80", "3:+20", "4:—", "5:+40", "6:0", "7:+60", "8:−40"]
    # 정리하면 점수 높은 순으로 순위가 붙고 실패한 종목은 순위에서 빠진다
    organized = facts["organized"]
    assert organized["sort"] == "rank" and organized["stale"] is False
    ranks = dict(r.split(":") for r in organized["rows"])
    assert ranks["4"] == "—" and sorted(int(v) for k, v in ranks.items() if k != "4") == list(range(1, 8))
    # 정리하면 업종별 해석과 종합 평가가 화면에 나오고, 보고서를 내려받을 수 있다 (실패한 4번 종목은 요약에서 빠진다)
    summary = facts["summary"]
    assert "점수가 있는 후보 7개" in summary["assessment"]
    assert {"점수가 있는 후보", "평균 평가점수", "등급", "수급 상태"} <= set(summary["stats"])
    assert len(summary["themes"]) == 7 and "반도체와반도체장비" not in summary["themes"]
    assert summary["interpretations"] == 7  # 업종마다 해석 문장이 있다
    assert summary["linkShown"] is True and summary["reportHref"].endswith("/report")
    status, content_type, disposition, first_line = summary["report"]
    assert status == 200 and content_type.startswith("text/markdown")
    assert disposition == 'attachment; filename="krx-screener-summary-2026-11-05.md"'
    assert first_line == "# KRX Screener 정리 요약 — 2026-11-05"
    # 차트: 서버가 그린 SVG 가 <img> 로 실제 그려진다 (전종목 수치가 있어 다섯 개)
    assert [c["alt"] for c in facts["charts"]] == ["등급 분포", "수급 상태 분포", "전종목 시장 폭", "업종별 평균 평가점수", "업종별 등급 구성"]
    assert all(c["loaded"] for c in facts["charts"])  # 받아서 그려진 그림이다 (깨진 이미지가 아니다)
    assert all("/charts/" in c["src"] and ".svg?v=" in c["src"] for c in facts["charts"])  # 정리 시각이 주소에 붙는다
    assert facts["chartsKept"] is True  # 같은 정리면 조회가 돌아도 그림을 다시 만들지 않는다
    status, content_type, start, svgs = facts["htmlReport"]  # HTML 보고서에도 같은 차트가 들어 있다
    assert status == 200 and content_type.startswith("text/html") and start == "<!doctype html>" and svgs == 5
    # 정리 뒤에 종목을 다시 분석하면 순위가 낡았다고 알리고, 다시 정리하면 사라진다
    assert facts["stale"]["shown"] is True
    assert facts["restored"]["stale"] is False
    assert "점수가 있는 후보 8개" in facts["restored"]["assessment"]  # 다시 정리하면 요약도 새로 만들어진다
    assert "낡음" not in facts["restored"]["summaryMeta"]
    # 확인창이 쿼터를 알리고, 취소하면 아무것도 보내지 않는다
    confirm = (
        "실패한 1개를 다시 처리합니다.\n서버에 이미 끝난 분석이 있으면 쿼터 없이 가져오고, 없는 종목만 새로 분석합니다.\n"
        "새로 분석하면 약 5분이 걸리고 분석 쿼터를 최대 1번 씁니다.\n계속할까요?"
    )
    assert facts["cancelled"] == {"dialogs": [confirm], "failed": "1", "startedNothing": True}
    assert facts["confirmed"] == {"dialogs": [confirm]}
    assert sorted(int(r.split(":")[1]) for r in facts["restored"]["rows"]) == list(range(1, 9))


def test_error_notices_stay_or_go_by_where_they_came_from(scenario):
    facts = scenario("errors")
    conflict = "이미 처리 중입니다."
    assert facts["conflict"] == {"first": conflict, "later": conflict}  # 동작 오류는 갱신이 돌아도 남는다
    down = "TradingAgents-KR 서버에 연결하지 못했습니다 (http://127.0.0.1:8000): ConnectionError"
    assert facts["analysisDown"] == {"first": down, "later": down}
    assert facts["retried"] == {"error": None}  # 풀린 뒤 다음 동작이 성공하면 사라진다
    assert facts["badDate"]["first"] == facts["badDate"]["later"] and facts["badDate"]["first"]
    assert "PostgreSQL" in facts["dbDown"]["first"] and facts["dbDown"]["later"] == facts["dbDown"]["first"]
    assert facts["dbBack"] == {"error": None}  # 조회 오류는 다음 조회가 성공하면 사라진다


def test_a_list_that_failed_shows_why_and_blocks_processing(scenario, pg_conn):
    pg_conn.execute(
        "INSERT INTO sessions (id, as_of, build_state, build_error) VALUES (%s, '2026-11-05', 'failed', %s)",
        (uuid.uuid4(), "시세를 받는 중 오류가 났습니다 (RuntimeError)."),
    )
    card = scenario("failedList")["card"]
    assert (card["shown"], card["title"], card["detail"]) == (True, "목록을 만들지 못했습니다", "시세를 받는 중 오류가 났습니다 (RuntimeError).")
    assert "다시 뽑" in card["hint"] and card["session"].endswith("· 실패 (최근)") and card["total"] == "—"
    assert card["startDisabled"] and card["organizeDisabled"] and "만들지 못했습니다" in card["startHint"]


def test_the_mock_mode_runs_without_a_backend_and_builds_a_new_list(scenario):
    facts = scenario("mock")
    assert facts["tag"] is True and facts["first"]["rows"] == 14
    assert facts["first"]["filter"].startswith("20일 평균 거래대금 10억↑")
    assert facts["building"] == "목록을 만드는 중입니다"
    assert facts["ready"]["total"] == "14" and facts["ready"]["session"].startswith("2026-11-04")


def test_an_idle_screen_notices_a_process_started_elsewhere_without_a_reload(scenario):
    facts = scenario("startedElsewhere")
    assert facts["idleBefore"]["start"] is False and "마지막 갱신" in facts["idleBefore"]["poll"]
    assert "초마다 확인" in facts["idleBefore"]["poll"]  # 쉬는 중에도 가끔 확인한다고 알린다
    assert facts["started"] == 202  # 화면 밖(API)에서 시작했다
    noticed = facts["noticed"]
    assert noticed["poll"].startswith("처리 중") and noticed["startDisabled"] and noticed["offsetDisabled"]  # 새로고침 없이 처리 중으로
    assert facts["afterFirst"] == {"done": "2", "start": "처리 시작"}
    assert facts["afterSecond"]["done"] == "3"  # 끝난 뒤에도 쉬는 중 확인으로 돌아가서 다음 처리도 알아챘다


def test_a_hidden_tab_stops_the_idle_check_and_a_shown_tab_checks_at_once(scenario):
    facts = scenario("hiddenTab")
    assert facts["visibleGrowth"] >= 2  # 보이는 동안은 쉬는 중에도 조회한다
    assert facts["hiddenGrowth"] == 0  # 숨겨진 탭은 조회하지 않는다
    assert facts["shownGrowth"] >= 1  # 다시 보이면 바로 한 번 조회한다
