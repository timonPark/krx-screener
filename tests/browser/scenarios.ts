// 브라우저 시나리오 (#58). 사용: node scenarios.ts <시나리오> <화면 주소> <Chrome 포트> <프로필 폴더>
// 화면에서 본 사실을 JSON 한 줄로 내고, 판정은 pytest(tests/test_browser_screen.py)가 한다.
// Node 가 타입을 지우고 바로 실행한다 (22.18 이상). 타입 검사는 `npm run typecheck`.
import type { ChildProcess } from 'node:child_process';
import { launchChrome, Page, sleep } from './cdp.ts';

const [name, base, port, profile]: string[] = process.argv.slice(2);

// 테스트 서버(tests/browser_harness.py)의 가짜를 실행 중에 조작한다
const ctl = (values: Record<string, unknown>): Promise<Response> =>
  fetch(new URL('__ctl', base), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(values) });

// 화면에서 본 사실. 판정은 pytest 가 한다
type Facts = Record<string, unknown>;

interface SessionOption {
  value: string;
  label: string;
}

interface Row {
  seq: string;
  name: string;
  status: string;
  grade: string;
  score: string;
  rank: string;
}

// ---- 화면을 읽고 조작하는 도구 ----
interface Ui {
  text(id: string): Promise<string>;
  shown(id: string): Promise<boolean>;
  set(id: string, v: string | number): Promise<void>;
  click(id: string): Promise<void>;
  disabled(id: string): Promise<boolean>;
  pickSession(value: string): Promise<void>;
  sessions(): Promise<SessionOption[]>;
  error(): Promise<string | null>;
  rows(): Promise<Row[]>;
  idle(ms?: number): Promise<void>;
  api(path: string, opts?: Record<string, unknown>): Promise<[number, unknown]>;
}

const make = (page: Page): Ui => ({
  text: (id: string): Promise<string> => page.eval<string>(`document.getElementById('${id}').textContent.trim().replace(/\\s+/g, ' ')`),
  shown: (id: string): Promise<boolean> => page.eval<boolean>(`!document.getElementById('${id}').hidden`),
  set: (id: string, v: string | number): Promise<void> => page.eval<void>(`(() => { const e = document.getElementById('${id}'); e.value = '${v}'; e.dispatchEvent(new Event('input', { bubbles: true })); })()`),
  click: (id: string): Promise<void> => page.eval<void>(`document.getElementById('${id}').click()`),
  disabled: (id: string): Promise<boolean> => page.eval<boolean>(`document.getElementById('${id}').disabled`),
  pickSession: (value: string): Promise<void> => page.eval<void>(`(() => { const e = document.getElementById('session'); e.value = '${value}'; e.dispatchEvent(new Event('change', { bubbles: true })); })()`),
  sessions: (): Promise<SessionOption[]> => page.eval<SessionOption[]>(`[...document.getElementById('session').options].map((o) => ({ value: o.value, label: o.textContent.trim().replace(/\\s+/g, ' ') }))`),
  error: (): Promise<string | null> => page.eval<string | null>(`(() => { const n = document.getElementById('error-notice'); return n.hidden ? null : n.textContent.trim(); })()`),
  rows: (): Promise<Row[]> => page.eval<Row[]>(`[...document.querySelectorAll('#rows tr')].map((tr) => { const td = tr.querySelectorAll('td'); const c = (i) => td[i].textContent.trim().replace(/\\s+/g, ' ');
      return { seq: c(0), name: td[1].querySelector('.name').childNodes[0].textContent.trim(), status: td[7].querySelector('.status').textContent.trim(), grade: c(8), score: c(9), rank: c(10) }; })`),
  idle: (ms: number = 60000): Promise<void> => page.waitFor(`!document.getElementById('poll-dot').classList.contains('live') && document.getElementById('p-running').textContent === '0'`, ms, 100),
  api: (path: string, opts: Record<string, unknown> = {}): Promise<[number, unknown]> =>
    page.eval<[number, unknown]>(`fetch('${path}', ${JSON.stringify(opts)}).then(async (r) => [r.status, await r.json()])`),
});

type Scenario = (page: Page, ui: Ui, out: Facts) => Promise<void>;

