// 세션이 목록을 뽑을 때 쓴 조건(백엔드 session.criteria)을 화면 문구로 바꾼다 (#57).
// 백엔드는 값 그대로를 주고(영어 키), 한국어 문구는 여기서만 만든다. criteria 가 null 이면 조건 기록 없음이다.

const EXCLUDE_LABEL = {
  preferred: '우선주', spac: '스팩', managed: '관리종목', zero_volume: '거래정지 의심',
  new_listing: '신규상장', reit: '리츠', caution: '투자주의환기', foreign: '외국기업',
};
const FLOW_MODE_LABEL = { mark: '표시만', filter: '약함 탈락' };

export const NO_RECORD = '조건 기록 없음';

// 원 → "10억" · "1.5억" · "5천만" (억 미만은 만 단위)
export function fmtWon(n) {
  if (n >= 1e8) return `${Number((n / 1e8).toFixed(2)).toLocaleString('ko-KR')}억`;
  if (n >= 1e4) return `${Number((n / 1e4).toFixed(2)).toLocaleString('ko-KR')}만`;
  return `${n.toLocaleString('ko-KR')}`;
}

// { filter: "20일 평균 거래대금 10억↑ · 종가 > 20일선 · 종가 > 60일선", extra: "제외: … · 수급: …" }
export function criteriaText(criteria) {
  if (!criteria) return { filter: NO_RECORD, extra: NO_RECORD };
  const { filters, exclude, flow } = criteria;
  const parts = [`${filters.avg_trading_value_window}일 평균 거래대금 ${fmtWon(filters.min_avg_trading_value_krw)}↑`];
  filters.price_above_ma.forEach((n) => parts.push(`종가 > ${n}일선`));
  if (filters.rsi_max != null) parts.push(`RSI < ${filters.rsi_max}`);
  const excluded = exclude.length ? exclude.map((k) => EXCLUDE_LABEL[k] || k).join('·') : '없음';
  const mode = FLOW_MODE_LABEL[flow.mode] || flow.mode;
  return { filter: parts.join(' · '), extra: `제외: ${excluded} · 수급: ${mode}, 최근 ${flow.window_days}거래일` };
}
