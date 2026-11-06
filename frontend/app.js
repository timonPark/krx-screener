import { api, ApiError } from './api.js';
import { config } from './config.js';

const $ = (id) => document.getElementById(id);
const state = { sessions: [], sessionId: null, data: null, filter: 'all', sort: 'seq', pollTimer: null, busy: false };

const STATUS_LABEL = { pending: '대기', running: '처리 중', done: '완료', failed: '실패' };
const GRADE_CLASS = { '매수': 'g-buy', '비중 확대': 'g-overweight', '보유': 'g-hold', '비중 축소': 'g-underweight', '매도': 'g-sell' };
const FLOW_CLASS = { '양호': 'flow-good', '부분': 'flow-partial', '약함': 'flow-weak' };

// ---------- 형식 ----------
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtInt = (n) => (n == null ? '—' : Number(n).toLocaleString('ko-KR'));
const fmtEok = (won) => (won == null ? '—' : `${Math.round(won / 1e8).toLocaleString('ko-KR')}억`);
const kst = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
const kstTime = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
function fmtDateTime(iso) {
  if (!iso) return '';
  const p = Object.fromEntries(kst.formatToParts(new Date(iso)).map((x) => [x.type, x.value]));
  return `${p.month}-${p.day} ${p.hour}:${p.minute}`;
}
const sessionLabel = (s) => `${s.as_of}-${String(s.id).replace(/-/g, '').slice(0, 8)}`;
function fmtScore(s) {
  if (s == null) return '';
  const sign = s > 0 ? '+' : s < 0 ? '−' : '';
  return sign + Math.abs(s).toFixed(2);
}
function fmtMinutes(m) {
  if (m < 60) return `${m}분`;
  const h = Math.floor(m / 60), r = m % 60;
  return r ? `${h}시간 ${r}분` : `${h}시간`;
}
function prevBusinessDay() {
  const now = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Seoul' }));
  const d = new Date(now);
  do { d.setDate(d.getDate() - 1); } while (d.getDay() === 0 || d.getDay() === 6);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// ---------- 알림 ----------
function showError(err) {
  const el = $('error-notice');
  if (!err) { el.hidden = true; el.textContent = ''; return; }
  el.textContent = err instanceof ApiError ? err.message : String(err);
  el.hidden = false;
}

// ---------- 데이터 ----------
async function loadSessions(selectId) {
  state.sessions = await api.listSessions();
  const sel = $('session');
  sel.innerHTML = state.sessions.length
    ? state.sessions.map((s, i) => `<option value="${esc(s.id)}">${esc(sessionLabel(s))}${i === 0 ? ' (최근)' : ''}</option>`).join('')
    : '<option value="">세션 없음 — 새 목록을 뽑으세요</option>';
  state.sessionId = selectId || state.sessions[0]?.id || null;
  if (state.sessionId) sel.value = state.sessionId;
}

async function refresh() {
  clearTimeout(state.pollTimer);
  if (!state.sessionId) { state.data = null; render(); return; }
  try {
    state.data = await api.getSession(state.sessionId);
    showError(null);
  } catch (err) {
    showError(err);
  }
  render();
  if (state.data?.processing) {
    state.pollTimer = setTimeout(refresh, config.mock ? config.mockPollMs : config.pollMs);
  }
}

// ---------- 그리기 ----------
function render() {
  const d = state.data;
  const items = d?.items || [];
  const total = d?.session.total ?? items.length;
  const c = d?.counts || { pending: 0, running: 0, done: 0, failed: 0 };

  // 세션 정보
  $('m-asof').textContent = d?.session.as_of || '—';
  $('m-created').textContent = d ? fmtDateTime(d.session.created_at) : '—';
  $('m-total').textContent = d ? fmtInt(total) : '—';

  // 갱신 표시
  $('poll-dot').classList.toggle('live', !!d?.processing);
  const last = kstTime.format(new Date());
  $('poll-text').textContent = d?.processing
    ? `처리 중 · ${(config.mock ? config.mockPollMs : config.pollMs) / 1000}초마다 갱신 · 마지막 ${last}`
    : `마지막 갱신 ${last}`;

  // 진행
  const processed = c.done + c.failed;
  $('p-processed').textContent = `${processed} / ${total}`;
  ['done', 'running', 'failed', 'pending'].forEach((k) => { $(`p-${k}`).textContent = c[k]; });
  const pct = (n) => (total ? `${(n / total) * 100}%` : '0%');
  $('b-done').style.width = pct(c.done);
  $('b-failed').style.width = pct(c.failed);
  $('b-running').style.width = pct(c.running);

  $('stale-notice').hidden = !d?.rank_stale;

  renderChips(c, total);
  renderRows(items, total);
  renderProcess(items, total);
  renderOrganize(c, d);
}

function renderChips(c, total) {
  const defs = [['all', '전체', total], ['pending', '대기', c.pending], ['running', '처리 중', c.running], ['done', '완료', c.done], ['failed', '실패', c.failed]];
  $('chips').innerHTML = defs.map(([key, label, n]) =>
    `<button type="button" class="chip" data-filter="${key}" aria-pressed="${state.filter === key}">${label} <span class="num">${n}</span></button>`).join('');
}

function sortItems(list) {
  const arr = list.slice();
  if (state.sort === 'score') arr.sort((a, b) => ((b.score ?? -99) - (a.score ?? -99)) || (a.seq - b.seq));
  else if (state.sort === 'rank') arr.sort((a, b) => ((a.rank ?? 1e9) - (b.rank ?? 1e9)) || (a.seq - b.seq));
  else arr.sort((a, b) => a.seq - b.seq);
  return arr;
}

function scoreCell(it) {
  if (it.state !== 'done' || it.score == null) return '<span class="faint">—</span>';
  const s = it.score;
  const w = Math.round((Math.min(Math.abs(s), 2) / 2) * 30);
  const cls = s > 0 ? 'pos' : s < 0 ? 'neg' : '';
  return `<div class="score"><span class="num score-val ${cls}">${fmtScore(s)}</span>`
    + `<span class="score-bar" aria-hidden="true">${w ? `<span class="${cls}" style="width:${w}px"></span>` : ''}</span></div>`
    + `<div class="tiny">에이전트 <span class="num">${it.agents_rated}/${it.agents_total}</span></div>`;
}

function statusSub(it) {
  if (it.state === 'done') return fmtDateTime(it.analyzed_at);
  if (it.state === 'failed') return `${it.error_message || '실패'} · 다시 처리하면 재시도`;
  if (it.state === 'running') return '분석 중';
  return '';
}

function reportCell(it) {
  if (!it.run_id) return '<span class="faint">—</span>';
  if (!config.reportUrlTemplate) return `<span class="num" style="font-size:12px">${esc(it.run_id)}</span>`;
  const href = config.reportUrlTemplate.replace('{run_id}', encodeURIComponent(it.run_id));
  return `<a class="num" style="font-size:12px" href="${esc(href)}" target="_blank" rel="noopener">${esc(it.run_id)}</a>`;
}

function renderRows(items, total) {
  const stale = !!state.data?.rank_stale;
  const visible = sortItems(items.filter((it) => state.filter === 'all' || it.state === state.filter));
  $('rows').innerHTML = visible.map((it) => {
    const flowCls = FLOW_CLASS[it.flow_status] || 'flow-unknown';
    const flowLabel = FLOW_CLASS[it.flow_status] ? it.flow_status : '미확인';
    const flowTitle = it.foreign_net_5d == null
      ? '최근 5거래일 순매수를 읽지 못함 (수급 0 아님)'
      : `외국인 ${fmtInt(it.foreign_net_5d)}주 · 기관 ${fmtInt(it.institution_net_5d)}주 (5거래일)`;
    const flags = (it.is_caution ? '<span class="flag">투자주의</span>' : '') + (it.is_foreign ? '<span class="flag">외국기업</span>' : '');
    const grade = it.state === 'done' && it.decision_label
      ? `<span class="grade ${GRADE_CLASS[it.decision_label] || 'g-hold'}">${esc(it.decision_label)}</span>`
      : '<span class="faint">—</span>';
    const rank = it.rank == null
      ? '<span class="num rank faint">—</span>'
      : `<span class="num rank${stale ? ' stale' : ''}">${it.rank}</span>${stale ? '<div class="stale-tag">낡음</div>' : ''}`;
    return `<tr class="${it.state === 'running' ? 'is-running' : ''}">
      <td class="r num muted">${it.seq}</td>
      <td class="nw"><div class="name">${esc(it.name)}${flags}</div><div class="sub"><span class="num">${esc(it.code)}</span> · ${esc(it.market)}</div></td>
      <td class="theme"><div class="sector">${esc(it.sector_name) || '<span class="faint">—</span>'}</div><div class="desc" title="${esc(it.description)}">${esc(it.description)}</div></td>
      <td class="nw"><span class="flow ${flowCls}" title="${esc(flowTitle)}"><i aria-hidden="true"></i>${flowLabel}</span></td>
      <td class="r num">${fmtInt(it.close)}</td>
      <td class="r num">${fmtEok(it.avg_trading_value)}</td>
      <td class="r num">${it.rsi == null ? '—' : Number(it.rsi).toFixed(1)}</td>
      <td class="nw"><span class="status st-${it.state}"><i aria-hidden="true"></i>${STATUS_LABEL[it.state] || esc(it.state)}</span><div class="tiny">${esc(statusSub(it))}</div></td>
      <td class="nw">${grade}</td>
      <td class="nw">${scoreCell(it)}</td>
      <td class="r nw">${rank}</td>
      <td class="nw">${reportCell(it)}</td>
    </tr>`;
  }).join('');
  $('empty').hidden = visible.length > 0;
  $('row-count').textContent = `${fmtInt(total)}개 중 ${fmtInt(visible.length)}개 표시`;
}

function readRange() {
  const offset = Number($('offset').value);
  const count = Number($('count').value);
  const valid = Number.isInteger(offset) && offset >= 0 && Number.isInteger(count) && count > 0;
  return { offset, count, valid };
}

function renderProcess(items, total) {
  const processing = !!state.data?.processing;
  const { offset, count, valid } = readRange();
  const inRange = valid ? items.filter((it) => it.seq > offset && it.seq <= offset + count) : [];
  const todo = inRange.filter((it) => it.state !== 'done').length;
  const end = Math.min(total, offset + count);

  $('range').textContent = valid && offset < total ? `${offset + 1}–${end}` : '—';
  $('eta').textContent = valid ? fmtMinutes(todo * config.minutesPerItem) : '—';
  $('offset').disabled = processing;
  $('count').disabled = processing;

  let hint = '누르면 바로 시작 응답을 받고 처리는 뒤에서 진행됩니다. 진행 상황은 위 표에서 갱신됩니다.';
  let disabled = state.busy || !state.data;
  if (processing) {
    const cur = items.find((it) => it.state === 'running');
    hint = `이 세션은 이미 처리 중입니다${cur ? ` (지금 ${cur.seq}번 ${cur.name})` : ''}. 서버에 취소 기능이 없어 중간에 멈출 수 없습니다.`;
    disabled = true;
  } else if (!valid) {
    hint = '오프셋은 0 이상, 개수는 1 이상의 정수로 입력하세요.'; disabled = true;
  } else if (offset >= total) {
    hint = `오프셋이 종목 수(${total})보다 크거나 같습니다.`; disabled = true;
  } else if (todo === 0) {
    hint = '이 구간의 종목은 모두 끝났습니다. 다른 구간을 고르세요.'; disabled = true;
  } else {
    hint = `이 구간에서 ${todo}개를 보냅니다 (끝난 ${inRange.length - todo}개는 건너뜀). ` + hint;
  }
  $('start-btn').disabled = disabled;
  $('start-btn').textContent = processing ? '처리 중…' : '처리 시작';
  $('start-hint').textContent = hint;
}

function renderOrganize(c, d) {
  const remaining = c.pending + c.running;
  const btn = $('organize-btn');
  btn.textContent = d?.ranked_at ? '정리 다시 실행' : '정리 실행';
  if (!d || !d.session.total) { btn.disabled = true; $('organize-hint').textContent = ''; return; }
  if (remaining > 0) {
    btn.disabled = true;
    $('organize-hint').textContent = `남은 ${remaining}개가 끝나야 실행할 수 있습니다`;
  } else {
    btn.disabled = state.busy;
    $('organize-hint').textContent = d.ranked_at && !d.rank_stale
      ? `최신 순위입니다 (${fmtDateTime(d.ranked_at)})`
      : (c.failed ? `실패 ${c.failed}개는 비교에서 제외됩니다` : '실행할 수 있습니다');
  }
}

// ---------- 동작 ----------
async function withBusy(fn) {
  state.busy = true; render();
  try { await fn(); showError(null); } catch (err) { showError(err); } finally { state.busy = false; }
  await refresh();
}

$('session').addEventListener('change', (e) => { state.sessionId = e.target.value; refresh(); });
$('sort').addEventListener('change', (e) => { state.sort = e.target.value; render(); });
$('chips').addEventListener('click', (e) => {
  const b = e.target.closest('[data-filter]');
  if (b) { state.filter = b.dataset.filter; render(); }
});
['offset', 'count'].forEach((id) => $(id).addEventListener('input', render));

$('new-list').addEventListener('submit', (e) => {
  e.preventDefault();
  const asOf = $('asof').value;
  const btn = $('new-list-btn');
  btn.disabled = true; btn.textContent = '뽑는 중…';
  withBusy(async () => {
    const s = await api.createSession(asOf);
    await loadSessions(s.id);
    state.filter = 'all'; state.sort = 'seq';
    $('offset').value = 0;
  }).finally(() => { btn.disabled = false; btn.textContent = '새 목록 뽑기'; });
});

$('process').addEventListener('submit', (e) => {
  e.preventDefault();
  const { offset, count, valid } = readRange();
  if (!valid) return;
  withBusy(() => api.process(state.sessionId, offset, count));
});

$('organize-btn').addEventListener('click', () => {
  withBusy(async () => {
    await api.organize(state.sessionId);
    state.sort = 'rank'; $('sort').value = 'rank';
  });
});

// ---------- 시작 ----------
(async function init() {
  $('mock-tag').hidden = !config.mock;
  $('asof').value = prevBusinessDay();
  try {
    await loadSessions();
  } catch (err) {
    showError(err);
  }
  await refresh();
})();