// ---- 시나리오 ----
const scenarios: Record<string, Scenario> = {
  // 새 목록: 만드는 중 진행 → 같은 기준일 409 → 끝나면 표와 조건 문구
  async newList(page: Page, ui: Ui, out: Facts): Promise<void> {
    await page.goto(base);
    out.firstSession = (await ui.sessions())[0].label;                      // 세션이 없을 때의 안내
    await ctl({ build_gate: 3 });                                           // 시세를 3개까지만 받고 멈춘다
    await ui.set('asof', '2026-11-05');
    await ui.click('new-list-btn');
    await page.waitFor(`document.getElementById('build-detail').textContent.includes('3 / 8')`, 20000);
    out.building = {
      title: await ui.text('build-title'), detail: await ui.text('build-detail'), bar: await page.eval<string>(`document.getElementById('build-bar').style.width`),
      total: await ui.text('m-total'), session: (await ui.sessions())[0].label, poll: await ui.text('poll-text'),
      startDisabled: await ui.disabled('start-btn'), startHint: await ui.text('start-hint'), organizeDisabled: await ui.disabled('organize-btn'),
      rows: (await ui.rows()).length,
    };
    // 같은 기준일을 또 뽑으면 만드는 중인 세션을 가리키는 409 안내가 뜬다
    await page.waitFor(`!document.getElementById('new-list-btn').disabled`, 10000);
    await ui.click('new-list-btn');
    await page.waitFor(`!document.getElementById('error-notice').hidden`, 10000);
    out.duplicate = { error: await ui.error(), session: (await ui.sessions())[0].label, sessions: (await ui.sessions()).length };
    await ctl({ build_gate: null });                                        // 이어서 받는다
    await page.waitFor(`document.getElementById('build-card').hidden && document.getElementById('m-total').textContent === '8'`, 30000);
    out.ready = {
      total: await ui.text('m-total'), rows: (await ui.rows()).length, session: (await ui.sessions())[0].label, buildCardShown: await ui.shown('build-card'),
      filter: await ui.text('m-filter'), extra: await ui.text('m-extra'), startDisabled: await ui.disabled('start-btn'),
    };
    await sleep(1200);                                                      // 끝난 뒤의 갱신이 오류를 남기지 않는다
    out.errorAfter = await ui.error();
  },

  // 처리: 구간 처리 중 갱신 → 정리 막힘 → 모두 처리 → 정리 → 재분석 뒤 낡음 → 다시 정리
  async processAndOrganize(page: Page, ui: Ui, out: Facts): Promise<void> {
    await ctl({ errors: ['030530'], polls: 3 });                            // 4번째 종목은 실패한다
    await page.goto(base);
    await ui.set('asof', '2026-11-05');
    await ui.click('new-list-btn');
    await page.waitFor(`document.getElementById('build-card').hidden && document.getElementById('m-total').textContent === '8'`, 30000);
    await ui.set('offset', 0); await ui.set('count', 4);
    await ui.click('start-btn');
    await page.waitFor(`document.getElementById('p-running').textContent !== '0'`, 15000);   // 새로고침 없이 처리 중이 보인다
    out.running = { poll: await ui.text('poll-text'), startDisabled: await ui.disabled('start-btn'), offsetDisabled: await ui.disabled('offset') };
    await ui.idle();
    out.afterFirst = { progress: await ui.text('p-processed'), done: await ui.text('p-done'), failed: await ui.text('p-failed'), pending: await ui.text('p-pending'), rows: (await ui.rows()).map((r: Row): string => `${r.seq}:${r.status}`) };
    out.retry = { label: await ui.text('retry-btn'), disabled: await ui.disabled('retry-btn'), hint: await ui.text('retry-hint') };   // 실패한 종목 다시 처리 (#77)
    await ui.set('offset', 3); await ui.set('count', 1);              // 실패한 4번만 든 구간: 구간 처리는 실패한 종목을 보내지 않는다
    out.failedOnlyRange = { startDisabled: await ui.disabled('start-btn'), hint: await ui.text('start-hint'), eta: await ui.text('eta') };
    await ui.set('offset', 0); await ui.set('count', 4);              // 3 완료 + 1 실패 구간: 보낼 것이 없다
    out.doneAndFailedRange = { startDisabled: await ui.disabled('start-btn'), hint: await ui.text('start-hint') };
    out.gated = { disabled: await ui.disabled('organize-btn'), hint: await ui.text('organize-hint') };   // 남은 종목이 있으면 정리가 막힌다
    out.gatedApi = (await ui.api('/api/sessions/latest/organize', { method: 'POST' }))[0];
    await ui.set('offset', 4); await ui.set('count', 100);
    await ui.click('start-btn');
    await page.waitFor(`document.getElementById('p-pending').textContent === '0'`, 60000);
    await ui.idle();
    out.allProcessed = { done: await ui.text('p-done'), failed: await ui.text('p-failed'), organizeDisabled: await ui.disabled('organize-btn') };
    out.scores = (await ui.rows()).map((r: Row): string => `${r.seq}:${r.score.split('에이전트')[0].trim()}`);   // 화면에 보이는 점수 글 (에이전트 수는 뺀다)
    await ui.click('organize-btn');
    await page.waitFor(`document.getElementById('sort').value === 'rank'`, 15000);
    out.organized = { sort: await page.eval<string>(`document.getElementById('sort').value`), stale: await ui.shown('stale-notice'), rows: (await ui.rows()).map((r: Row): string => `${r.seq}:${r.rank}`) };
    await ctl({ errors: [] });                                              // 실패했던 종목이 이번에는 성공한다
    page.acceptDialogs = false;                                             // 확인창에서 취소하면 아무것도 보내지 않는다
    await ui.click('retry-btn');
    await sleep(1500);
    out.cancelled = { dialogs: [...page.dialogs], failed: await ui.text('p-failed'), startedNothing: await ui.text('p-running') === '0' };
    page.acceptDialogs = true;
    page.dialogs.length = 0;
    await ui.click('retry-btn');                                            // 확인하면 실패한 종목만 다시 보낸다
    await page.waitFor(`document.getElementById('p-failed').textContent === '0'`, 30000);
    out.confirmed = { dialogs: [...page.dialogs] };
    await ui.idle();
    await page.waitFor(`!document.getElementById('stale-notice').hidden`, 15000);
    out.stale = { shown: await ui.shown('stale-notice'), organizeHint: await ui.text('organize-hint') };
    await ui.click('organize-btn');
    await page.waitFor(`document.getElementById('stale-notice').hidden`, 15000);
    out.restored = { stale: await ui.shown('stale-notice'), rows: (await ui.rows()).map((r: Row): string => `${r.seq}:${r.rank}`) };
  },

  // 오류 알림: 동작 오류는 갱신이 성공해도 남고, 조회 오류는 다음 조회가 성공하면 사라진다
  async errors(page: Page, ui: Ui, out: Facts): Promise<void> {
    await ctl({ polls: 3 });
    await page.goto(base);
    await ui.set('asof', '2026-11-05');
    await ui.click('new-list-btn');
    await page.waitFor(`document.getElementById('build-card').hidden && document.getElementById('m-total').textContent === '8'`, 30000);
    const settle = async (): Promise<string | null> => { await sleep(1500); return ui.error(); };       // 갱신(0.4초 간격)이 여러 번 돈 뒤에도 남아 있는가

    // (a) 다른 곳이 먼저 처리를 시작한 세션을 이 화면에서 처리 시작 → 409
    await ui.set('offset', 0); await ui.set('count', 3);
    await ui.api('/api/sessions/latest/process', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ offset: 0, count: 2 }) });
    await sleep(200);
    await ui.click('start-btn');
    await page.waitFor(`!document.getElementById('error-notice').hidden`, 10000);
    out.conflict = { first: await ui.error(), later: await settle() };
    await ui.idle();

    // (b) 분석 서버가 꺼져 있을 때 처리 시작 → 503, 갱신이 성공해도 남는다
    await ctl({ health_error: 'TradingAgents-KR 서버에 연결하지 못했습니다 (http://127.0.0.1:8000): ConnectionError' });
    await page.waitFor(`!document.getElementById('start-btn').disabled`, 15000);
    await ui.set('offset', 3); await ui.set('count', 2);
    await ui.click('start-btn');
    await page.waitFor(`document.getElementById('error-notice').textContent.includes('TradingAgents-KR')`, 10000);   // 앞의 409 알림이 바뀔 때까지
    out.analysisDown = { first: await ui.error(), later: await settle() };
    await ctl({ health_error: null });
    await ui.click('start-btn');                                           // 풀린 뒤 다시 누르면 처리가 시작되고 알림이 사라진다
    await page.waitFor(`document.getElementById('error-notice').hidden`, 10000);
    out.retried = { error: await ui.error() };
    await ui.idle();

    // (c) 거래일이 아닌 기준일로 새 목록 → 422
    await ui.set('asof', '2026-11-08');
    await ui.click('new-list-btn');
    await page.waitFor(`document.getElementById('error-notice').textContent.includes('거래일이 아니거나')`, 10000);
    out.badDate = { first: await ui.error(), later: await settle() };

    // (d) DB 가 꺼진 동안의 조회 실패는 계속 보이고, 복구 뒤 조회가 성공하면 사라진다
    await ctl({ db_down: true });
    const current: string = await page.eval<string>(`document.getElementById('session').value`);
    await ui.pickSession(current);                                         // 쉬는 중에는 주기 갱신이 없으니 세션을 다시 골라 조회를 부른다
    await page.waitFor(`!document.getElementById('error-notice').hidden && document.getElementById('error-notice').textContent.includes('PostgreSQL')`, 15000);
    out.dbDown = { first: await ui.error(), later: await settle() };
    await ctl({ db_down: false });
    await ui.pickSession(current);
    await page.waitFor(`document.getElementById('error-notice').hidden`, 15000);
    out.dbBack = { error: await ui.error() };
  },

  // 쉬는 중인 화면이 다른 곳(API)이 시작한 처리를 새로고침 없이 알아챈다 (#71)
  async startedElsewhere(page: Page, ui: Ui, out: Facts): Promise<void> {
    await ctl({ polls: 15 });                                                // 종목마다 1.5초쯤 걸려서 처리 중이 보인다
    await page.goto(base);
    await ui.set('asof', '2026-11-05');
    await ui.click('new-list-btn');
    await page.waitFor(`document.getElementById('build-card').hidden && document.getElementById('m-total').textContent === '8'`, 30000);
    out.idleBefore = { poll: await ui.text('poll-text'), start: await ui.disabled('start-btn') };
    // 화면은 건드리지 않고 화면 밖(API)에서 처리를 시작한다
    out.started = (await ui.api('/api/sessions/latest/process', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ offset: 0, count: 2 }) }))[0];
    await page.waitFor(`document.getElementById('start-btn').textContent === '처리 중…'`, 15000);   // 새로고침 없이 처리 중으로 바뀐다
    out.noticed = { poll: await ui.text('poll-text'), startDisabled: await ui.disabled('start-btn'), offsetDisabled: await ui.disabled('offset') };
    await ui.idle();
    out.afterFirst = { done: await ui.text('p-done'), start: await ui.text('start-btn') };
    // 처리가 끝난 뒤에도 쉬는 중 확인으로 돌아가서, 다음에 다른 곳이 시작한 처리도 알아챈다
    await ui.api('/api/sessions/latest/process', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ offset: 2, count: 1 }) });
    await page.waitFor(`document.getElementById('start-btn').textContent === '처리 중…'`, 15000);
    await ui.idle();
    out.afterSecond = { done: await ui.text('p-done'), poll: await ui.text('poll-text') };
  },

  // 숨겨진 탭은 쉬는 중 조회를 하지 않고, 다시 보이면 바로 한 번 조회한다 (#71)
  async hiddenTab(page: Page, ui: Ui, out: Facts): Promise<void> {
    await page.goto(base);
    await ui.set('asof', '2026-11-05');
    await ui.click('new-list-btn');
    await page.waitFor(`document.getElementById('build-card').hidden && document.getElementById('m-total').textContent === '8'`, 30000);
    const count: string = `performance.getEntriesByType('resource').filter((e) => /\\/api\\/sessions\\/[0-9a-f-]{36}$/.test(e.name)).length`;
    // 보이는 동안은 쉬는 중에도 조회한다 (1.5초 간격이라 4초 안에 2번은 간다)
    const before: number = await page.eval<number>(count);
    await sleep(4000);
    out.visibleGrowth = (await page.eval<number>(count)) - before;
    // 탭이 숨겨지면 멈춘다: 숨긴 채 4초(조회 간격의 두 배 넘게) 지나도 조회가 늘지 않는다
    await page.eval<void>(`Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })`);
    const hiddenStart: number = await page.eval<number>(count);              // 숨기자마자 센다: 이미 예약된 조회가 지나가도 부르지 않아야 한다
    await sleep(4000);
    out.hiddenGrowth = (await page.eval<number>(count)) - hiddenStart;
    // 다시 보이면 바로 한 번 조회한다 (예약된 타이머가 아니라 visibilitychange 가 부른다: 숨긴 동안 타이머는 멈췄다)
    const shownStart: number = await page.eval<number>(count);
    await page.eval<void>(`(() => { delete document.visibilityState; document.dispatchEvent(new Event('visibilitychange')); })()`);
    await sleep(500);
    out.shownGrowth = (await page.eval<number>(count)) - shownStart;
  },

  // 만들지 못한 목록: 이유를 보이고 처리·정리는 막힌다
  async failedList(page: Page, ui: Ui, out: Facts): Promise<void> {
    await page.goto(base);
    out.card = {
      shown: await ui.shown('build-card'), title: await ui.text('build-title'), detail: await ui.text('build-detail'),
      hint: await ui.text('build-hint'), session: (await ui.sessions())[0].label, total: await ui.text('m-total'),
      startDisabled: await ui.disabled('start-btn'), startHint: await ui.text('start-hint'), organizeDisabled: await ui.disabled('organize-btn'),
    };
  },

  // 예시 모드(?mock=1): 백엔드 없이 화면이 돌고 새 목록이 끝까지 만들어진다
  async mock(page: Page, ui: Ui, out: Facts): Promise<void> {
    await page.goto(new URL('?mock=1', base).href);
    out.tag = await ui.shown('mock-tag');
    out.first = { total: await ui.text('m-total'), rows: (await ui.rows()).length, filter: await ui.text('m-filter') };
    await ui.set('asof', '2026-11-04');
    await ui.click('new-list-btn');
    await page.waitFor(`!document.getElementById('build-card').hidden`, 10000);
    out.building = await ui.text('build-title');
    await page.waitFor(`document.getElementById('build-card').hidden && document.getElementById('m-total').textContent !== '—'`, 30000);
    out.ready = { total: await ui.text('m-total'), session: (await ui.sessions())[0].label };
  },
};

