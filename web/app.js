/* dshfind 前端：检索、全高结果列表、自绘滚动条、GSAP 动效。
   与后端（server.py）通过 127.0.0.1 上的 JSON 接口通信，每次启动的 token 放在 URL 里。 */

(() => {
  'use strict';

  // ---------- 基础 ----------
  const params = new URLSearchParams(location.search);
  const TOKEN = params.get('t') || '';
  const WIN_TITLE = params.get('w') || 'dshfind';
  document.title = WIN_TITLE;

  const $ = (id) => document.getElementById(id);
  const list = $('list');
  const inner = $('listInner');
  const empty = $('empty');
  const sbar = $('sbar');
  const sbarThumb = $('sbarThumb');
  const qInput = $('q');
  const preview = $('preview');
  const pvBody = $('pvBody');
  const pvMeta = $('pvMeta');
  const pvText = $('pvText');
  const pvTitle = $('pvTitle');
  const pvToggle = $('pvToggle');
  const modePill = $('modePill');
  const statEl = $('stat');
  const footStat = $('footStat');

  const hasGsap = typeof window.gsap !== 'undefined';
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const anim = hasGsap && !reduced;

  const state = {
    mode: 'file',
    q: '',
    hits: [],
    sel: -1,
    noise: true,
    ready: false,
    loading: true,
    refreshing: false,   // 是否正在全量重建索引
    lastQuery: '',
    seq: 0,          // 请求序号，防竞态
    pvCollapsed: true,   // 预览默认收起，把高度让给结果列表
  };

  // ---------- 网络 ----------
  async function api(path, opts = {}) {
    const url = path + (path.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(TOKEN);
    const res = await fetch(url, {
      ...opts,
      headers: { 'X-Dsh-Token': TOKEN, 'Content-Type': 'application/json' },
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }
  // 取消请求时 fetch 会抛 AbortError，调用方需要能识别（不要当成失败提示）

  const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body || {}) });

  // ---------- 彻底退出 ----------
  // 关窗口只是关窗口：后台的 HTTP 服务、热键轮询、四个卷的文件监听都还在。
  // 想真的退出就走这个接口，服务端会顺手把 Edge 窗口一起关掉再退进程。
  let quitting = false;
  async function quitApp() {
    if (quitting) return;
    quitting = true;
    const btn = $('quitBtn');
    if (btn) { btn.disabled = true; btn.textContent = '退出中…'; }
    footStat.innerHTML = '正在退出 dshfind…';
    try {
      await post('/api/quit');
    } catch (_) { /* 进程已经走了，请求可能断在这里，正常 */ }
    // Edge 应用窗口不是脚本打开的，window.close() 有可能被拦掉；
    // 服务端会负责把窗口关掉，这里的调用只是多一层保险。
    setTimeout(() => { try { window.close(); } catch (_) {} }, 300);
    setTimeout(() => {
      footStat.innerHTML = '已退出。这个窗口可以关了。';
      if (btn) btn.textContent = '已退出';
    }, 700);
  }

  // ---------- 重建索引 ----------
  // 运行期间新增的文件由文件监听自动收录（0.5 秒左右）；
  // 但**程序关着**的时候新增的文件没人看见，只能重扫一遍磁盘才能补进来。
  // 所以底部放一个「重建索引」按钮，等同 Ctrl+R。
  let reindexing = false;
  async function reindexNow(ask = true) {
    if (reindexing) return;
    if (ask) {
      const ok = window.confirm(
        '要重新扫描磁盘吗？\n\n' +
        '会遍历 C、D、E、F 四个卷，大约 8 分钟。\n' +
        '注意：重建期间索引是不完整的，可能搜不到东西，重建完就恢复。\n\n' +
        '什么时候需要：只有「程序关闭期间」新增/移动的文件才要这一步；' +
        '程序运行期间新增的文件会自动收录，不用重建。');
      if (!ok) return;
    }
    reindexing = true;
    const btn = $('reindexBtn');
    if (btn) { btn.disabled = true; btn.textContent = '重建中…'; }
    footStat.innerHTML = '已开始重建索引，进度显示在这里…';
    try {
      await post('/api/refresh');
    } catch (e) {
      footStat.innerHTML = '<span class="warn">启动重建失败：' + esc(e.message) + '</span>';
    }
    pollStatus(true);
    // 按钮的恢复交给 pollStatus（它会一直读 status.refreshing，刷新页面也不会错位）
  }

  // ---------- 外观设置（改名字 / 换图标） ----------
  // 名字存进 data/settings.json 的 app_title；图标由服务端存成 data/appicon.ico
  // （多档尺寸）+ data/appicon.png（预览）。Edge 的 --app 窗口标题跟着 document.title 走。
  //
  // 图标为什么要前端处理：标题栏/任务栏那个位置只有 16～32 像素。直接丢一张
  // 979×919 的原图上去，浏览器把它硬挤到 16px，细节全糊在一起（用户报的就是这个）。
  // 所以这里把原图**中心裁成正方形**，再按 256→128→64→48→32→24→16 逐档减半缩小，
  // 每档各自编码成 PNG 交给服务端打包成 ICO —— 系统能直接挑最合适的那一档。
  const AP = { title: '', icon: null, picked: null };
  const ICON_SIZES = [256, 128, 64, 48, 32, 24, 16];

  function loadImage(src) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error('图片读不出来（格式不支持？）'));
      img.src = src;
    });
  }

  function squareCanvas(img, size) {
    // 中心裁正方形再缩到 size×size；只在第一次从原图裁，后面都是逐档减半
    const c = document.createElement('canvas');
    c.width = c.height = size;
    const g = c.getContext('2d');
    g.imageSmoothingEnabled = true;
    g.imageSmoothingQuality = 'high';
    const side = Math.min(img.width, img.height);
    const sx = (img.width - side) / 2;
    const sy = (img.height - side) / 2;
    g.drawImage(img, sx, sy, side, side, 0, 0, size, size);
    return c;
  }

  function shrinkCanvas(srcCanvas, size) {
    const c = document.createElement('canvas');
    c.width = c.height = size;
    const g = c.getContext('2d');
    g.imageSmoothingEnabled = true;
    g.imageSmoothingQuality = 'high';
    g.drawImage(srcCanvas, 0, 0, srcCanvas.width, srcCanvas.height, 0, 0, size, size);
    return c;
  }

  async function buildIconSizes(file) {
    const url = URL.createObjectURL(file);
    let img;
    try {
      img = await loadImage(url);
    } finally {
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    const out = {};
    let canvas = squareCanvas(img, ICON_SIZES[0]);   // 256，从原图裁
    for (let i = 0; i < ICON_SIZES.length; i++) {
      const size = ICON_SIZES[i];
      if (i > 0) canvas = shrinkCanvas(canvas, size); // 每档都从上一档缩，避免一次缩太多
      out[size] = canvas.toDataURL('image/png');
    }
    return out;
  }

  function applyAppearance(a) {
    if (!a) return;
    AP.title = a.title || '';
    AP.icon = a.icon || null;
    document.title = AP.title || 'dshfind';
    const link = $('favicon');
    if (link) {
      // 一直用 /favicon.ico：服务端有自定义图标时返回那个多尺寸 ICO
      link.href = '/favicon.ico?v=' + (AP.icon && AP.icon.sizes && AP.icon.sizes.length
        ? AP.icon.url.split('v=')[1] : Date.now());
      if (AP.icon) {
        link.setAttribute('sizes',
          (AP.icon.sizes && AP.icon.sizes.length ? AP.icon.sizes : [16, 32, 48])
            .map((s) => s + 'x' + s).join(' '));
      } else {
        link.removeAttribute('sizes');
      }
    }
  }

  function renderIconPreview() {
    const box = $('apIconPreview');
    if (!box) return;
    const src = AP.picked ? AP.picked.dataUrl : (AP.icon ? AP.icon.url : '');
    box.innerHTML = src
      ? '<img alt="应用图标" src="' + src.replace(/"/g, '&quot;') + '">'
      : '<span class="icon-empty">未设置</span>';
  }

  function appearanceOpen() { return !$('appearanceMask').hidden; }

  async function openAppearance() {
    AP.picked = null;
    $('apName').value = AP.title || '';
    $('apStat').textContent = '';
    renderIconPreview();
    $('appearanceMask').hidden = false;
    setTimeout(() => $('apName').focus(), 30);
    try {
      const a = await api('/api/appearance');
      applyAppearance(a);
      $('apName').value = AP.title || '';
      renderIconPreview();
    } catch (_) { /* 读不到就用当前的 */ }
  }

  function closeAppearance() { $('appearanceMask').hidden = true; }

  async function saveAppearance() {
    const stat = $('apStat');
    stat.textContent = '保存中…';
    try {
      const r1 = await post('/api/appearance', { title: $('apName').value.trim() });
      if (!r1.ok) { stat.textContent = r1.error || '保存失败'; return; }
      applyAppearance(r1.appearance);
      if (AP.picked) {
        const body = AP.picked.sizes
          ? { sizes: AP.picked.sizes }
          : { data: AP.picked.dataUrl };
        const r2 = await post('/api/appearance/icon', body);
        if (!r2.ok) { stat.textContent = r2.error || '图标保存失败'; return; }
        applyAppearance(r2.appearance);
        AP.picked = null;
      }
      $('apName').value = AP.title || '';
      renderIconPreview();
      stat.textContent = '已保存';
      setTimeout(closeAppearance, 500);
    } catch (e) {
      stat.textContent = '保存失败：' + e.message;
    }
  }


  // ---------- 工具 ----------
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function fmtSize(n) {
    if (n < 0) return '';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = n, i = 0;
    while (v >= 1000 && i < u.length - 1) { v /= 1000; i++; }
    return (i === 0 ? v : v.toFixed(1)) + ' ' + u[i];
  }

  function fmtTime(ms) {
    if (!ms) return '';
    const d = new Date(ms);
    const p = (x) => String(x).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
           ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function fmtAgo(ms) {
    if (!ms) return '';
    const s = (Date.now() - ms) / 1000;
    if (s < 60) return '刚刚';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
    if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
    if (s < 86400 * 30) return Math.floor(s / 86400) + ' 天前';
    return fmtTime(ms).slice(0, 10);
  }

  const ROLE_CN = {
    user: '我', assistant: '助手', 'tool-call': '调用',
    'tool-result': '工具输出', title: '标题', system: '系统',
    'session-header': '会话头',
  };

  // 把 "shift+ctrl+f" 显示成 "Shift+Ctrl+F"
  function hotkeyLabel(spec) {
    return String(spec).split('+').map((p) => {
      const k = p.trim().toLowerCase();
      if (k === 'shift' || k === 'ctrl' || k === 'alt' || k === 'win') {
        return k.charAt(0).toUpperCase() + k.slice(1);
      }
      return p.trim().toUpperCase();
    }).join('+');
  }

  // ---------- 模式切换（GSAP 滑动的底块） ----------
  function movePill(instant) {
    const btn = document.querySelector('.mode.is-on');
    if (!btn) return;
    const target = { x: btn.offsetLeft, width: btn.offsetWidth };
    if (!anim) {
      modePill.style.transform = 'translateX(' + target.x + 'px)';
      modePill.style.width = target.width + 'px';
      return;
    }
    gsap.to(modePill, {
      x: target.x, width: target.width,
      duration: instant ? 0 : 0.34, ease: 'power3.out',
    });
  }

  function setMode(mode, opts = {}) {
    if (state.mode === mode && !opts.force) return;
    state.mode = mode;
    document.querySelectorAll('.mode').forEach((b) => {
      b.classList.toggle('is-on', b.dataset.mode === mode);
    });
    movePill();
    if (anim) {
      gsap.fromTo('.searchbox',
        { y: 6 }, { y: 0, duration: 0.3, ease: 'power2.out' });
    }
    runSearch();
    qInput.focus();
  }

  document.querySelectorAll('.mode').forEach((b) => {
    b.addEventListener('click', () => setMode(b.dataset.mode));
  });

  // ---------- 渲染 ----------
  function fileRowHtml(h, i) {
    const isDir = h.is_dir;
    let name = esc(h.name);
    const sp = h.span;
    if (sp && sp.kind === 'name' && sp.text) {
      const s = sp.s, e = sp.e;
      const raw = h.name;
      name = esc(raw.slice(0, s)) + '<span class="sz">' + esc(raw.slice(s, e)) +
             '</span>' + esc(raw.slice(e));
    }
    let dir = h.disp.slice(0, h.disp.length - h.name.length - 1) || h.disp;
    if (sp && sp.kind === 'path' && sp.text) dir = esc(dir);

    const meta = [];
    meta.push(isDir ? '文件夹' : fmtSize(h.size));
    meta.push(fmtAgo(h.mtime));
    if (sp && (sp.kind === 'initials' || sp.kind === 'full')) {
      meta.push('拼音命中 ' + (sp.kind === 'initials' ? '首字母' : '全拼') + '：' + sp.text);
    }
    return '<div class="row" data-i="' + i + '">' +
      '<div class="row-main">' +
        '<div class="row-name' + (isDir ? ' is-dir' : '') + '">' + name + '</div>' +
        '<div class="row-sub">' + esc(dir) +
          '<span class="dot">·</span>' + esc(meta.join(' · ')) +
        '</div>' +
      '</div>' +
      '<div class="row-badge">' + esc(h.kind || '') + '</div>' +
    '</div>';
  }

  // DSH 的项目目录名是转义过的（--D-unity~76F8~5173~5BF9~8BDD--），
  // ~XXXX 是 Unicode 码点十六进制，还原回中文才好读
  function decodeProj(s) {
    if (!s) return '';
    return String(s).replace(/^--|--$/g, '')
      .replace(/~([0-9A-Fa-f]{4})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)));
  }

  function sessionRowHtml(h, i) {
    // 「对话」模式按需求只搜标题：高亮也只可能出现在标题里
    let title = esc(h.title || '(无标题)');
    if (h.snippet && h.snippet.indexOf('[') >= 0) {
      title = esc(h.snippet).replace(/\[([^\]]+)\]/g, '<span class="hl">$1</span>');
    }
    const proj = esc(decodeProj(h.proj));
    return '<div class="row" data-i="' + i + '">' +
      '<div class="row-main">' +
        '<div class="row-name row-snip">' + title + '</div>' +
        '<div class="row-sub">' +
          (proj ? proj + '<span class="dot">·</span>' : '') +
          esc(h.cwd || '') +
          '<span class="dot">·</span>' + fmtAgo(h.time) +
        '</div>' +
      '</div>' +
      '<div class="row-badge">会话</div>' +
    '</div>';
  }

  function render(opts = {}) {
    const hits = state.hits;
    if (!hits.length) {
      inner.innerHTML = '';
      empty.hidden = false;
      sbar.hidden = true;
      if (opts.emptyText) {
        empty.querySelector('.empty-title').textContent = opts.emptyText;
        empty.querySelector('.empty-sub').innerHTML = opts.emptySub || '';
      }
      return;
    }
    empty.hidden = true;
    const html = state.mode === 'file'
      ? hits.map(fileRowHtml).join('')
      : hits.map(sessionRowHtml).join('');
    inner.innerHTML = html;
    list.scrollTop = 0;
    state.sel = hits.length ? 0 : -1;
    paintSelection();
    syncScrollbar();
    animateRows();
    updatePvSummary();
    if (!state.pvCollapsed) {
      if (state.mode === 'file') loadFilePreview(0);
      else loadSessionPreview(0);
    }
  }

  function animateRows() {
    if (!anim) return;
    const rows = inner.querySelectorAll('.row');
    const n = Math.min(rows.length, 14);
    // 同样用 from + immediateRender:false：行本来就是可见的，
    // 动画只是"接下来 0.3 秒淡入"，帧没跑也不会留下不可见的行。
    gsap.from(Array.from(rows).slice(0, n), {
      autoAlpha: 0, y: 10, duration: 0.3, stagger: 0.018, ease: 'power2.out',
      immediateRender: false, clearProps: 'transform,opacity,visibility',
    });
  }

  function paintSelection() {
    const rows = inner.children;
    for (let i = 0; i < rows.length; i++) {
      rows[i].classList.toggle('is-sel', i === state.sel);
    }
  }

  // ---------- 自绘滚动条 ----------
  function syncScrollbar() {
    const sh = list.scrollHeight;
    const ch = list.clientHeight;
    if (sh <= ch + 1) { sbar.hidden = true; return; }
    sbar.hidden = false;
    const track = sbar.clientHeight;
    const th = Math.max(28, Math.round(track * ch / sh));
    sbarThumb.style.height = th + 'px';
    const maxTop = track - th;
    const p = list.scrollTop / (sh - ch);
    sbarThumb.style.transform = 'translateY(' + Math.round(maxTop * p) + 'px)';
  }

  list.addEventListener('scroll', syncScrollbar, { passive: true });
  window.addEventListener('resize', () => { syncScrollbar(); movePill(true); });

  // 拖动滑块
  let drag = null;
  sbarThumb.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const track = sbar.clientHeight;
    const th = sbarThumb.offsetHeight;
    drag = { y: e.clientY, top: list.scrollTop, range: track - th };
    sbarThumb.classList.add('is-drag');
    document.body.style.cursor = 'grabbing';
  });
  window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    const dy = e.clientY - drag.y;
    const sh = list.scrollHeight, ch = list.clientHeight;
    const maxScroll = Math.max(1, sh - ch);
    const ratio = drag.range > 0 ? dy / drag.range : 0;
    list.scrollTop = drag.top + ratio * maxScroll;
  });
  window.addEventListener('mouseup', () => {
    if (!drag) return;
    drag = null;
    sbarThumb.classList.remove('is-drag');
    document.body.style.cursor = '';
  });
  // 点轨道翻页
  sbar.addEventListener('mousedown', (e) => {
    if (e.target === sbarThumb) return;
    const r = sbar.getBoundingClientRect();
    const forward = e.clientY > r.top + r.height / 2;
    smoothScroll(list.scrollTop + (forward ? 1 : -1) * list.clientHeight * 0.9);
  });

  function smoothScroll(top) {
    const max = Math.max(0, list.scrollHeight - list.clientHeight);
    const target = Math.max(0, Math.min(max, top));
    if (!anim) { list.scrollTop = target; return; }
    gsap.to(list, {
      scrollTop: target, duration: 0.3, ease: 'power2.out', overwrite: true,
      onUpdate: syncScrollbar,
    });
  }

  // 滚轮：自己做缓动，避免一格一格地跳
  list.addEventListener('wheel', (e) => {
    if (e.ctrlKey) return;
    const max = Math.max(0, list.scrollHeight - list.clientHeight);
    if (max <= 0) return;
    e.preventDefault();
    const cur = (gsap && gsap.getProperty(list, 'scrollTop')) || list.scrollTop;
    smoothScroll(cur + e.deltaY * 1.1);
  }, { passive: false });

  // ---------- 选择与滚动定位 ----------
  function ensureVisible(i) {
    const row = inner.children[i];
    if (!row) return;
    const top = row.offsetTop;
    const bottom = top + row.offsetHeight;
    const viewTop = list.scrollTop;
    const viewBottom = viewTop + list.clientHeight;
    if (top < viewTop + 4) smoothScroll(top - 4);
    else if (bottom > viewBottom - 4) smoothScroll(bottom - list.clientHeight + 4);
  }

  function select(i, opts = {}) {
    if (!state.hits.length) return;
    i = Math.max(0, Math.min(state.hits.length - 1, i));
    state.sel = i;
    paintSelection();
    ensureVisible(i);
    updatePvSummary();
    if (!state.pvCollapsed) {
      if (state.mode === 'file') loadFilePreview(i);
      else loadSessionPreview(i);
    }
  }

  // 收起状态下，标题栏显示当前选中项的摘要，不白占高度
  function updatePvSummary() {
    const h = state.hits[state.sel];
    if (!h) { pvTitle.textContent = '预览'; return; }
    if (state.mode === 'file') {
      const meta = h.is_dir ? '文件夹' : fmtSize(h.size);
      pvTitle.textContent = (h.name || '') + '   ·   ' + meta +
        '   ·   ' + (state.pvCollapsed ? '空格展开' : '空格收起');
    } else {
      pvTitle.textContent = (h.title || '(无标题)') +
        '   ·   ' + (state.pvCollapsed ? '空格展开会话内容' : '空格收起');
    }
  }

  // ---------- 预览 ----------
  function setPreviewMeta(rows) {
    pvMeta.innerHTML = rows.map(([k, v]) =>
      '<div><span class="k">' + esc(k) + '</span><span class="v">' + esc(v) +
      '</span></div>').join('');
  }

  async function loadFilePreview(i) {
    const h = state.hits[i];
    if (!h) return;
    pvTitle.textContent = '预览';
    const rows = [
      ['名称', h.name],
      ['路径', h.disp],
      ['类型', h.is_dir ? '文件夹' : (h.ext ? h.ext.toUpperCase() : '无扩展名')],
    ];
    if (!h.is_dir) {
      rows.push(['大小', fmtSize(h.size) + '（' + h.size.toLocaleString() + ' 字节）']);
    }
    rows.push(['修改', fmtTime(h.mtime) + '（' + fmtAgo(h.mtime) + '）']);
    if (h.noise) rows.push(['标记', '位于缓存/依赖目录']);
    setPreviewMeta(rows);
    pvText.textContent = h.is_dir ? '' : '　';
    try {
      const info = await api('/api/preview?path=' + encodeURIComponent(h.path));
      if (!info.ok) { pvText.textContent = ''; return; }
      if (info.initials) {
        setPreviewMeta(rows.concat([['拼音', info.initials + ' / ' + info.pinyin]]));
      }
      if (info.text != null) {
        pvText.textContent = info.text + (info.text_truncated ? '\n\n…（内容较长，已截断）' : '');
      } else {
        pvText.textContent = '';
      }
    } catch (_) { /* 预览失败不影响主流程 */ }
  }

  async function loadSessionPreview(i) {
    const h = state.hits[i];
    if (!h) return;
    pvTitle.textContent = '会话内容（按需解压，只在预览时读正文）';
    setPreviewMeta([
      ['标题', h.title || '(无标题)'],
      ['项目', h.cwd || decodeProj(h.proj) || ''],
      ['时间', fmtTime(h.time)],
    ]);
    if (!h.log_path) { pvText.textContent = '找不到会话日志文件'; return; }
    pvText.textContent = '载入中…';
    try {
      const r = await api('/api/context?log_path=' + encodeURIComponent(h.log_path));
      if (!r.ok) { pvText.textContent = '读取失败：' + (r.error || '未知错误'); return; }
      setPreviewMeta([
        ['标题', h.title || '(无标题)'],
        ['项目', h.cwd || decodeProj(h.proj) || ''],
        ['时间', fmtTime(h.time)],
        ['事件数', String(r.total || 0)],
        ['显示', '前 ' + (r.events || []).length + ' 条'],
      ]);
      pvText.textContent = (r.events || []).map((ev) => {
        const head = '[' + (ROLE_CN[ev.role] || ev.role) + '] ' + fmtTime(ev.time_ms);
        const body = (ev.text || '').slice(0, 1200);
        return head + '\n' + body;
      }).join('\n\n───\n\n');
    } catch (e) {
      pvText.textContent = '读取失败：' + e.message;
    }
  }

  function togglePreview(force) {
    state.pvCollapsed = force === undefined ? !state.pvCollapsed : force;
    preview.classList.toggle('is-collapsed', state.pvCollapsed);
    if (!state.pvCollapsed) {
      if (state.mode === 'file') loadFilePreview(state.sel);
      else loadSessionPreview(state.sel);
    }
    updatePvSummary();
    if (anim) {
      gsap.fromTo(preview, { y: state.pvCollapsed ? 0 : 8 },
        { y: 0, duration: 0.26, ease: 'power2.out' });
    }
    syncScrollbar();
  }

  pvToggle.addEventListener('click', () => togglePreview());

  // ---------- 搜索 ----------
  let debounce = null;
  let inflight = null;      // 上一轮的请求，输入变化时直接取消，避免结果错乱

  // 「正在搜索」的即时反馈：文件模式一次查询要几百毫秒到两秒（要还原 1000 条真实大小写），
  // 不给提示的话，用户看到的就是"打了字什么也没有"。
  function setBusy(on, q) {
    list.classList.toggle('is-busy', !!on);
    if (on) {
      footStat.innerHTML = (state.refreshing
        ? '索引正在重建（约 8 分钟），结果可能不完整 · '
        : '') + '正在搜索 <b>' + esc(q) + '</b> …';
      if (!state.hits.length) {
        render({ emptyText: state.refreshing ? '索引重建中…' : '正在搜索…',
                 emptySub: state.refreshing
                   ? '重建期间结果可能不完整，重建完就恢复'
                   : '在索引 <b>' + (state.count || 0).toLocaleString() +
                     '</b> 条里找 <code>' + esc(q) + '</code>' });
      }
    }
  }

  function runSearch(delay = 55) {
    clearTimeout(debounce);
    debounce = setTimeout(doSearch, delay);
  }

  async function doSearch() {
    const q = qInput.value.trim();
    state.q = q;
    const mySeq = ++state.seq;
    if (!q) {
      if (inflight) { inflight.abort(); inflight = null; }
      state.hits = [];
      setBusy(false);
      render({ emptyText: '开始输入', emptySub: defaultHint() });
      footStat.textContent = state.mode === 'file'
        ? '索引 ' + state.count.toLocaleString() + ' 条'
        : '已索引 ' + (state.sessions || 0) + ' 个会话标题';
      return;
    }
    if (!state.ready) {
      footStat.textContent = '索引还在载入，稍等一下…';
      return;
    }
    if (inflight) inflight.abort();
    inflight = new AbortController();
    const signal = inflight.signal;
    setBusy(true, q);
    try {
      if (state.mode === 'file') {
        const r = await api('/api/search?q=' + encodeURIComponent(q) +
                            '&limit=1000&noise=' + (state.noise ? '1' : '0'),
                            { signal });
        if (mySeq !== state.seq) return;
        if (r.aborted) return;              // 服务端说"这个查询已经被后面取代了"
        setBusy(false);
        state.hits = r.hits || [];
        render({ emptyText: '没有匹配结果',
                 emptySub: '试试更短的关键词，或按 <code>Ctrl+E</code> 把缓存/依赖目录也纳入' });
        const diag = (r.diagnostics || []).join('；');
        footStat.innerHTML = '候选 <b>' + (r.candidates || 0).toLocaleString() +
          '</b> 条 · 返回 <b>' + state.hits.length + '</b> 条 · ' +
          Math.round(r.total_ms) + ' ms' +
          (r.parsed ? ' · <span style="opacity:.7">' + esc(r.parsed) + '</span>' : '') +
          (diag ? ' · <span class="warn">' + esc(diag) + '</span>' : '');
      } else {
        const r = await api('/api/session?q=' + encodeURIComponent(q) + '&limit=500',
                            { signal });
        if (mySeq !== state.seq) return;
        setBusy(false);
        state.hits = r.hits || [];
        render({ emptyText: '没有标题匹配的会话',
                 emptySub: '这里只搜会话<b>标题</b>，不搜正文' });
        footStat.innerHTML = '标题命中 <b>' + state.hits.length + '</b> 条 · ' +
          Math.round(r.ms) + ' ms · 已索引 ' + (state.sessions || 0) + ' 个会话';
      }
    } catch (e) {
      if (mySeq === state.seq) setBusy(false);
      if (e.name === 'AbortError') return;     // 被更新的输入取代了，正常
      footStat.innerHTML = '<span class="warn">查询失败：' + esc(e.message) + '</span>';
    }
  }

  function defaultHint() {
    return state.mode === 'file'
      ? '支持拼音首字母 <code>wd</code>、中文、<code>ext:md</code>、<code>size:&gt;10mb</code>、<code>dm:week</code>、<code>*.png</code>、<code>a | b</code>'
      : '只搜会话<b>标题</b>（不搜正文）；输入几个字就会即时过滤';
  }

  qInput.addEventListener('input', () => runSearch());
  qInput.addEventListener('focus', () => $('searchbox').classList.add('is-focus'));
  qInput.addEventListener('blur', () => $('searchbox').classList.remove('is-focus'));

  // ---------- 键盘 ----------
  document.addEventListener('keydown', (e) => {
    const k = e.key;
    if (k === 'ArrowDown' || (k === 'n' && e.ctrlKey)) {
      e.preventDefault(); select(state.sel + 1); return;
    }
    if (k === 'ArrowUp' || (k === 'p' && e.ctrlKey)) {
      e.preventDefault(); select(state.sel - 1); return;
    }
    if (k === ' ') {
      e.preventDefault();
      togglePreview();
      return;
    }
    if (k === 'PageDown') { e.preventDefault(); select(state.sel + 12); return; }
    if (k === 'PageUp') { e.preventDefault(); select(state.sel - 12); return; }
    if (k === 'Escape') {
      if (appearanceOpen()) { closeAppearance(); return; }
      if (qInput.value) { qInput.value = ''; runSearch(0); }
      else window.close();
      return;
    }
    if (k === 'Tab') {
      e.preventDefault();
      setMode(state.mode === 'file' ? 'session' : 'file');
      return;
    }
    if (k === 'q' && e.ctrlKey) {
      e.preventDefault();
      quitApp();
      return;
    }
    if (k === 'Enter') {
      const h = state.hits[state.sel];
      if (!h) return;
      e.preventDefault();
      if (e.ctrlKey) {
        if (state.mode === 'file') post('/api/reveal', { path: h.path });
      } else if (state.mode === 'file') {
        post('/api/open', { path: h.path });
      }
      return;
    }
    if (k === 'c' && e.ctrlKey) {
      const h = state.hits[state.sel];
      if (!h) return;
      e.preventDefault();
      const text = state.mode === 'file' ? h.disp : (h.sid + '#' + h.seq);
      navigator.clipboard.writeText(text).then(() => {
        footStat.innerHTML = '已复制：<b>' + esc(text) + '</b>';
      });
      return;
    }
    if (k === 'e' && e.ctrlKey) {
      e.preventDefault();
      post('/api/toggle-noise').then((r) => {
        state.noise = r.noise_on;
        footStat.innerHTML = '缓存/依赖目录：<b>' + (r.noise_on ? '已隐藏' : '已包含') + '</b>';
        runSearch(0);
      });
      return;
    }
    if (k === 'r' && e.ctrlKey) {
      e.preventDefault();
      reindexNow(false);        // 快捷键不弹确认框，按了就开始
      return;
    }
    if (k === '/' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      qInput.focus(); qInput.select();
    }
  });

  list.addEventListener('click', (e) => {
    const row = e.target.closest('.row');
    if (!row) return;
    select(Number(row.dataset.i));
  });
  list.addEventListener('dblclick', (e) => {
    const row = e.target.closest('.row');
    if (!row) return;
    const h = state.hits[Number(row.dataset.i)];
    if (h && state.mode === 'file') post('/api/open', { path: h.path });
  });

  $('quitBtn').addEventListener('click', quitApp);
  $('reindexBtn').addEventListener('click', () => reindexNow(true));

  // ---------- 外观设置的事件 ----------
  $('settingsBtn').addEventListener('click', openAppearance);
  $('apClose').addEventListener('click', closeAppearance);
  $('apCancel').addEventListener('click', closeAppearance);
  $('apSave').addEventListener('click', saveAppearance);
  $('appearanceMask').addEventListener('click', (e) => {
    if (e.target === $('appearanceMask')) closeAppearance();   // 点遮罩关掉
  });
  $('apPick').addEventListener('click', () => $('apFile').click());
  $('apFile').addEventListener('change', async () => {
    const f = $('apFile').files && $('apFile').files[0];
    if (!f) return;
    const stat = $('apStat');
    if (f.size > 12_000_000) {
      stat.textContent = '图片太大了（原图上限 12 MB）';
      return;
    }
    stat.textContent = '正在裁成正方形并生成各档尺寸…';
    try {
      const sizes = await buildIconSizes(f);       // {16: dataURL, 32: …, 256: …}
      AP.picked = { dataUrl: sizes[256], sizes, name: f.name };
      stat.textContent = '已选择：' + f.name + '（已缩成 '
        + ICON_SIZES[ICON_SIZES.length - 1] + '～' + ICON_SIZES[0]
        + ' 像素，点保存生效）';
      renderIconPreview();
    } catch (e) {
      stat.textContent = '处理图片失败：' + e.message;
    }
    $('apFile').value = '';
  });
  $('apResetIcon').addEventListener('click', async () => {
    try {
      const r = await post('/api/appearance/icon/reset', {});
      AP.picked = null;
      if (r.ok) {
        applyAppearance(r.appearance);
        renderIconPreview();
        $('apStat').textContent = '图标已恢复默认';
      } else {
        $('apStat').textContent = r.error || '恢复失败';
      }
    } catch (e) { $('apStat').textContent = '恢复失败：' + e.message; }
  });

  // ---------- 状态轮询 ----------
  let pollTimer = null;

  async function pollStatus(force) {
    clearTimeout(pollTimer);
    let st;
    try {
      st = await api('/api/status');
    } catch (e) {
      footStat.innerHTML = '<span class="warn">连不上后端服务</span>';
      pollTimer = setTimeout(() => pollStatus(force), 800);
      return;
    }
    state.count = st.entries || 0;
    state.sessions = st.sessions || 0;
    state.noise = st.noise_on;

    const wasReady = state.ready;
    state.ready = !!st.ready;
    state.loading = !!st.loading;
    state.refreshing = !!st.refreshing;      // 「重建索引」按钮靠它恢复

    const rb = $('reindexBtn');
    if (rb) {
      if (st.refreshing) {
        rb.disabled = true;
        rb.textContent = '重建中…';
      } else if (reindexing || rb.disabled) {
        reindexing = false;
        rb.disabled = false;
        rb.textContent = '重建索引';
      }
      // 监听丢过事件 → 索引可能不完整：把按钮点亮，提示用户自己决定要不要重扫
      rb.classList.toggle('is-alert', !!st.needs_rescan && !st.refreshing);
      rb.title = st.needs_rescan && !st.refreshing
        ? '监听丢过文件变更事件，索引可能不完整 —— 需要的话点这里重扫（约 8 分钟）'
        : '重新扫描磁盘（约 8 分钟），把程序关闭期间新增的文件补进来，等同 Ctrl+R';
    }

    statEl.innerHTML = '文件 <b>' + (st.entries || 0).toLocaleString() + '</b> 条 · 会话 <b>' +
      (st.sessions || 0) + '</b> 个' +
      (st.hotkey ? ' · 唤出 <b>' + esc(hotkeyLabel(st.hotkey)) + '</b>' : '');
    statEl.style.color = '';

    if (st.load_error) {
      footStat.innerHTML = '<span class="warn">索引载入失败：' + esc(st.load_error) +
        '（可跑 python dshfind.py --reindex）</span>';
    } else if (st.loading) {
      footStat.innerHTML = '<b>正在载入索引…</b>（首次约 10～20 秒）';
    } else if (st.refreshing) {
      footStat.innerHTML = '<b>刷新中</b>：' + esc(st.refresh_detail || st.refresh_phase || '');
    } else if (!state.q) {
      const watch = st.watch_running
        ? ' · <b style="color:var(--accent)">自动更新中</b>' +
          (st.watch_changes ? '（已同步 ' + st.watch_changes + ' 次变更）' : '')
        : ' · 监听未启动';
      // 监听丢过事件时只提示，不自动扫 —— 扫不扫由用户点「重建索引」决定
      const stale = st.needs_rescan
        ? ' · <span class="warn">监听丢过事件，索引可能不完整（可点「重建索引」）</span>'
        : '';
      footStat.innerHTML = '索引 <b>' + (st.entries || 0).toLocaleString() + '</b> 条' +
        (st.noise_on ? ' · 已隐藏缓存/依赖目录' : ' · 含缓存/依赖目录') + watch + stale;
    }

    if (state.ready && !wasReady) {
      // 索引刚就绪：让界面有个"到位"的反馈
      if (anim) {
        gsap.fromTo('.searchbox', { borderColor: 'rgba(91,157,255,.85)' },
          { borderColor: 'var(--line)', duration: 0.9, ease: 'power2.out',
            clearProps: 'borderColor' });
      }
      if (state.q) runSearch(0);
    }

    const busy = st.loading || st.refreshing || !state.ready;
    pollTimer = setTimeout(() => pollStatus(false), busy ? 600 : 5000);
  }

  // ---------- 启动 ----------
  function boot() {
    movePill(true);
    preview.classList.toggle('is-collapsed', state.pvCollapsed);
    if (anim) {
      // 关键：用 from + immediateRender:false —— 元素的"自然状态"就是可见的，
      // 动画只在第一帧之后才把它压回不可见再淡入。
      // 这样即使 requestAnimationFrame 没跑（无头浏览器、后台标签页、掉帧），
      // 界面也一定是可见的，不会出现一片空白。
      gsap.from('.app', {
        autoAlpha: 0, scale: 0.985, duration: 0.42, ease: 'power3.out',
        transformOrigin: '50% 0%', immediateRender: false,
      });
      gsap.from('.searchbox', {
        autoAlpha: 0, y: 8, duration: 0.4, delay: 0.06, ease: 'power2.out',
        immediateRender: false,
      });
      gsap.from('.list-wrap', {
        autoAlpha: 0, y: 10, duration: 0.45, delay: 0.12, ease: 'power2.out',
        immediateRender: false,
      });
      gsap.from('.preview', {
        autoAlpha: 0, y: 10, duration: 0.45, delay: 0.18, ease: 'power2.out',
        immediateRender: false,
      });
    }
    render({ emptyText: '正在载入索引…', emptySub: defaultHint() });
    qInput.focus();
    pollStatus(true);
    // 预填查询 / 预设模式（方便从命令行或脚本直接打开到某个状态）
    const pre = params.get('q');
    if (params.get('mode') === 'session') setMode('session', { force: true });
    if (pre) { qInput.value = pre; runSearch(0); }
    // 读一次外观（名字 + 图标），并把窗口标题对齐到用户设置的名字
    api('/api/appearance').then((a) => {
      applyAppearance(a);
      if (params.get('settings') === '1') openAppearance();
    }).catch(() => {});
    // 兜底：万动画被卡住，1.2 秒后强制把外壳显示出来
    setTimeout(() => {
      document.querySelectorAll('.app, .searchbox, .list-wrap, .preview').forEach((el) => {
        if (getComputedStyle(el).opacity === '0') {
          el.style.opacity = '1';
          el.style.visibility = 'visible';
        }
      });
      document.querySelectorAll('.row').forEach((el) => {
        if (getComputedStyle(el).opacity === '0') el.style.opacity = '1';
      });
    }, 1200);
    // 字体加载完再量一次模式底块
    document.fonts && document.fonts.ready.then(() => movePill(true));
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
