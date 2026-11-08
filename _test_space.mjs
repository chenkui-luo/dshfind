// 验证：输入框里能正常打出空格；空格在输入框外依然是「预览开/关」快捷键。
// 用法：node _test_space.mjs "http://127.0.0.1:8793/?t=qatoken123"
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const URL_APP = process.argv[2];
if (!URL_APP) { console.error('需要传入带 token 的 URL'); process.exit(2); }
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9341;
const userDir = path.join(os.tmpdir(), 'dshspace-' + Date.now());
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
};
const send = (method, params = {}) => {
  const myId = ++id;
  ws.send(JSON.stringify({ id: myId, method, params }));
  return new Promise((res) => pending.set(myId, res));
};
const ev = async (expression) => {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text);
  return r.result?.result?.value;
};
await send('Runtime.enable');

const state = () => ev(`(() => ({
  q: document.getElementById('q').value,
  focused: document.activeElement && document.activeElement.id,
  pvCollapsed: document.getElementById('preview').classList.contains('is-collapsed'),
  rows: document.querySelectorAll('#listInner .row').length,
}))()`);

async function key(k, extra = {}) {
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: k, code: extra.code || k, text: extra.text, ...extra });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code: extra.code || k });
  await sleep(60);
}
async function typeText(text, gap = 70) {
  for (const ch of text) {
    const isSpace = ch === ' ';
    await send('Input.dispatchKeyEvent', {
      type: 'keyDown', text: ch, key: ch, code: isSpace ? 'Space' : undefined,
      windowsVirtualKeyCode: isSpace ? 32 : undefined,
      nativeVirtualKeyCode: isSpace ? 32 : undefined,
    });
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key: ch, code: isSpace ? 'Space' : undefined });
    await sleep(gap);
  }
}
async function clearInput() {
  await ev(`(() => { const q = document.getElementById('q'); q.focus(); q.value = '';
    q.dispatchEvent(new Event('input', {bubbles:true})); })()`);
  await sleep(120);
}

// 等服务就绪
for (let i = 0; i < 160; i++) {
  const st = await ev(`(document.getElementById('stat')||{}).textContent||''`);
  if (typeof st === 'string' && st.includes('条')) break;
  await sleep(500);
}
await sleep(500);

const steps = [];
const rec = async (label) => steps.push({ label, ...(await state()) });

await clearInput();
await rec('A0 就绪（输入框聚焦）');
const a0 = steps[steps.length - 1];

// 1) 真实敲键：空格必须能进输入框
await typeText('foo bar');
await sleep(400);
await rec('A1 打 "foo bar"');
const a1 = steps[steps.length - 1];

// 2) 多个空格 / 空格在开头结尾
await clearInput();
await typeText('  a b  ');
await sleep(300);
await rec('A2 打 "  a b  "');
const a2 = steps[steps.length - 1];

// 3) 中文输入法组字时的空格不要被抢（合成一条 isComposing 的 keydown）
await clearInput();
await ev(`(() => {
  const q = document.getElementById('q'); q.focus();
  document.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', isComposing: true, bubbles: true, cancelable: true }));
  return 'sent';
})()`);
await sleep(200);
await rec('A3 组字中空格（不应触发预览切换）');
const a3 = steps[steps.length - 1];

// 4) 输入框外，空格仍然切换预览
await ev(`(() => { document.getElementById('q').blur(); document.body.focus(); return 1; })()`);
await sleep(150);
await key(' ', { text: ' ', code: 'Space', windowsVirtualKeyCode: 32, nativeVirtualKeyCode: 32 });
await sleep(400);
await rec('B1 框外空格 → 收起预览');
const b1 = steps[steps.length - 1];

await key(' ', { text: ' ', code: 'Space', windowsVirtualKeyCode: 32, nativeVirtualKeyCode: 32 });
await sleep(400);
await rec('B2 再按一次 → 展开预览');
const b2 = steps[steps.length - 1];

// 5) 外观设置里的标题框也要能打空格
const ap = await ev(`(() => {
  const b = document.getElementById('settingsBtn');
  if (!b) return 'no-settings-btn';
  b.click();
  return 'opened';
})()`);
await sleep(600);
let apRes = null;
if (ap === 'opened') {
  apRes = await ev(`(() => {
    const t = document.getElementById('apName');
    if (!t) return { err: 'no #apName' };
    t.focus();
    const before = document.getElementById('preview').classList.contains('is-collapsed');
    t.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true }));
    return { before, after: document.getElementById('preview').classList.contains('is-collapsed') };
  })()`);
  await sleep(200);
  const focused = await ev(`document.activeElement === document.getElementById('apName')`);
  apRes.focused = focused;
}

const checks = {
  '空格能打出来 (foo bar)': a1.q === 'foo bar',
  '首尾/中间空格都保留 (  a b  )': a2.q === '  a b  ',
  '框外空格切换预览': b1.pvCollapsed === !a0.pvCollapsed,
  '再按一次切回去': b2.pvCollapsed === a0.pvCollapsed,
  '组字中空格不切换预览': a3.pvCollapsed === a0.pvCollapsed,
  '外观弹窗标题框能聚焦': apRes ? apRes.focused === true : 'skip',
  '外观弹窗里空格不切换预览': apRes ? apRes.before === apRes.after : 'skip',
};

let fail = 0;
for (const [k, v] of Object.entries(checks)) {
  const ok = v === true || v === 'skip';
  if (!ok) fail++;
  console.log((ok ? 'PASS  ' : 'FAIL  ') + k + (v === 'skip' ? '（跳过）' : ''));
}
console.log('\n--- 细节 ---');
console.log(JSON.stringify({ steps, apRes, logs }, null, 1));
try { await send('Browser.close'); } catch (_) {}
chrome.kill();
ws.close();
try { fs.rmSync(userDir, { recursive: true, force: true }); } catch (_) {}
process.exit(fail ? 1 : 0);
