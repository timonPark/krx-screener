import type { Item } from './types.js';

// 서버(TradingAgents-KR)는 숫자 6자리 종목코드만 받는다. 백엔드의 `analysis.server_accepts` 와 같은 규칙이다
// (`tests/test_frontend_contract.py` 가 같은 코드들로 둘을 대조한다).
export const serverAccepts = (code: string): boolean => /^\d{6}$/.test(code);

// "실패한 종목 다시 처리" 가 보낼 종목과 뺄 종목 (#77). 영숫자 코드는 다시 보내도 같은 사유로 즉시 실패하므로 뺀다.
export interface RetryPlan {
  retry: Item[];
  excluded: Item[];
}

export function retryPlan(items: Item[]): RetryPlan {
  const failed: Item[] = items.filter((it: Item): boolean => it.state === 'failed');
  return {
    retry: failed.filter((it: Item): boolean => serverAccepts(it.code)),
    excluded: failed.filter((it: Item): boolean => !serverAccepts(it.code)),
  };
}
