// 백엔드 없이 화면을 확인하기 위한 예시 API (?mock=1).
// 시세·수급은 results/2026-11-05/candidates.csv 의 실제 값, 앞 3종목 등급은 analysis.csv 의 실제 결과다.
// 그 외 등급·점수·업종 설명은 화면 확인용 예시다.
import { config } from './config.js';

// code, name, market, close, avg_trading_value, rsi, flow, foreign5d, inst5d, sector, desc, [grade, score, rated]
const ROWS = [
  ['028050', '삼성E&A', 'KOSPI', 64800, 201712536740, 67.2, '부분', -624606, 645940, '건설', '화공·비화공 플랜트 설계·조달·시공(EPC)', ['보유', 0.3, 10]],
  ['207940', '삼성바이오로직스', 'KOSPI', 1519000, 114812586050, 64.5, '부분', -55526, 117512, '생물공학', '바이오의약품 위탁개발생산(CDMO)', ['비중 축소', -0.6, 10]],
  ['003490', '대한항공', 'KOSPI', 30300, 78384368135, 61.2, '양호', 581950, 3195614, '항공사', '여객·화물 운송', ['보유', 0.1, 10]],
  ['030530', '원익홀딩스', 'KOSDAQ', 26550, 70126006650, 50.1, '양호', 664820, 204972, '반도체와반도체장비', '반도체 장비·특수가스 사업 지주회사', null],
  ['064400', 'LG씨엔에스', 'KOSPI', 72800, 61110267525, 50.6, '부분', -295085, 570209, 'IT서비스', 'IT 시스템 구축·클라우드·AI 서비스', ['비중 확대', 0.7, 10]],
  ['000810', '삼성화재', 'KOSPI', 658000, 58706948800, 61.0, '부분', -58756, 68160, '손해보험', '손해보험 사업', ['보유', 0.2, 10]],
  ['128940', '한미약품', 'KOSPI', 465000, 54309443875, 60.1, '부분', -121522, 247038, '제약', '개량신약·바이오신약 개발', ['비중 확대', 0.9, 10]],
  ['033780', 'KT&G', 'KOSPI', 196900, 52433254115, 66.8, '부분', -168504, 249918, '담배', '담배·홍삼 사업', ['보유', 0, 10]],
  ['053800', '안랩', 'KOSDAQ', 97500, 48887925060, 53.8, '약함', -50390, -49436, '소프트웨어', '백신·보안 솔루션 개발', ['비중 축소', -0.5, 9]],
  ['015760', '한국전력', 'KOSPI', 31200, 36474566735, 60.8, '부분', -543941, 2778157, '전기유틸리티', '전력 송배전·판매', ['비중 확대', 0.6, 10]],
  ['328130', '루닛', 'KOSDAQ', 14710, 28231017258, 60.6, '부분', -548121, 995505, '건강관리기술', 'AI 기반 암 진단·치료 솔루션', ['매수', 1.3, 10]],
  ['086280', '현대글로비스', 'KOSPI', 200000, 27624551525, 57.3, '부분', -55633, 265316, '물류', '완성차 물류·해운', ['보유', 0.1, 10]],
  ['000100', '유한양행', 'KOSPI', 82000, 26186807855, 56.9, '미확인', null, null, '제약', '의약품 제조·판매', ['비중 확대', 0.8, 10]],
  ['263860', '지니언스', 'KOSDAQ', 23250, 24033763926, 51.6, '약함', -116682, -91407, '소프트웨어', '네트워크 접근제어(NAC)·보안', ['보유', -0.1, 10]],
];

