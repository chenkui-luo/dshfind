// 端到端：把用户那张 hina2.png 真的选进去 → 保存 → 检查 /favicon.ico 是不是多尺寸 ICO。
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const URL_APP = process.argv[2];
const IMG = process.argv[3];
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9338;
const userDir = path.join(os.tmpdir(), 'dshic-' + Date.now());

const chrome = spawn(CHROME, [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-breakpad',
  '--disable-crash-reporter', '--disable-extensions', '--hide-scrollbars',
  '--force-device-scale-factor=1', '--window-size=1280,900',
  '--remote-debugging-port=' + PORT, '--user-data-dir=' + userDir,
  '--allow-file-access-from-files', URL_APP,
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
  if (r.result?.exceptionDetails) return { __err: r.result.exceptionDetails.exception?.description };
  return r.result?.result?.value;
};

await send('Runtime.enable');
await send('DOM.enable');
await send('Page.enable');

for (let i = 0; i < 160; i++) {
  const t = await ev(`(document.getElementById('stat')||{}).textContent||''`);
  if (typeof t === 'string' && t.includes('条')) break;
  await sleep(500);
}
await sleep(500);

// 打开面板
await ev(`document.getElementById('settingsBtn').click(), 'ok'`);
await sleep(400);

// 把真实文件塞进 <input type=file>
const doc = await send('DOM.getDocument', { depth: -1 });
const q = await send('DOM.querySelector', {
  nodeId: doc.result.root.nodeId, selector: '#apFile',
});
await send('DOM.setFileInputFiles', { nodeId: q.result.nodeId, files: [IMG] });
await sleep(2500);

const picked = await ev(`document.getElementById('apStat').textContent`);
const preview = await ev(`(() => {
  const img = document.querySelector('#apIconPreview img');
  return img ? { w: img.naturalWidth, h: img.naturalHeight } : null;
})()`);
console.log('选择后提示：', picked);
console.log('预览图尺寸：', JSON.stringify(preview));

await ev(`document.getElementById('apSave').click(), 'ok'`);
await sleep(2500);

const info = await ev(`(async () => {
  const t = new URLSearchParams(location.search).get('t');
  const r = await fetch('/api/appearance?t=' + t);
  return await r.json();
})()`);
const ico = await ev(`(async () => {
  const r = await fetch('/favicon.ico');
  const buf = new Uint8Array(await r.arrayBuffer());
  const n = buf[4] | (buf[5] << 8);
  const sizes = [];
  for (let i = 0; i < n; i++) {
    const b = 6 + i * 16;
    const w = buf[b];
    sizes.push(w === 0 ? 256 : w);
  }
  return { status: r.status, type: r.headers.get('content-type'), count: n, sizes,
           magic: Array.from(buf.slice(0, 4)) };
})()`);
const png = await ev(`(async () => {
  const r = await fetch('/appicon');
  const buf = new Uint8Array(await r.arrayBuffer());
  const dv = new DataView(buf.buffer);
  return { status: r.status, w: dv.getUint32(16), h: dv.getUint32(20), bytes: buf.length };
})()`);

console.log('服务端记录的图标：', JSON.stringify(info.icon));
console.log('/favicon.ico：', JSON.stringify(ico));
console.log('/appicon：', JSON.stringify(png));

const ok = ico.status === 200 && ico.magic.join(',') === '0,0,1,0' && ico.count >= 6
  && ico.sizes.includes(16) && ico.sizes.includes(32) && ico.sizes.includes(256)
  && png.w === 256 && png.h === 256;
console.log('结果：', ok ? '多尺寸 ICO 生成正确 ✅' : '不对 ❌');

try { await send('Browser.close'); } catch (_) {}
chrome.kill();
ws.close();
try { fs.rmSync(userDir, { recursive: true, force: true }); } catch (_) {}
process.exit(ok ? 0 : 2);
