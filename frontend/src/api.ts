// 백엔드 API 호출. 경로와 응답 모양은 frontend/README.md 의 "API 계약" 을 따른다.
import { config } from './config.js';
import { createMockApi } from './mock.js';

export class ApiError extends Error {
  constructor(status, body) {
    super((body && body.detail) || `요청 실패 (HTTP ${status})`);
    this.status = status;
    this.body = body || {};
  }
}

async function request(method, path, payload) {
  let res;
  try {
    res = await fetch(config.apiBase + path, {
      method,
      headers: payload ? { 'Content-Type': 'application/json' } : undefined,
      body: payload ? JSON.stringify(payload) : undefined,
    });
  } catch {
    throw new ApiError(0, { detail: '서버에 연결하지 못했습니다. 백엔드가 켜져 있는지 확인하세요.' });
  }
  const text = await res.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = { detail: text }; }
  if (!res.ok) throw new ApiError(res.status, body);
  return body;
}

const sid = (id) => encodeURIComponent(id || 'latest');

const httpApi = {
  // 1. 목록 뽑기 → 새 세션
  createSession: (asOf) => request('POST', '/api/sessions', { as_of: asOf }),
  // 세션 목록 (최근 순)
  listSessions: () => request('GET', '/api/sessions'),
  // 3. 조회: 세션 정보 + 개수 + 전체 종목
  getSession: (id) => request('GET', `/api/sessions/${sid(id)}`),
  // 2. 나눠서 처리 (202: 시작, 409: 이미 처리 중)
  process: (id, offset, count) => request('POST', `/api/sessions/${sid(id)}/process`, { offset, count }),
  // 4. 정리 (409: 남은 종목 있음, body.remaining)
  organize: (id) => request('POST', `/api/sessions/${sid(id)}/organize`),
};

export const api = config.mock ? createMockApi(ApiError) : httpApi;
