// 백엔드 API 호출. 경로와 응답 모양은 frontend/README.md 의 "API 계약" 을 따른다.
import { config } from './config.js';
import { createMockApi } from './mock.js';
import type { Api, ApiErrorBody, OrganizeResult, ProcessResult, Session, SessionDetail } from './types.js';

export class ApiError extends Error {
  readonly status: number;
  readonly body: ApiErrorBody;

  constructor(status: number, body: ApiErrorBody | null) {
    super((body && body.detail) || `요청 실패 (HTTP ${status})`);
    this.status = status;
    this.body = body || {};
  }
}

async function request<T>(method: string, path: string, payload?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(config.apiBase + path, {
      method,
      headers: payload ? { 'Content-Type': 'application/json' } : undefined,
      body: payload ? JSON.stringify(payload) : undefined,
    });
  } catch {
    throw new ApiError(0, { detail: '서버에 연결하지 못했습니다. 백엔드가 켜져 있는지 확인하세요.' });
  }
  const text: string = await res.text();
  let body: unknown = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = { detail: text }; }
  if (!res.ok) throw new ApiError(res.status, body as ApiErrorBody | null);
  return body as T;
}

const sid = (id: string | null): string => encodeURIComponent(id || 'latest');

const httpApi: Api = {
  // 1. 목록 뽑기 → 새 세션
  createSession: (asOf: string): Promise<Session> => request<Session>('POST', '/api/sessions', { as_of: asOf }),
  // 세션 목록 (최근 순)
  listSessions: (): Promise<Session[]> => request<Session[]>('GET', '/api/sessions'),
  // 3. 조회: 세션 정보 + 개수 + 전체 종목
  getSession: (id: string | null): Promise<SessionDetail> => request<SessionDetail>('GET', `/api/sessions/${sid(id)}`),
  // 2. 나눠서 처리 (202: 시작, 409: 이미 처리 중)
  process: (id: string | null, offset: number, count: number): Promise<ProcessResult> =>
    request<ProcessResult>('POST', `/api/sessions/${sid(id)}/process`, { offset, count }),
  // 2'. 실패한 종목 다시 처리: 세션 전체의 실패한 종목(숫자 6자리 코드만). 구간 처리는 실패한 종목을 보내지 않는다 (#77)
  processFailed: (id: string | null): Promise<ProcessResult> =>
    request<ProcessResult>('POST', `/api/sessions/${sid(id)}/process-failed`),
  // 4. 정리 (409: 남은 종목 있음, body.remaining)
  organize: (id: string | null): Promise<OrganizeResult> => request<OrganizeResult>('POST', `/api/sessions/${sid(id)}/organize`),
  // 4'. 정리 요약 보고서(마크다운 첨부 파일). 정리하기 전에는 409
  reportHref: (id: string | null): string => `${config.apiBase}/api/sessions/${sid(id)}/report`,
};

export const api: Api = config.mock ? createMockApi(ApiError) : httpApi;
