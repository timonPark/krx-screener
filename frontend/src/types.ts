// 백엔드 응답의 모양 (src/krx_screener/routes_sessions.py 의 응답 모델과 같다).
// 키가 어긋나지 않게 tests/test_frontend_types.py 가 이 인터페이스의 키와 백엔드 모델의 필드를 대조한다.
// 화면 계약 설명: frontend/README.md "API 계약".

// 목록을 만드는 중의 상태 (#56): building 만드는 중 · ready 완성 · failed 실패 · interrupted 만들던 서버가 죽음
export type BuildState = 'building' | 'ready' | 'failed' | 'interrupted';
export type RunningStage = 'prices' | 'flow' | 'sector';
export type BuildStage = RunningStage | '';

export interface Build {
  state: BuildState;
  stage: BuildStage;
  done: number; // 그 단계에서 끝난 개수
  total: number; // 그 단계의 전체 개수 (시세 단계 처음에는 아직 모르면 0)
  error: string; // failed 일 때 한국어 사유
}

// 목록을 뽑을 때 쓴 조건 (#57)
export interface Filters {
  min_avg_trading_value_krw: number;
  avg_trading_value_window: number;
  price_above_ma: number[];
  rsi_max: number | null; // null 이면 RSI 조건 없음
}

export interface FlowCriteria {
  mode: string; // mark(표시만) | filter(약함 탈락)
  window_days: number;
}

export interface Criteria {
  filters: Filters;
  exclude: string[]; // 제외한 종류 (preferred, spac, managed, zero_volume, new_listing, reit, caution, foreign)
  flow: FlowCriteria;
}

export interface Session {
  id: string;
  as_of: string; // 2026-11-05
  created_at: string; // UTC, 예: 2026-11-06T00:12:00Z
  total: number;
  build: Build;
  criteria: Criteria | null; // null 이면 조건 기록 없음 (기존 세션, 명령줄 세션)
}

export type ItemState = 'pending' | 'running' | 'done' | 'failed';

export interface Item {
  seq: number;
  code: string;
  name: string;
  market: string;
  sector_code: string;
  sector_name: string;
  description: string;
  is_caution: boolean;
  is_foreign: boolean;
  close: number | null;
  avg_trading_value: number | null;
  rsi: number | null;
  flow_status: string; // 양호 | 부분 | 약함 | 미확인
  foreign_net_5d: number | null;
  institution_net_5d: number | null;
  state: ItemState;
  analyzed_at: string | null;
  error_message: string;
  report_id: string;
  decision_label: string; // 매수 | 비중 확대 | 보유 | 비중 축소 | 매도 | REVIEW | 빈 문자열
  score: number | null;
  agents_rated: number;
  agents_total: number;
  rank: number | null;
}

export interface Counts {
  pending: number;
  running: number;
  done: number;
  failed: number;
}

export interface SessionDetail {
  session: Session;
  processing: boolean; // 이 세션을 처리 중인지(잠금). true 면 화면이 주기적으로 다시 부른다
  ranked_at: string | null; // 정리 실행 시각, 안 했으면 null
  rank_stale: boolean; // 정리 이후 재분석된 종목이 있으면 true
  counts: Counts;
  items: Item[];
}

export interface ProcessResult {
  started: boolean;
  queued: number;
}

export interface OrganizeResult {
  ranked: number;
  unscored: number;
}

// 오류 응답 본문. detail 은 한국어 메시지이고 화면이 그대로 띄운다.
export interface ApiErrorBody {
  detail?: string;
  remaining?: number; // 정리: 아직 끝나지 않은 종목 수
  session_id?: string | null; // 새 목록: 이미 만드는 중인 세션
}

// 화면이 부르는 API. 진짜(api.ts 의 httpApi)와 예시(mock.ts)가 같은 모양이다.
export interface Api {
  createSession(asOf: string): Promise<Session>;
  listSessions(): Promise<Session[]>;
  getSession(id: string | null): Promise<SessionDetail>;
  process(id: string | null, offset: number, count: number): Promise<ProcessResult>;
  processFailed(id: string | null): Promise<ProcessResult>;
  organize(id: string | null): Promise<OrganizeResult>;
}
