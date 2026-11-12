// 量底栏布局：状态文字变长之后，右边三个按钮还在不在窗口里、点不点得着。
// 用法：node _test_foot.mjs <url> <截图前缀> [宽度列表，逗号分隔]
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const URL_APP = process.argv[2];
const SHOT_PREFIX = process.argv[3] || '_qa_foot';
const WIDTHS = (process.argv[4] || '1707,1400,1200,1000').split(',').map(Number);
if (!URL_APP) { console.error('需要传入 url'); process.exit(2); }

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9347;
const userDir = path.join(os.tmpdir(), 'dshfoot-' + Date.now());
const chrome = spawn(CHROME, [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-breakpad',
  '--disable-crash-reporter', '--disable-extensions', '--hide-scrollbars',
  '--force-device-scale-factor=1', '--window-size=1700,900',
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
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
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

for (let i = 0; i < 60; i++) {
  const t = await ev(`(document.getElementById('footStat')||{}).textContent||''`);
  if (typeof t === 'string' && (t.includes('自动更新') || t.includes('监听丢过'))) break;
  await sleep(300);
}
await sleep(900);

const measure = () => ev(`(() => {
  const vw = window.innerWidth;
  const r = (el) => {
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return { l: Math.round(b.left), r: Math.round(b.right), w: Math.round(b.width),
             h: Math.round(b.height), hidden: el.hidden };
  };
  const hit = (el) => {
    if (!el || el.hidden) return null;
    const b = el.getBoundingClientRect();
    const x = Math.min(Math.max(b.left + b.width / 2, 0), vw - 1);
    const y = b.top + b.height / 2;
    if (b.left < 0 || b.right > vw) return 'out-of-view';
    const t = document.elementFromPoint(x, y);
    if (!t) return 'nothing';
    return (t === el || el.contains(t)) ? 'ok' : ('covered-by:' + (t.className || t.tagName));
  };
  const foot = document.querySelector('.foot');
  const stat = document.getElementById('footStat');
  const keys = document.querySelector('.foot .keys');
  return {
    vw,
    foot: r(foot), footScrollW: foot.scrollWidth, footClientW: foot.clientWidth,
    stat: r(stat), statScrollW: stat.scrollWidth, statClientW: stat.clientWidth,
    statOverflow: stat.scrollWidth - stat.clientWidth,
    statTextEnd: Math.round(stat.getBoundingClientRect().left + stat.scrollWidth),
    keys: r(keys), keysOverflow: keys ? keys.scrollWidth - keys.clientWidth : null,
    root: r(document.getElementById('rebuildRootBtn')),
    reindex: r(document.getElementById('reindexBtn')),
    quit: r(document.getElementById('quitBtn')),
    hitRoot: hit(document.getElementById('rebuildRootBtn')),
    hitReindex: hit(document.getElementById('reindexBtn')),
    hitQuit: hit(document.getElementById('quitBtn')),
    statText: stat.textContent.slice(0, 200),
    footTitle: stat.title || '',
  };
})()`);

const rows = [];
for (const w of WIDTHS) {
  await send('Emulation.setDeviceMetricsOverride',
    { width: w, height: 820, deviceScaleFactor: 1, mobile: false });
  await sleep(500);
  const m = await measure();
  rows.push(m);
  const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  const file = `${SHOT_PREFIX}_${w}.png`;
  fs.writeFileSync(file, Buffer.from(shot.result.data, 'base64'));
  // 只截底栏（放大 2 倍，方便看清省略号和按钮）
  const footShot = await send('Page.captureScreenshot', {
    format: 'png',
    clip: { x: 0, y: 820 - 44, width: w, height: 44, scale: 2 },
    captureBeyondViewport: false,
  });
  if (footShot.result?.data) {
    fs.writeFileSync(`${SHOT_PREFIX}_foot_${w}.png`, Buffer.from(footShot.result.data, 'base64'));
  }
}

let bad = 0;
for (const m of rows) {
  const problems = [];
  if (m.reindex.r > m.vw) problems.push('重建索引 出界');
  if (!m.root.hidden && m.root.r > m.vw) problems.push('只重建 F 盘 出界');
  if (m.quit.r > m.vw) problems.push('退出 出界');
  if (m.hitReindex !== 'ok') problems.push('重建索引 ' + m.hitReindex);
  if (!m.root.hidden && m.hitRoot !== 'ok') problems.push('只重建F盘 ' + m.hitRoot);
  if (m.hitQuit !== 'ok') problems.push('退出 ' + m.hitQuit);
  if (m.footScrollW > m.footClientW) problems.push('foot 横向溢出 ' + (m.footScrollW - m.footClientW) + 'px');
  if (!m.footTitle || m.footTitle.trim() !== m.statText.trim()) problems.push('悬停全文没跟上');
  if (problems.length) bad++;
  console.log(
    `宽 ${m.vw} ｜ 重建索引右边缘 ${m.reindex.r} ｜ 退出右边缘 ${m.quit.r} ｜ ` +
    `状态文字溢出 ${m.statOverflow}px ｜ 快捷键提示溢出 ${m.keysOverflow}` +
    (problems.length ? '  ❌ ' + problems.join('；') : '  ✅ 可点'));
}
console.log('\n' + JSON.stringify(rows, null, 1));
console.log('\n截图：' + rows.map((m) => `${SHOT_PREFIX}_${m.vw}.png`).join('、'));

try { await send('Browser.close'); } catch (_) {}
chrome.kill();
ws.close();
try { fs.rmSync(userDir, { recursive: true, force: true }); } catch (_) {}
process.exit(bad ? 1 : 0);
