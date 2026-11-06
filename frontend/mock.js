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

const RUN_IDS = ['610aaf9bfe1b', '295daa56e541', 'd68b8eb44c9c'];
const randId = () => Math.random().toString(16).slice(2, 14).padEnd(12, '0');
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : `${randId()}-mock`).replace(/-/g, '');

function makeSession(asOf, seeded) {
  const id = seeded ? '3f2a9c1b7d4e4a0f9b21c6e8d5a03f17' : uuid();
  const items = ROWS.map((r, i) => {
    const item = {
      seq: i + 1, code: r[0], name: r[1], market: r[2], close: r[3], avg_trading_value: r[4], rsi: r[5],
      flow_status: r[6], foreign_net_5d: r[7], institution_net_5d: r[8],
      sector_code: '', sector_name: r[9], description: r[10],
      is_caution: false, is_foreign: false,
      state: 'pending', analyzed_at: null, error_message: '', run_id: '',
      decision_label: '', score: null, agents_rated: 0, agents_total: 10, rank: null,
      _result: r[11],
    };
    if (seeded && i < 3) finish(item, RUN_IDS[i]);
    if (seeded && i === 3) { item.state = 'failed'; item.error_message = '시간 초과 (900초)'; item.analyzed_at = new Date().toISOString(); }
    return item;
  });
  return {
    session: { id, as_of: asOf, created_at: new Date().toISOString(), total: items.length },
    items, processing: false, ranked_at: null, rank_stale: false,
  };
}

function finish(item, runId) {
  const res = item._result || ['보유', 0, 10];
  item.state = 'done';
  item.analyzed_at = new Date().toISOString();
  item.run_id = runId || randId();
  item.decision_label = res[0];
  item.score = res[1];
  item.agents_rated = res[2];
  item.error_message = '';
}

function counts(items) {
  const c = { pending: 0, running: 0, done: 0, failed: 0 };
  items.forEach((it) => { c[it.state] += 1; });
  return c;
}

export function createMockApi(ApiError) {
  const sessions = [makeSession('2026-11-05', true)];
  const find = (id) => (!id || id === 'latest' ? sessions[0] : sessions.find((s) => s.session.id === id));
  const delay = (v) => new Promise((ok) => setTimeout(() => ok(structuredClone(v)), 150));
  const view = (s) => {
    const items = s.items.map(({ _result, ...rest }) => rest);
    return { session: s.session, processing: s.processing, ranked_at: s.ranked_at, rank_stale: s.rank_stale, counts: counts(s.items), items };
  };

  return {
    async listSessions() { return delay(sessions.map((s) => s.session)); },
    async createSession(asOf) {
      const s = makeSession(asOf, false);
      sessions.unshift(s);
      return delay(s.session);
    },
    async getSession(id) {
      const s = find(id);
      if (!s) throw new ApiError(404, { detail: '세션을 찾지 못했습니다.' });
      return delay(view(s));
    },
    async process(id, offset, count) {
      const s = find(id);
      if (s.processing) throw new ApiError(409, { detail: '이미 처리 중입니다.' });
      const targets = s.items.slice(offset, offset + count).filter((it) => it.state !== 'done');
      if (!targets.length) return delay({ started: false, queued: 0 });
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
      return delay({ started: true, queued: targets.length });
    },
    async organize(id) {
      const s = find(id);
      const c = counts(s.items);
      const remaining = c.pending + c.running;
      if (remaining > 0) throw new ApiError(409, { detail: `남은 종목 ${remaining}개가 끝나야 정리할 수 있습니다.`, remaining });
      s.items.forEach((it) => { it.rank = null; });
      s.items.filter((it) => it.state === 'done')
        .sort((a, b) => (b.score - a.score) || (a.seq - b.seq))
        .forEach((it, k) => { it.rank = k + 1; });
      s.ranked_at = new Date().toISOString();
      s.rank_stale = false;
      return delay({ ranked: c.done });
    },
  };
}
