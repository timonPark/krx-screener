"""화면(frontend/)과 백엔드 응답의 계약 (#27): 보고서 번호는 ``report_id`` 이고 실행 번호(``run_id``)는 화면에 나오지 않는다.

화면은 DOM 이 필요한 코드라 그대로 테스트하기 어렵다. 그래서
- 링크 주소를 만드는 로직은 DOM 이 없는 ``report.js`` 로 나눠서 Node 로 직접 확인하고,
- 예시 데이터(``mock.js``)가 만드는 응답의 키가 **백엔드 응답 모델과 같은지** 대조한다 (예시 모드가 실제 모양과 어긋나지 않게).
Node(22 이상)가 없으면 이 테스트들은 건너뛴다.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from krx_screener.routes_sessions import CountsOut, ItemOut, SessionDetailOut, SessionOut

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
REPORT_ID = re.compile(r"^\d{6}_\d{8}_\d{6}$")  # 028050_20261106_172435


def _node_major() -> int:
    node = shutil.which("node")
    if not node:
        return 0
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, check=True).stdout
        return int(out.strip().lstrip("v").split(".")[0])
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


needs_node = pytest.mark.skipif(_node_major() < 22, reason="Node 22 이상이 없음")


def run_js(body: str) -> Any:
    """화면 모듈을 가져와 ``body`` 를 실행하고 ``print(JSON)`` 한 결과를 돌려준다."""
    script = f"""
globalThis.location = {{ search: '' }};  // config.js 가 주소 검색어를 읽는다
const dir = {json.dumps(FRONTEND.as_uri())};
{body}
"""
    result = subprocess.run(
        [shutil.which("node"), "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# --- 링크 주소 ---


@needs_node
@pytest.mark.parametrize(
    ("template", "report_id", "expected"),
    [
        (None, "028050_20261106_172435", None),  # 주소를 정하지 않으면 링크가 없다 (글자로만 보인다)
        ("", "028050_20261106_172435", None),
        ("http://127.0.0.1:4321/report?id={report_id}", "028050_20261106_172435", "http://127.0.0.1:4321/report?id=028050_20261106_172435"),
        ("http://x/api/reports/{report_id}/download?format=html", "028050_20261106_172435", "http://x/api/reports/028050_20261106_172435/download?format=html"),
        ("http://x/r/{report_id}", "a/b c", "http://x/r/a%2Fb%20c"),  # 번호는 주소에 안전하게 넣는다
        ("http://x/{report_id}/{report_id}", "028050_20261106_172435", "http://x/028050_20261106_172435/028050_20261106_172435"),
        ("http://x/report?id={report_id}", "", None),  # 번호가 없으면 링크가 없다
        ("http://x/report?id={report_id}", None, None),
    ],
)
def test_the_report_link_is_built_from_the_report_id(template, report_id, expected):
    got = run_js(
        f"""
const {{ reportHref }} = await import(dir + '/report.js');
console.log(JSON.stringify(reportHref({json.dumps(template)}, {json.dumps(report_id)})));
"""
    )
    assert got == expected


@needs_node
def test_the_old_run_id_placeholder_is_not_filled_in():
    got = run_js(
        """
const { reportHref } = await import(dir + '/report.js');
console.log(JSON.stringify(reportHref('http://x/r/{run_id}', '028050_20261106_172435')));
"""
    )
    assert "{run_id}" in got  # 예전 자리표시자를 쓰는 낡은 설정은 눈에 띄게 깨진다 (엉뚱한 번호로 조용히 연결되지 않는다)


# --- 예시 데이터가 백엔드 응답과 같은 모양 ---

MOCK = """
class ApiError extends Error { constructor(s, b) { super(b.detail); this.status = s; this.body = b; } }
const { createMockApi } = await import(dir + '/mock.js');
const { config } = await import(dir + '/config.js');
config.mockMsPerItem = 1;
const api = createMockApi(ApiError);
"""


@needs_node
def test_the_mock_answers_have_exactly_the_keys_of_the_backend_models():
    got = run_js(MOCK + "console.log(JSON.stringify({ detail: await api.getSession('latest'), list: await api.listSessions() }));")
    detail = got["detail"]
    assert set(detail) == set(SessionDetailOut.model_fields)
    assert set(detail["session"]) == set(SessionOut.model_fields)
    assert set(detail["counts"]) == set(CountsOut.model_fields)
    assert set(got["list"][0]) == set(SessionOut.model_fields)
    for item in detail["items"]:
        assert set(item) == set(ItemOut.model_fields), set(item) ^ set(ItemOut.model_fields)


@needs_node
def test_the_mock_items_carry_a_report_id_and_never_a_run_id():
    detail = run_js(MOCK + "console.log(JSON.stringify(await api.getSession('latest')));")
    done = [i for i in detail["items"] if i["state"] == "done"]
    assert done, "예시 데이터에 끝난 종목이 있어야 보고서 칸을 볼 수 있다"
    for item in done:
        assert REPORT_ID.match(item["report_id"]), item["report_id"]
        assert item["report_id"].startswith(item["code"] + "_")  # 종목코드로 시작한다
    assert all(i["report_id"] == "" for i in detail["items"] if i["state"] != "done")  # 끝나지 않았으면 번호가 없다
    assert not any("run_id" in i for i in detail["items"])


@needs_node
def test_a_stock_the_mock_finishes_gets_a_report_id_too():
    detail = run_js(
        MOCK
        + """
