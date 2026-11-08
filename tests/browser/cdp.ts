// 최소 CDP 클라이언트 (#58): 헤드리스 Chrome 을 띄우고 화면을 조작·관찰한다. 외부 패키지 없음 (Node 의 WebSocket·fetch).
// Node 가 타입을 지우고 바로 실행한다 (22.18 이상): 지울 수 있는 문법만 쓴다 (enum·매개변수 속성 없음, import 는 .ts 확장자).
import { spawn } from 'node:child_process';
import type { ChildProcess } from 'node:child_process';
import { existsSync } from 'node:fs';

export const sleep = (ms: number): Promise<void> => new Promise<void>((resolve: () => void): void => { setTimeout(resolve, ms); });

const MAC: string = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
export const chromePath = (): string | null => process.env.KRX_SCREENER_CHROME || (existsSync(MAC) ? MAC : null);

export async function launchChrome(port: number, profileDir: string): Promise<ChildProcess> {
  const path: string | null = chromePath();
  if (!path) throw new Error('Chrome 을 찾지 못했다 (KRX_SCREENER_CHROME 으로 알려 준다)');
  const proc: ChildProcess = spawn(path, [
    '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profileDir}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu', '--window-size=1500,1250', 'about:blank',
  ], { stdio: 'ignore' });
  for (let i: number = 0; i < 80; i++) {
    try {
      const r: Response = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (r.ok) return proc;
    } catch { /* 아직 안 떴다 */ }
    await sleep(250);
  }
  proc.kill();
  throw new Error('Chrome 이 시작되지 않았다');
}

// CDP 가 보내는 메시지: 요청의 응답(id)이거나 이벤트(method)다
interface CdpMessage {
  id?: number;
  result?: unknown;
  error?: { message: string };
  method?: string;
  params?: Record<string, unknown>;
}

interface Pending {
  ok: (value: unknown) => void;
  bad: (error: Error) => void;
}

interface RemoteObject {
  value?: unknown;
  description?: string;
}

interface EvaluateResult {
  result: RemoteObject;
  exceptionDetails?: { text: string; exception?: { description?: string } };
}

interface BrowserTarget {
  type: string;
  webSocketDebuggerUrl: string;
}

export class Page {
  private readonly ws: WebSocket;
  private id: number;
  private readonly pending: Map<number, Pending>;
  private readonly events: CdpMessage[];
  // 화면이 띄운 확인창(confirm)의 문구. 창은 바로 닫는다: acceptDialogs 가 true 면 확인, false 면 취소
  readonly dialogs: string[] = [];
  acceptDialogs: boolean = true;

  constructor(ws: WebSocket) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map<number, Pending>();
    this.events = [];
  }

  static async open(port: number): Promise<Page> {
    const list: BrowserTarget[] = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()) as BrowserTarget[];
    const target: BrowserTarget | undefined = list.find((t: BrowserTarget): boolean => t.type === 'page');
    if (!target) throw new Error('열린 페이지가 없다');
    const ws: WebSocket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise<void>((ok: () => void, bad: (reason: unknown) => void): void => { ws.onopen = (): void => ok(); ws.onerror = bad; });
    const page: Page = new Page(ws);
    ws.onmessage = (m: MessageEvent): void => {
      const msg: CdpMessage = JSON.parse(String(m.data)) as CdpMessage;
      const waiting: Pending | undefined = msg.id === undefined ? undefined : page.pending.get(msg.id);
      if (msg.id !== undefined && waiting) {
        page.pending.delete(msg.id);
        if (msg.error) waiting.bad(new Error(msg.error.message)); else waiting.ok(msg.result);
      } else if (msg.method === 'Page.javascriptDialogOpening') {
        page.dialogs.push(String(msg.params?.message ?? ''));
        void page.send('Page.handleJavaScriptDialog', { accept: page.acceptDialogs });
      } else if (msg.method) page.events.push(msg);
    };
    await page.send('Page.enable');
    await page.send('Runtime.enable');
    await page.send('Emulation.setDeviceMetricsOverride', { width: 1500, height: 1250, deviceScaleFactor: 1, mobile: false });
    return page;
  }

  send(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    this.id += 1;
    const id: number = this.id;
    return new Promise<unknown>((ok: (value: unknown) => void, bad: (error: Error) => void): void => {
      this.pending.set(id, { ok, bad });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  // 화면에서 식을 실행해 값을 받는다. T 는 호출하는 쪽이 아는 값의 모양이다.
  async eval<T>(expression: string): Promise<T> {
    const r: EvaluateResult = (await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })) as EvaluateResult;
    if (r.exceptionDetails) throw new Error('페이지 오류: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result.value as T;
  }

  async goto(url: string): Promise<void> {
    await this.send('Page.navigate', { url });
    await this.waitFor(`document.readyState === 'complete' && document.getElementById('poll-text')?.textContent !== '불러오는 중…'`, 20000);
  }

  async waitFor(expr: string, ms: number = 15000, step: number = 50): Promise<void> {
    const t0: number = Date.now();
    for (;;) {
      let v: unknown = false;
      try { v = await this.eval<unknown>(expr); } catch { /* 페이지가 다시 불러오는 중일 수 있다 */ }
      if (v) return;
      if (Date.now() - t0 > ms) throw new Error(`시간 초과(${ms}ms): ${expr.slice(0, 140)}`);
      await sleep(step);
    }
  }

  // 화면에 나타난 콘솔 오류 (잡히지 않은 예외와 console.error). 화면 규칙을 어긴 호출이 있으면 여기 남는다.
  consoleErrors(): string[] {
    return this.events
      .filter((e: CdpMessage): boolean => e.method === 'Runtime.exceptionThrown' || (e.method === 'Runtime.consoleAPICalled' && e.params?.type === 'error'))
      .map((e: CdpMessage): string => {
        if (e.method === 'Runtime.exceptionThrown') {
          const details: EvaluateResult['exceptionDetails'] = (e.params as { exceptionDetails: EvaluateResult['exceptionDetails'] }).exceptionDetails;
          return details?.exception?.description || details?.text || '';
        }
        const args: RemoteObject[] = (e.params as { args: RemoteObject[] }).args;
        return args.map((a: RemoteObject): string => String(a.value ?? a.description)).join(' ');
      });
  }

  close(): void { try { this.ws.close(); } catch { /* 이미 닫힘 */ } }
}
