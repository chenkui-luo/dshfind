// 像人一样逐字敲（真实按键事件），观察"切过对话模式之后，文件模式还出不出结果"。
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const URL_APP = process.argv[2];
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9334;
const userDir = path.join(os.tmpdir(), 'dshqa2-' + Date.now());
const chrome = spawn(CHROME, [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-breakpad',
  '--disable-crash-reporter', '--disable-extensions', '--hide-scrollbars',
  '--force-device-scale-factor=1', '--window-size=1280,900',
  '--remote-debugging-port=' + PORT, '--user-data-dir=' + userDir, URL_APP,
], { stdio: 'ignore' });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pageTarget() {
  for (let i = 0; i < 80; i++) {
    try {
      const r = await fetch('http://127.0.0.1:' + PORT + '/json/list');
      const p = (await r.json()).find((t) => t.type === 'page' && t.url.startsWith('http'));
      if (p) return p;
    } catch (_) {}
    await sleep(250);
  }
  throw new Error('Chrome 没起来');
}
const page = await pageTarget();
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0;
const pending = new Map();
const logs = [];
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === 'Runtime.exceptionThrown') {
    logs.push('[异常] ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
  }
  if (m.method === 'Runtime.consoleAPICalled') {
    logs.push('[console] ' + m.params.args.map((a) => a.value ?? a.description).join(' '));
  }
};
const send = (method, params = {}) => {
  const myId = ++id;
  ws.send(JSON.stringify({ id: myId, method, params }));
  return new Promise((res) => pending.set(myId, res));
};
const ev = async (expression) => {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  return r.result?.result?.value;
};
await send('Runtime.enable');

const snap = () => ev(`(() => {
  const t = (el) => el ? (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 90) : null;
  const res = performance.getEntriesByType('resource')
    .filter(e => e.name.includes('/api/'))
    .slice(-4)
    .map(e => {
      const u = new URL(e.name);
      return u.pathname.replace('/api/','') + ':' + (u.searchParams.get('q')||'') +
             ' d=' + Math.round(e.duration) + ' xfer=' + (e.transferSize ?? -1) +
             ' rs=' + (e.responseStatus ?? '?');
    });
  return {
    rows: document.querySelectorAll('#listInner .row').length,
    first: t(document.querySelector('#listInner .row-name')),
    empty: !document.getElementById('empty').hidden ? t(document.querySelector('.empty-title')) : null,
    foot: t(document.getElementById('footStat')),
    q: document.getElementById('q').value,
    mode: (document.querySelector('.mode.is-on') || {}).dataset?.mode,
    listH: document.getElementById('list').clientHeight,
    inputs: window.__inputs,
    f: (window.__f || []).slice(-4),
    res,
  };
})()`);

// 真实按键：一个字符一个字符打
async function typeText(text, gap = 90) {
  for (const ch of text) {
    await send('Input.dispatchKeyEvent', { type: 'keyDown', text: ch, key: ch });
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key: ch });
    await sleep(gap);
  }
}
async function clearInput() {
  await ev(`(() => { const q = document.getElementById('q'); q.focus(); q.value = ''; q.dispatchEvent(new Event('input', {bubbles:true})); })()`);
}
async function clickMode(mode) {
  await ev(`[...document.querySelectorAll('.mode')].find(b => b.dataset.mode === ${JSON.stringify(mode)}).click()`);
}
const timeline = [];
async function watch(label, times = [400, 1200, 2600, 6000]) {
  let last = 0;
  for (const t of times) {
    await sleep(t - last); last = t;
    timeline.push({ label, at: t, ...(await snap()) });
  }
}

for (let i = 0; i < 120; i++) {
  const st = await ev(`(document.getElementById('stat')||{}).textContent||''`);
  if (typeof st === 'string' && st.includes('条')) break;
  await sleep(500);
}
await sleep(700);
await ev(`(() => {
  window.__inputs = 0;
  window.__f = [];
  document.getElementById('q').addEventListener('input', () => { window.__inputs++; });
  const of = window.fetch;
  window.fetch = function (u, o) {
    const rec = { u: String(u).slice(0, 70), t: Date.now() };
    window.__f.push(rec);
    const p = of.apply(this, arguments);
    p.then(() => { rec.done = 'ok'; }, (e) => { rec.done = 'err:' + e.name; });
    return p;
  };
  return 'probe ok';
})()`);
timeline.push({ label: '0 就绪', at: 0, ...(await snap()) });

await typeText('readme');
await watch('A 文件模式打 readme');

await clickMode('session');
await watch('B 切到对话', [600]);
await typeText('检索', 140);
await watch('C 对话里打 检索', [800]);

await clickMode('file');
await watch('D 切回文件', [800]);
await typeText('doc');
await watch('E 文件里打 doc');
await clearInput();
await typeText('pdf');
await watch('F 清空后打 pdf');

console.log(JSON.stringify({ timeline, logs }, null, 1));
try { await send('Browser.close'); } catch (_) {}
chrome.kill();
ws.close();
try { fs.rmSync(userDir, { recursive: true, force: true }); } catch (_) {}