// 목록을 만드는 중의 상태 (백엔드의 build). 완성된 세션은 ready 다.
const READY = { state: 'ready', stage: '', done: 0, total: 0, error: '' };
// 목록을 뽑을 때 쓴 조건 (백엔드의 session.criteria). 새로 뽑는 예시 목록은 '설정을 바꾼 뒤' 를 흉내 내려고 다른 값을 쓴다
const CRITERIA = {
  filters: { min_avg_trading_value_krw: 1_000_000_000, avg_trading_value_window: 20, price_above_ma: [20, 60], rsi_max: null },
  exclude: ['preferred', 'spac', 'managed', 'zero_volume', 'new_listing', 'reit'],
  flow: { mode: 'mark', window_days: 5 },
};
const NEW_CRITERIA = {
  filters: { min_avg_trading_value_krw: 2_000_000_000, avg_trading_value_window: 20, price_above_ma: [5, 20], rsi_max: 70 },
  exclude: ['preferred', 'spac', 'reit'],
  flow: { mode: 'filter', window_days: 3 },
};
const PRICE_COUNT = 2398;   // 시세를 받는 전종목 수 (실제 값을 흉내 낸다)

// 보고서 번호 = 종목코드_저장일_저장시각 (TradingAgents-KR 의 보고서 폴더 이름, 예: 028050_20261106_172435)
const SEEDED_STAMPS = ['20261106_172435', '20261106_175036', '20261106_181027'];
const pad = (n) => String(n).padStart(2, '0');
const stamp = (d = new Date()) =>
  `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
const randId = () => Math.random().toString(16).slice(2, 14).padEnd(12, '0');
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : `${randId()}-mock`).replace(/-/g, '');

function makeSession(asOf, seeded, criteria = NEW_CRITERIA) {
  const id = seeded ? '3f2a9c1b7d4e4a0f9b21c6e8d5a03f17' : uuid();
  const items = ROWS.map((r, i) => {
    const item = {
      seq: i + 1, code: r[0], name: r[1], market: r[2], close: r[3], avg_trading_value: r[4], rsi: r[5],
      flow_status: r[6], foreign_net_5d: r[7], institution_net_5d: r[8],
      sector_code: '', sector_name: r[9], description: r[10],
      is_caution: false, is_foreign: false,
      state: 'pending', analyzed_at: null, error_message: '', report_id: '',
      decision_label: '', score: null, agents_rated: 0, agents_total: 0, rank: null,
      _result: r[11],
    };
    if (seeded && i < 3) finish(item, SEEDED_STAMPS[i]);
    if (seeded && i === 3) { item.state = 'failed'; item.error_message = '시간 초과 (900초)'; item.analyzed_at = new Date().toISOString(); }
    return item;
  });
  return {
    session: { id, as_of: asOf, created_at: new Date().toISOString(), total: items.length, build: { ...READY }, criteria },
    items, processing: false, ranked_at: null, rank_stale: false,
  };
}

function finish(item, reportStamp) {
  const res = item._result || ['보유', 0, 10];
  item.state = 'done';
  item.analyzed_at = new Date().toISOString();
  item.report_id = `${item.code}_${reportStamp || stamp()}`;
  item.decision_label = res[0];
  item.score = res[1];
  item.agents_rated = res[2];
  item.agents_total = 10;   // 끝난 종목은 에이전트 10개 기준 (처리 전에는 0, 백엔드와 같다)
  item.error_message = '';
}

// 새 목록을 뒤에서 만드는 것을 흉내 낸다: 시세 70% → 수급 15% → 업종 15% 의 시간으로 진행하고, 끝나면 종목을 채운다
function simulateBuild(s, items) {
  const started = Date.now();
  const stages = [['prices', 0.7, PRICE_COUNT], ['flow', 0.15, items.length], ['sector', 0.15, items.length]];
  const tick = () => {
    const frac = Math.min(1, (Date.now() - started) / config.mockBuildMs);
    if (frac >= 1) {
      s.items = items;
      s.session.total = items.length;
      s.session.build = { state: 'ready', stage: '', done: items.length, total: items.length, error: '' };
      return;
    }
    let from = 0;
    for (const [stage, share, total] of stages) {
      if (frac < from + share) {
        s.session.build = { state: 'building', stage, done: Math.floor(((frac - from) / share) * total), total, error: '' };
        break;
      }
      from += share;
    }
    setTimeout(tick, 100);
  };
  tick();
}

function counts(items) {
  const c = { pending: 0, running: 0, done: 0, failed: 0 };
  items.forEach((it) => { c[it.state] += 1; });
  return c;
}

export function createMockApi(ApiError) {
  // 맨 뒤 세션은 조건을 저장하지 않던 때의 세션이다 (criteria null → "조건 기록 없음")
  const sessions = [makeSession('2026-11-05', true, CRITERIA), makeSession('2026-11-04', false, null)];
  const find = (id) => (!id || id === 'latest' ? sessions[0] : sessions.find((s) => s.session.id === id));
  // 값은 지연이 끝난 시점에 만든다: 만드는 중에 세션과 종목이 어긋난 채로 복사되지 않게 한다
  const delay = (make) => new Promise((ok) => setTimeout(() => ok(structuredClone(make())), 150));
  const view = (s) => {
    const items = s.items.map(({ _result, ...rest }) => rest);
    return { session: s.session, processing: s.processing, ranked_at: s.ranked_at, rank_stale: s.rank_stale, counts: counts(s.items), items };
  };

  return {
    async listSessions() { return delay(() => sessions.map((s) => s.session)); },
    async createSession(asOf) {
      const running = sessions.find((x) => x.session.build.state === 'building' && x.session.as_of === asOf);
      if (running) throw new ApiError(409, { detail: `이미 ${asOf} 목록을 뽑는 중입니다.`, session_id: running.session.id });
      const built = makeSession(asOf, false);
      // 응답은 만들기를 시작한 세션(종목 없음)이다. 종목은 simulateBuild 가 끝낼 때 채운다
      const s = { ...built, items: [], session: { ...built.session, total: 0, build: { state: 'building', stage: 'prices', done: 0, total: 0, error: '' } } };
      sessions.unshift(s);
      simulateBuild(s, built.items);
      return delay(() => s.session);
    },
    async getSession(id) {
      const s = find(id);
      if (!s) throw new ApiError(404, { detail: '세션을 찾지 못했습니다.' });
      return delay(() => view(s));
    },
    async process(id, offset, count) {
      const s = find(id);
      if (s.session.build.state !== 'ready') throw new ApiError(409, { detail: '목록을 만드는 중입니다. 끝난 뒤에 처리할 수 있습니다.' });
      if (s.processing) throw new ApiError(409, { detail: '이미 처리 중입니다.' });
      const targets = s.items.slice(offset, offset + count).filter((it) => it.state !== 'done');
      if (!targets.length) return delay(() => ({ started: false, queued: 0 }));
      s.processing = true;
      (async () => {
        for (const it of targets) {
          it.state = 'running';
          it.error_message = '';
          await new Promise((ok) => setTimeout(ok, config.mockMsPerItem));
          finish(it);
          if (s.ranked_at) s.rank_stale = true;
        }
        s.processing = false;
      })();
      return delay(() => ({ started: true, queued: targets.length }));
    },
    async organize(id) {
      const s = find(id);
      if (s.session.build.state !== 'ready') throw new ApiError(409, { detail: '목록을 만드는 중입니다. 끝난 뒤에 정리할 수 있습니다.', remaining: 0 });
      const c = counts(s.items);
      const remaining = c.pending + c.running;
      if (remaining > 0) throw new ApiError(409, { detail: `남은 ${remaining}개가 끝나야 정리할 수 있습니다.`, remaining });
      s.items.forEach((it) => { it.rank = null; });
      s.items.filter((it) => it.state === 'done')
        .sort((a, b) => (b.score - a.score) || (a.seq - b.seq))
        .forEach((it, k) => { it.rank = k + 1; });
      s.ranked_at = new Date().toISOString();
      s.rank_stale = false;
      return delay(() => ({ ranked: c.done, unscored: 0 }));
    },
  };
}