await api.process('latest', 3, 2);   // 실패한 종목(순번 4)과 대기 종목(순번 5)
for (let i = 0; i < 200; i++) {
  const d = await api.getSession('latest');
  if (!d.processing) break;
}
console.log(JSON.stringify(await api.getSession('latest')));
"""
    )
    items = {i["seq"]: i for i in detail["items"]}
    for seq in (4, 5):
        assert items[seq]["state"] == "done"
        assert REPORT_ID.match(items[seq]["report_id"]), items[seq]["report_id"]
        assert items[seq]["report_id"].startswith(items[seq]["code"] + "_")


# --- 화면 코드에 run_id 가 남아 있지 않다 ---


@pytest.mark.parametrize("name", ["app.js", "mock.js", "config.js", "api.js", "report.js", "index.html"])
def test_the_screen_code_no_longer_uses_the_run_id(name):
    assert "run_id" not in (FRONTEND / name).read_text(encoding="utf-8")


def test_the_report_column_reads_the_report_id():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "it.report_id" in app


# --- 오류 알림 (#28): 동작이 실패해서 띄운 오류는 이어지는 조회가 성공해도 사라지지 않는다 ---

NOTICE = """
const { createNotice } = await import(dir + '/notice.js');
const el = { hidden: true, textContent: '' };
const notice = createNotice(el);
const state = () => ({ hidden: el.hidden, text: el.textContent });
"""


@needs_node
def test_an_action_error_survives_the_refresh_that_follows_it():
    """처리·정리·새 목록이 실패하면 오류를 띄우고 곧바로 조회를 다시 부른다. 그 조회가 성공해도 오류가 사라지면 사용자가 읽을 수 없다
    (실제 브라우저에서 약 20ms 만 보였다)."""
    got = run_js(
        NOTICE
        + """
notice.show('이미 처리 중입니다.', 'action');
notice.clearIfFromRefresh();           // 이어서 부른 조회가 성공했다
console.log(JSON.stringify(state()));
"""
    )
    assert got == {"hidden": False, "text": "이미 처리 중입니다."}


@needs_node
def test_a_refresh_error_goes_away_when_a_later_refresh_succeeds():
    got = run_js(
        NOTICE
        + """
notice.show('PostgreSQL 에 연결하지 못했습니다.', 'refresh');
notice.clearIfFromRefresh();           // 서버가 되살아나서 조회가 성공했다
console.log(JSON.stringify(state()));
"""
    )
    assert got == {"hidden": True, "text": ""}


@needs_node
def test_the_next_action_or_a_session_change_clears_an_action_error():
    got = run_js(
        NOTICE
        + """
notice.show('남은 3개가 끝나야 정리할 수 있습니다.', 'action');
notice.clear();                        // 다음 동작이 성공했거나 다른 세션을 골랐다
console.log(JSON.stringify(state()));
"""
    )
    assert got == {"hidden": True, "text": ""}


@needs_node
def test_a_newer_error_replaces_the_older_one_and_takes_over_its_source():
    got = run_js(
        NOTICE
        + """
notice.show('이미 처리 중입니다.', 'action');
notice.show('서버에 연결하지 못했습니다.', 'refresh');   // 이어서 부른 조회도 실패했다
const first = state();
notice.clearIfFromRefresh();                              // 이제 조회가 성공하면 지워진다
console.log(JSON.stringify([first, state()]));
"""
    )
    assert got == [
        {"hidden": False, "text": "서버에 연결하지 못했습니다."},
        {"hidden": True, "text": ""},
    ]


@needs_node
def test_clearing_when_nothing_is_shown_is_harmless():
    got = run_js(NOTICE + "notice.clear(); notice.clearIfFromRefresh(); console.log(JSON.stringify(state()));")
    assert got == {"hidden": True, "text": ""}


def test_the_screen_keeps_action_errors_through_the_refresh_that_follows():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "createNotice" in app and "clearIfFromRefresh" in app
    assert "showError(err, 'action')" in app  # 동작(withBusy)의 오류는 동작 출처로 띄운다


# --- 예시 데이터의 값과 문구도 백엔드와 같다 (#28 에서 실제 화면과 견주며 찾은 차이) ---


@needs_node
def test_the_mock_organize_refusal_reads_like_the_backends():
    got = run_js(
        MOCK
        + """
try { await api.organize('latest'); console.log(JSON.stringify(null)); }
catch (e) { console.log(JSON.stringify({ status: e.status, body: e.body })); }
"""
    )
    # 예시 데이터는 처음에 대기 종목이 10개다 (백엔드: `남은 N개가 끝나야 정리할 수 있습니다.`)
    assert got == {"status": 409, "body": {"detail": "남은 10개가 끝나야 정리할 수 있습니다.", "remaining": 10}}


@needs_node
def test_the_mock_organize_answer_has_the_same_keys_as_the_backends():
    got = run_js(
        MOCK
        + """
await api.process('latest', 4, 100);       // 실패한 4번은 두고 나머지를 모두 처리
for (let i = 0; i < 400; i++) { if (!(await api.getSession('latest')).processing) break; await new Promise((r) => setTimeout(r, 20)); }
console.log(JSON.stringify(await api.organize('latest')));
"""
    )
    assert set(got) == {"ranked", "unscored"} and got["unscored"] == 0  # 백엔드: {"ranked": N, "unscored": M}


@needs_node
def test_the_mock_items_not_yet_analyzed_look_like_the_backends():
    detail = run_js(MOCK + "console.log(JSON.stringify(await api.getSession('latest')));")
    pending = [i for i in detail["items"] if i["state"] == "pending"]
    assert pending
    for item in pending:  # 백엔드: 처리 전에는 점수가 없고 에이전트 수도 0/0 이다
        assert (item["score"], item["agents_rated"], item["agents_total"], item["rank"]) == (None, 0, 0, None)
    for item in (i for i in detail["items"] if i["state"] == "done"):
        assert item["agents_total"] == 10  # 끝난 종목은 에이전트 10개 기준
