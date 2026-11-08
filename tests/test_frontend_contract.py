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

from krx_screener.routes_sessions import (
    BuildOut,
    CountsOut,
    CriteriaOut,
    FiltersOut,
    FlowCriteriaOut,
    ItemOut,
    SessionDetailOut,
    SessionOut,
)

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
SRC = FRONTEND / "src"  # TypeScript 소스
DIST = FRONTEND / "dist"  # tsc 가 만든 결과 (Node 로 불러 확인하는 것은 이쪽)
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


_skip_without_node = pytest.mark.skipif(_node_major() < 22, reason="Node 22 이상이 없음")


def needs_node(test):
    """Node 22 이상이 있어야 돌고, 화면을 빌드해서 ``frontend/dist`` 를 최신으로 만든 뒤에 돈다."""
    return pytest.mark.usefixtures("frontend_dist")(_skip_without_node(test))


def run_js(body: str) -> Any:
    """화면 모듈을 가져와 ``body`` 를 실행하고 ``print(JSON)`` 한 결과를 돌려준다."""
    script = f"""
globalThis.location = {{ search: '' }};  // config.js 가 주소 검색어를 읽는다
const dir = {json.dumps(DIST.as_uri())};
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
    assert set(detail["session"]["build"]) == set(BuildOut.model_fields)  # 목록을 만드는 중의 상태 (#56)
    criteria = detail["session"]["criteria"]  # 목록을 뽑을 때 쓴 조건 (#57)
    assert set(criteria) == set(CriteriaOut.model_fields)
    assert set(criteria["filters"]) == set(FiltersOut.model_fields)
    assert set(criteria["flow"]) == set(FlowCriteriaOut.model_fields)
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
const wait = async () => { for (let i = 0; i < 400; i++) { if (!(await api.getSession('latest')).processing) break; await new Promise((r) => setTimeout(r, 20)); } };
await api.process('latest', 3, 2);   // 구간 처리는 실패한 종목(순번 4)을 보내지 않고 대기 종목(순번 5)만 보낸다
await wait();
await api.processFailed('latest');   // 실패한 종목(순번 4)은 이 호출로 다시 처리한다
await wait();
console.log(JSON.stringify(await api.getSession('latest')));
"""
    )
    items = {i["seq"]: i for i in detail["items"]}
    for seq in (4, 5):
        assert items[seq]["state"] == "done"
        assert REPORT_ID.match(items[seq]["report_id"]), items[seq]["report_id"]
        assert items[seq]["report_id"].startswith(items[seq]["code"] + "_")


# --- 화면 코드에 run_id 가 남아 있지 않다 ---


@pytest.mark.parametrize(
    "path",
    ["src/app.ts", "src/mock.ts", "src/config.ts", "src/api.ts", "src/report.ts", "src/types.ts", "index.html"],
)
def test_the_screen_code_no_longer_uses_the_run_id(path):
    assert "run_id" not in (FRONTEND / path).read_text(encoding="utf-8")


def test_the_report_column_reads_the_report_id():
    app = (SRC / "app.ts").read_text(encoding="utf-8")
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
    app = (SRC / "app.ts").read_text(encoding="utf-8")
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


# --- 새 목록을 뒤에서 만든다 (#56): 예시 모드도 백엔드처럼 바로 응답하고 진행을 보여 준다 ---


@needs_node
def test_a_new_mock_list_answers_at_once_then_goes_through_the_stages_and_becomes_ready():
    got = run_js(
        MOCK
        + """
config.mockBuildMs = 600;
const created = await api.createSession('2026-11-04');
const stages = new Set();
let last = null;
for (let i = 0; i < 100; i++) {
  last = await api.getSession(created.id);
  stages.add(last.session.build.stage + ':' + last.session.build.state);
  if (last.session.build.state === 'ready') break;
}
console.log(JSON.stringify({ created, stages: [...stages], last }));
process.exit(0);
"""
    )
    assert (got["created"]["total"], got["created"]["build"]["state"]) == (0, "building")  # 응답은 만들기를 시작한 세션
    assert "prices:building" in got["stages"] or any(x.endswith(":building") for x in got["stages"])  # 만드는 중의 단계를 거친다
    assert any(x.endswith(":building") for x in got["stages"])
    last = got["last"]
    assert last["session"]["build"]["state"] == "ready" and last["session"]["total"] == len(last["items"]) > 0
    assert last["session"]["build"]["done"] == last["session"]["build"]["total"] == last["session"]["total"]


