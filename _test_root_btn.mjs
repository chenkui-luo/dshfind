// 端到端验证「只重建 F 盘」按钮：点一下 → 确认框 → 后端只重扫 F → 完成。
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const URL_APP = process.argv[2];
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9339;
const userDir = path.join(os.tmpdir(), 'dshroot-' + Date.now());

const chrome = spawn(CHROME, [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-breakpad',
  '--disable-crash-reporter', '--disable-extensions', '--hide-scrollbars',
  '--force-device-scale-factor=1', '--window-size=1280,900',
  '--remote-debugging-port=' + PORT, '--user-data-dir=' + userDir, URL_APP,
], { stdio: 'ignore' });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let page = null;
for (let i = 0; i < 80; i++) {
  try {
    const r = await fetch('http://127.0.0.1:' + PORT + '/json/list');
    page = (await r.json()).find((t) => t.type === 'page' && t.url.startsWith('http'));
    if (page) break;
  } catch (_) {}
  await sleep(250);
}
if (!page) throw new Error('Chrome 没起来');

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0;
const pending = new Map();
let dialog = null;
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); return; }
  if (msg.method === 'Page.javascriptDialogOpening') {
    dialog = msg.params.message;
    ws.send(JSON.stringify({ id: ++id, method: 'Page.handleJavaScriptDialog',
                             params: { accept: true } }));
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
const status = () => ev(`(async () => {
  const t = new URLSearchParams(location.search).get('t');
  const r = await fetch('/api/status?t=' + t);
  const j = await r.json();
  return { ready: j.ready, refreshing: j.refreshing, phase: j.refresh_phase,
           detail: j.refresh_detail, root: j.refresh_root, entries: j.entries,
           quick: j.quick_root };
})()`);

await send('Runtime.enable');
await send('Page.enable');

for (let i = 0; i < 200; i++) {
  const st = await status();
  if (st && st.ready) break;
  await sleep(500);
}
await sleep(500);

const before = await status();
const btn = await ev(`(() => {
  const b = document.getElementById('rebuildRootBtn');
  return b ? { hidden: b.hidden, text: b.textContent.trim(), disabled: b.disabled } : null;
})()`);
console.log('状态：', JSON.stringify(before));
console.log('按钮：', JSON.stringify(btn));

await ev(`document.getElementById('rebuildRootBtn').click(), 'clicked'`);
await sleep(800);
console.log('确认框：', dialog ? dialog.split('\n')[0] : '（没弹）');

let started = null;
for (let i = 0; i < 40; i++) {
  const st = await status();
  if (st.refreshing) { started = st; break; }
  await sleep(500);
}
console.log('已开始：', JSON.stringify(started));
console.log('状态栏：', await ev(`document.getElementById('footStat').textContent.trim()`));

let done = null;
for (let i = 0; i < 400; i++) {
  await sleep(1500);
  const st = await status();
  if (!st.refreshing) { done = st; break; }
}
console.log('结束：', JSON.stringify(done));
console.log('按钮文字：', await ev(`document.getElementById('rebuildRootBtn').textContent.trim()`));
console.log('状态栏：', await ev(`document.getElementById('footStat').textContent.trim()`));

const ok = btn && !btn.hidden && btn.text.includes('F')
  && started && started.refreshing && (started.root || '').toUpperCase().startsWith('F')
  && done && done.entries > 0;

try { await send('Browser.close'); } catch (_) {}
chrome.kill();
ws.close();
try { fs.rmSync(userDir, { recursive: true, force: true }); } catch (_) {}
console.log('结果：', ok ? '只重建 F 盘 走通了 ✅' : '没走通 ❌');
process.exit(ok ? 0 : 2);