const chrome: ChildProcess = await launchChrome(Number(port), profile);
const out: Facts = {};
let page: Page | undefined;
try {
  page = await Page.open(Number(port));
  const run: Scenario | undefined = scenarios[name];
  if (!run) throw new Error(`없는 시나리오: ${name}`);
  await run(page, make(page), out);
  out.consoleErrors = page.consoleErrors();
  console.log(JSON.stringify(out));
} catch (e) {
  const message: string = e instanceof Error ? e.message : String(e);
  // 실패했을 때 그 순간의 화면과 서버 상태를 같이 남긴다
  const screen: string | null = await page?.eval<string>(`JSON.stringify({ poll: document.getElementById('poll-text').textContent, live: document.getElementById('poll-dot').classList.contains('live'), progress: document.getElementById('p-processed').textContent, running: document.getElementById('p-running').textContent, error: document.getElementById('error-notice').textContent })`).catch((): null => null) ?? null;
  const server: string | null = await fetch(new URL('api/sessions/latest', base))
    .then((r: Response): Promise<{ processing: boolean; counts: unknown }> => r.json() as Promise<{ processing: boolean; counts: unknown }>)
    .then((d: { processing: boolean; counts: unknown }): string => JSON.stringify({ processing: d.processing, counts: d.counts }))
    .catch((): null => null);
  console.error(`✗ ${name}: ${message}\n화면: ${screen}\n서버: ${server}\n지금까지 본 것: ${JSON.stringify(out)}\n콘솔 오류: ${JSON.stringify(page?.consoleErrors() ?? [])}`);
  process.exitCode = 1;
} finally {
  page?.close();
  chrome.kill();
}