@needs_node
def test_the_mock_progress_has_the_backends_shape_while_building():
    got = run_js(
        MOCK
        + """
config.mockBuildMs = 3000;
const created = await api.createSession('2026-11-04');
const detail = await api.getSession(created.id);
const list = await api.listSessions();
console.log(JSON.stringify({ detail, list }));
process.exit(0);
"""
    )
    build = got["detail"]["session"]["build"]
    assert set(build) == set(BuildOut.model_fields) and build["state"] == "building"
    assert build["stage"] in {"prices", "flow", "sector"} and 0 <= build["done"] <= max(build["total"], 0)
    assert got["detail"]["items"] == [] and got["detail"]["counts"] == {"pending": 0, "running": 0, "done": 0, "failed": 0}
    assert got["list"][0]["build"]["state"] == "building"  # 목록에도 보인다


@needs_node
def test_asking_again_for_a_date_being_built_is_a_409_with_the_running_session():
    got = run_js(
        MOCK
        + """
config.mockBuildMs = 3000;
const first = await api.createSession('2026-11-04');
let error = null;
try { await api.createSession('2026-11-04'); } catch (e) { error = { status: e.status, body: e.body }; }
const other = await api.createSession('2026-11-03');   // 다른 기준일은 같이 만들 수 있다
console.log(JSON.stringify({ first, error, other }));
process.exit(0);
"""
    )
    assert got["error"]["status"] == 409 and got["error"]["body"]["session_id"] == got["first"]["id"]
    assert got["error"]["body"]["detail"] == "이미 2026-11-04 목록을 뽑는 중입니다."  # 백엔드와 같은 문구
    assert got["other"]["build"]["state"] == "building"


@needs_node
def test_a_mock_list_that_is_being_built_cannot_be_processed_or_organized():
    got = run_js(
        MOCK
        + """
config.mockBuildMs = 3000;
await api.createSession('2026-11-04');
const out = {};
for (const [name, call] of [['process', () => api.process('latest', 0, 1)], ['organize', () => api.organize('latest')]]) {
  try { await call(); out[name] = null; } catch (e) { out[name] = { status: e.status, body: e.body }; }
}
console.log(JSON.stringify(out));
process.exit(0);
"""
    )
    assert got["process"] == {"status": 409, "body": {"detail": "목록을 만드는 중입니다. 끝난 뒤에 처리할 수 있습니다."}}
    assert got["organize"] == {"status": 409, "body": {"detail": "목록을 만드는 중입니다. 끝난 뒤에 정리할 수 있습니다.", "remaining": 0}}


# --- 세션이 뽑을 때 쓴 조건으로 필터 문구를 만든다 (#57) ---

CRITERIA_JS = """
const { criteriaText, fmtWon, NO_RECORD } = await import(dir + '/criteria.js');
"""


@needs_node
def test_the_filter_text_is_made_from_the_criteria():
    got = run_js(
        CRITERIA_JS
        + """
const base = { filters: { min_avg_trading_value_krw: 1_000_000_000, avg_trading_value_window: 20, price_above_ma: [20, 60], rsi_max: null },
  exclude: ['preferred', 'spac', 'managed', 'zero_volume', 'new_listing', 'reit'], flow: { mode: 'mark', window_days: 5 } };
const changed = { filters: { min_avg_trading_value_krw: 2_000_000_000, avg_trading_value_window: 10, price_above_ma: [5], rsi_max: 70 },
  exclude: [], flow: { mode: 'filter', window_days: 3 } };
console.log(JSON.stringify({ base: criteriaText(base), changed: criteriaText(changed), none: criteriaText(null), NO_RECORD }));
"""
    )
    assert got["base"] == {
        "filter": "20일 평균 거래대금 10억↑ · 종가 > 20일선 · 종가 > 60일선",  # config.yaml 의 값이면 예전 고정 문구와 같다
        "extra": "제외: 우선주·스팩·관리종목·거래정지 의심·신규상장·리츠 · 수급: 표시만, 최근 5거래일",
    }
    assert got["changed"] == {
        "filter": "10일 평균 거래대금 20억↑ · 종가 > 5일선 · RSI < 70",
        "extra": "제외: 없음 · 수급: 약함 탈락, 최근 3거래일",
    }
    assert got["none"] == {"filter": "조건 기록 없음", "extra": "조건 기록 없음"} and got["NO_RECORD"] == "조건 기록 없음"


