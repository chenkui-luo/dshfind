// 验证底部「重建索引」按钮：点一下 → 确认框 → 真的让后端开始全量重建。
// 只用 CDP，不依赖任何第三方库。
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const URL_APP = process.argv[2];
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9336;
const userDir = path.join(os.tmpdir(), 'dshrx-' + Date.now());

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
let dialogText = null;
ws.onmessage = async (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); return; }
  if (msg.method === 'Page.javascriptDialogOpening') {
    dialogText = msg.params.message;
    ws.send(JSON.stringify({
      id: ++id, method: 'Page.handleJavaScriptDialog', params: { accept: true },
    }));
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

await send('Page.enable');
await send('Runtime.enable');

for (let i = 0; i < 160; i++) {
  const ok = await ev(`(document.getElementById('stat')||{}).textContent||''`);
  if (typeof ok === 'string' && ok.includes('条')) break;
  await sleep(500);
}
await sleep(600);

const before = await ev(`(() => {
  const b = document.getElementById('reindexBtn');
  const q = document.getElementById('quitBtn');
  const br = b.getBoundingClientRect(), qr = q.getBoundingClientRect();
  return {
    exists: !!b, text: b.textContent.trim(), disabled: b.disabled,
    btnRect: { x: Math.round(br.x), y: Math.round(br.y), w: Math.round(br.width) },
    quitRect: { x: Math.round(qr.x), y: Math.round(qr.y) },
    footH: document.querySelector('.foot').offsetHeight,
  };
})()`);
console.log('按钮状态：', JSON.stringify(before));

await ev(`document.getElementById('reindexBtn').click(), 'clicked'`);
await sleep(1200);
console.log('确认框内容：', dialogText ? dialogText.slice(0, 60).replace(/\n/g, ' ') + '…' : '（没弹）');

let refreshing = false, foot = '', btnTxt = '';
for (let i = 0; i < 40; i++) {
  await sleep(500);
  const st = await ev(`(async () => {
    const r = await fetch('/api/status?t=' + new URLSearchParams(location.search).get('t'));
    const j = await r.json();
    return { refreshing: !!j.refreshing, phase: j.refresh_phase || '' };
  })()`);
  foot = await ev(`document.getElementById('footStat').textContent.trim()`);
  btnTxt = await ev(`document.getElementById('reindexBtn').textContent.trim()`);
  if (st && st.refreshing) { refreshing = true; break; }
}
console.log('后端是否在重建：', refreshing ? '是 ✅' : '否 ❌');
console.log('状态栏：', foot.slice(0, 70));
console.log('按钮文字：', btnTxt);
console.log('结果：', refreshing ? '按钮真的能触发重建 ✅' : '没触发 ❌');

try { await send('Browser.close'); } catch (_) {}
chrome.kill();
ws.close();
try { fs.rmSync(userDir, { recursive: true, force: true }); } catch (_) {}
process.exit(refreshing ? 0 : 2);
