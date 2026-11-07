// 최소 CDP 클라이언트 (#58): 헤드리스 Chrome 을 띄우고 화면을 조작·관찰한다. 외부 패키지 없음 (Node 22 이상의 WebSocket·fetch).
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const MAC = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
export const chromePath = () => process.env.KRX_SCREENER_CHROME || (existsSync(MAC) ? MAC : null);

export async function launchChrome(port, profileDir) {
  const proc = spawn(chromePath(), [
    '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profileDir}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu', '--window-size=1500,1250', 'about:blank',
  ], { stdio: 'ignore' });
  for (let i = 0; i < 80; i++) {
    try { const r = await fetch(`http://127.0.0.1:${port}/json/version`); if (r.ok) return proc; } catch { /* 아직 안 떴다 */ }
    await sleep(250);
  }
  proc.kill();
  throw new Error('Chrome 이 시작되지 않았다');
}

export class Page {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.events = []; }

  static async open(port) {
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const ws = new WebSocket(list.find((t) => t.type === 'page').webSocketDebuggerUrl);
    await new Promise((ok, bad) => { ws.onopen = ok; ws.onerror = bad; });
    const page = new Page(ws);
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.id && page.pending.has(msg.id)) {
        const { ok, bad } = page.pending.get(msg.id);
        page.pending.delete(msg.id);
        if (msg.error) bad(new Error(msg.error.message)); else ok(msg.result);
      } else if (msg.method) page.events.push(msg);
    };
    await page.send('Page.enable');
    await page.send('Runtime.enable');
    await page.send('Emulation.setDeviceMetricsOverride', { width: 1500, height: 1250, deviceScaleFactor: 1, mobile: false });
    return page;
  }

  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((ok, bad) => { this.pending.set(id, { ok, bad }); this.ws.send(JSON.stringify({ id, method, params })); });
  }

  async eval(expression) {
    const r = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error('페이지 오류: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result.value;
  }

  async goto(url) {
    await this.send('Page.navigate', { url });
    await this.waitFor(`document.readyState === 'complete' && document.getElementById('poll-text')?.textContent !== '불러오는 중…'`, 20000);
  }

  async waitFor(expr, ms = 15000, step = 50) {
    const t0 = Date.now();
    for (;;) {
      let v = false;
      try { v = await this.eval(expr); } catch { /* 페이지가 다시 불러오는 중일 수 있다 */ }
      if (v) return v;
      if (Date.now() - t0 > ms) throw new Error(`시간 초과(${ms}ms): ${expr.slice(0, 140)}`);
      await sleep(step);
    }
  }

  // 화면에 나타난 콘솔 오류 (잡히지 않은 예외와 console.error). 화면 규칙을 어긴 호출이 있으면 여기 남는다.
  consoleErrors() {
    return this.events
      .filter((e) => e.method === 'Runtime.exceptionThrown' || (e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error'))
      .map((e) => (e.method === 'Runtime.exceptionThrown'
        ? e.params.exceptionDetails.exception?.description || e.params.exceptionDetails.text
        : e.params.args.map((a) => a.value ?? a.description).join(' ')));
  }

  close() { try { this.ws.close(); } catch { /* 이미 닫힘 */ } }
}
