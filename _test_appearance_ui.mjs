// 界面层验证：点齿轮 → 打开面板 → 改名字 → 保存 → 窗口标题（document.title）真的变了。
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const URL_APP = process.argv[2];
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9337;
const userDir = path.join(os.tmpdir(), 'dshap-' + Date.now());

const chrome = spawn(CHROME, [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-breakpad',
  '--disable-crash-reporter', '--disable-extensions', '--hide-scrollbars',
  '--force-device-scale-factor=1', '--window-size=1280,900',
  '--remote-debugging-port=' + PORT, '--user-data-dir=' + userDir, URL_APP,
], { stdio: 'ignore' });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
for (let i = 0; i < 80; i++) {
  try {
    const r = await fetch('http://127.0.0.1:' + PORT + '/json/list');
    const p = (await r.json()).find((t) => t.type === 'page' && t.url.startsWith('http'));
    if (p) { globalThis.__page = p; break; }
  } catch (_) {}
  await sleep(250);
}
const ws = new WebSocket(globalThis.__page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0;
const pending = new Map();
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
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

for (let i = 0; i < 160; i++) {
  const t = await ev(`(document.getElementById('stat')||{}).textContent||''`);
  if (typeof t === 'string' && t.includes('条')) break;
  await sleep(500);
}
await sleep(500);

const before = await ev(`document.title`);
await ev(`document.getElementById('settingsBtn').click(), 'ok'`);
await sleep(400);
const opened = await ev(`!document.getElementById('appearanceMask').hidden`);

const NAME = '我的文件检索';
await ev(`(() => { const i = document.getElementById('apName'); i.value = ${JSON.stringify(NAME)}; return i.value; })()`);
await ev(`document.getElementById('apSave').click(), 'ok'`);
await sleep(1200);

const after = await ev(`document.title`);
const closed = await ev(`document.getElementById('appearanceMask').hidden`);
const iconHref = await ev(`document.getElementById('favicon').href`);
const persisted = await ev(`(async () => {
  const r = await fetch('/api/appearance?t=' + new URLSearchParams(location.search).get('t'));
  const j = await r.json();
  return j.title;
})()`);

console.log('打开面板：', opened ? '是 ✅' : '否 ❌');
console.log('标题：', JSON.stringify(before), '→', JSON.stringify(after));
console.log('面板保存后自动关闭：', closed ? '是 ✅' : '否');
console.log('favicon href：', iconHref);
console.log('服务端持久化：', JSON.stringify(persisted));

const ok = opened && after === NAME && persisted === NAME;
console.log('结果：', ok ? '改名字真的生效 ✅' : '没生效 ❌');

// 还原成默认名字
await ev(`(async () => {
  const t = new URLSearchParams(location.search).get('t');
  await fetch('/api/appearance?t=' + t, { method: 'POST',
    headers: { 'X-Dsh-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: '' }) });
})()`);

try { await send('Browser.close'); } catch (_) {}
chrome.kill();
ws.close();
try { fs.rmSync(userDir, { recursive: true, force: true }); } catch (_) {}
process.exit(ok ? 0 : 2);