@needs_node
def test_money_is_written_in_korean_units():
    got = run_js(
        CRITERIA_JS
        + "console.log(JSON.stringify([1_000_000_000, 1_500_000_000, 100_000_000, 50_000_000, 123_456_789_000, 5_000].map(fmtWon)));"
    )
    assert got == ["10억", "15억", "1억", "5,000만", "1,234.57억", "5,000"]


@needs_node
def test_each_mock_session_has_its_own_criteria_and_the_old_one_has_none():
    got = run_js(
        MOCK
        + """
config.mockBuildMs = 50;
const list = await api.listSessions();
const created = await api.createSession('2026-11-03');
console.log(JSON.stringify({ list, created }));
process.exit(0);
"""
    )
    seeded, legacy = got["list"]
    assert seeded["criteria"]["filters"]["min_avg_trading_value_krw"] == 1_000_000_000 and legacy["criteria"] is None
    assert got["created"]["criteria"]["filters"]["min_avg_trading_value_krw"] == 2_000_000_000  # 설정을 바꾼 뒤에 뽑은 목록
    assert got["created"]["criteria"] != seeded["criteria"]


# --- 실패한 종목 다시 처리 (#77) ---


@needs_node
def test_the_screens_rule_for_codes_the_server_accepts_matches_the_backends():
    from krx_screener.analysis import server_accepts

    codes = ["005930", "0126Z0", "00680K", "12345", "1234567", "00593 ", "", "abcdef", "000010"]
    got = run_js(
        f"""
const {{ serverAccepts }} = await import(dir + '/retry.js');
console.log(JSON.stringify({json.dumps(codes)}.map((c) => serverAccepts(c))));
"""
    )
    assert got == [server_accepts(c) for c in codes]  # 숫자 6자리만. 영숫자 코드·우선주는 다시 보내도 서버가 거절한다


@needs_node
def test_the_retry_plan_leaves_out_failed_codes_the_server_would_refuse():
    got = run_js(
        """
const { retryPlan } = await import(dir + '/retry.js');
const it = (code, state) => ({ code, state });
const plan = retryPlan([it('000010', 'failed'), it('0126Z0', 'failed'), it('000030', 'done'), it('000040', 'pending'), it('000050', 'failed')]);
console.log(JSON.stringify({ retry: plan.retry.map((i) => i.code), excluded: plan.excluded.map((i) => i.code) }));
"""
    )
    assert got == {"retry": ["000010", "000050"], "excluded": ["0126Z0"]}


@needs_node
def test_the_mock_retry_answers_like_the_backends():
    got = run_js(
        MOCK
        + """
config.mockMsPerItem = 400;   // 응답(0.15초)이 올 때 아직 처리 중이도록
const wait = async () => { for (let i = 0; i < 400; i++) { if (!(await api.getSession('latest')).processing) break; await new Promise((r) => setTimeout(r, 20)); } };
const out = {};
out.first = await api.processFailed('latest');            // 예시 데이터에는 실패한 종목(순번 4)이 하나 있다
try { await api.processFailed('latest'); out.busy = null; } catch (e) { out.busy = { status: e.status, body: e.body }; }   // 처리 중에 다시 부르면 409
await wait();
out.after = (await api.getSession('latest')).items[3].state;
out.nothing = await api.processFailed('latest');          // 더 다시 보낼 종목이 없다
console.log(JSON.stringify(out));
process.exit(0);
"""
    )
    assert got["first"] == {"started": True, "queued": 1}  # 백엔드: 202 {"started": true, "queued": N}
    assert got["busy"] == {"status": 409, "body": {"detail": "이미 처리 중입니다."}}
    assert got["after"] == "done"
    assert got["nothing"] == {"started": False, "queued": 0}  # 백엔드: 200 {"started": false, "queued": 0}
