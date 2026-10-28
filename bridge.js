// dshfind 的 zstd 解码桥。
//
// DSH 的会话日志是一种"拼接的多帧 zstd"容器（每个 append 批次一个独立帧，
// 各自带校验和），不是单个连续 zstd 流。Node 的 zstdDecompressSync 只吃
// 第一帧，所以这里照 DSH 自己的 scanZstdFrames 实现先扫帧、再逐帧解压。
//
// 用法：node bridge.js --in <日志> --out <输出jsonl> [--offset <字节>]
//       0 退出 = 成功；输出每行一个事件对象。

'use strict';
const fs = require('fs');
const zlib = require('zlib');

const ZSTD_MAGIC = 4247762216;

/** 扫出所有结构完整的 zstd 帧区间（不依赖解压）。 */
function scanZstdFrames(buffer) {
  const frames = [];
  let offset = 0;
  while (offset < buffer.length) {
    const start = offset;
    if (buffer.length - offset < 4) return { frames, tornStart: start };
    if (buffer.readUInt32LE(offset) !== ZSTD_MAGIC) {
      // 帧魔数不对：如果不是在开头，就当成撕裂尾部处理，能读的都读出来
      if (frames.length === 0) throw new Error('invalid zstd frame magic at byte ' + offset);
      return { frames, tornStart: start };
    }
    offset += 4;
    if (offset === buffer.length) return { frames, tornStart: start };
    const descriptor = buffer.readUInt8(offset);
    offset += 1;
    if ((descriptor & 24) !== 0) throw new Error('reserved frame-header bit at ' + (offset - 1));
    const contentSizeFlag = descriptor >>> 6;
    const singleSegment = (descriptor & 32) !== 0;
    const checksum = (descriptor & 4) !== 0;
    const dictionaryFlag = descriptor & 3;
    const dictionaryBytes = dictionaryFlag === 3 ? 4 : dictionaryFlag;
    const contentSizeBytes = contentSizeFlag === 0 ? (singleSegment ? 1 : 0) : (1 << contentSizeFlag);
    const remainingHeaderBytes = (singleSegment ? 0 : 1) + dictionaryBytes + contentSizeBytes;
    if (buffer.length - offset < remainingHeaderBytes) return { frames, tornStart: start };
    offset += remainingHeaderBytes;
    for (;;) {
      if (buffer.length - offset < 3) return { frames, tornStart: start };
      const blockHeader = buffer.readUIntLE(offset, 3);
      offset += 3;
      const lastBlock = (blockHeader & 1) !== 0;
      const blockType = (blockHeader >>> 1) & 3;
      const blockSize = blockHeader >>> 3;
      if (blockType === 3) throw new Error('reserved block type at ' + (offset - 3));
      const payloadBytes = blockType === 1 ? 1 : blockSize;
      if (buffer.length - offset < payloadBytes) return { frames, tornStart: start };
      offset += payloadBytes;
      if (lastBlock) break;
    }
    if (checksum) {
      if (buffer.length - offset < 4) return { frames, tornStart: start };
      offset += 4;
    }
    frames.push({ start, end: offset });
  }
  return { frames };
}

/** 把消息 content 数组压成纯文本。 */
function contentToText(content) {
  if (!content) return '';
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return JSON.stringify(content);
  const out = [];
  for (const b of content) {
    if (!b || typeof b !== 'object') continue;
    if (b.type === 'text' && typeof b.text === 'string') {
      out.push(b.text);
    } else if (b.type === 'file') {
      const name = (b.attachment && b.attachment.name) || '';
      out.push('[附件] ' + name);
    } else if (b.type === 'image') {
      out.push('[图片]');
    } else if (b.type === 'tool-result') {
      out.push(contentToText(b.content));
    } else if (b.type === 'tool-call') {
      if (typeof b.name === 'string') out.push(b.name + '(' + (b.arguments || '') + ')');
    }
  }
  return out.join('\n');
}

/** 把一条会话事件转成扁平的可检索对象，非正文事件返回 null。 */
function eventToRow(ev, sid) {
  if (!ev || typeof ev !== 'object') return null;
  const t = ev.type;
  const d = ev.data || {};
  const base = {
    session_id: sid,
    seq: ev.seq,
    time_ms: ev.time || 0,
    kind: t,
  };
  if (t === 'user/message') {
    const text = contentToText(d.content);
    if (!text) return null;
    return Object.assign(base, { role: 'user', text });
  }
  if (t === 'assistant/message') {
    const msg = d.message || {};
    const text = contentToText(msg.content);
    if (!text) return null;
    const src = msg.source || {};
    return Object.assign(base, {
      role: 'assistant', text,
      model: src.model || '', provider: src.provider || '',
    });
  }
  if (t === 'tool/call') {
    return Object.assign(base, {
      role: 'tool-call',
      text: String(d.name || '') + ' ' + String(d.arguments || ''),
      tool: String(d.name || ''),
    });
  }
  if (t === 'tool/result') {
    const msg = d.message || {};
    const text = contentToText(msg.content);
    if (!text) return null;
    return Object.assign(base, {
      role: 'tool-result', text,
      tool: (d.meta && d.meta.name) || '',
      is_error: !!(msg.content || []).some((b) => b && b.isError),
    });
  }
  if (t === 'session/title') {
    return Object.assign(base, { role: 'title', text: String(d.title || '') });
  }
  if (t === 'system/message') {
    const text = contentToText(d.content);
    if (!text) return null;
    return Object.assign(base, { role: 'system', text });
  }
  if (t === 'session') {
    return Object.assign(base, {
      role: 'session-header',
      text: JSON.stringify({ cwd: ev.cwd, createdAt: ev.createdAt, id: ev.id }),
      header: true,
    });
  }
  return null;
}

function argOf(argv, name, dflt) {
  const i = argv.indexOf(name);
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : dflt;
}

function main() {
  const argv = process.argv;
  const inPath = argOf(argv, '--in');
  const outPath = argOf(argv, '--out');
  if (!inPath || !outPath) {
    console.error('usage: node bridge.js --in <file> --out <file> [--offset N]');
    process.exit(2);
  }
  const offset = Number(argOf(argv, '--offset', '0')) || 0;
  const buf = fs.readFileSync(inPath);
  const slice = offset > 0 ? buf.subarray(offset) : buf;
  const { frames, tornStart } = scanZstdFrames(slice);

  // 会话 id 从头部帧里取
  let sid = '';
  const out = fs.createWriteStream(outPath, { encoding: 'utf8' });
  let written = 0;
  let plaintextBytes = 0;

  const emit = (text) => {
    for (const line of text.split('\n')) {
      if (!line.trim()) continue;
      let ev;
      try { ev = JSON.parse(line); } catch (e) { continue; }
      if (!sid && ev.type === 'session' && ev.id) sid = ev.id;
      const row = eventToRow(ev, sid || 'unknown');
      if (!row) continue;
      out.write(JSON.stringify(row) + '\n');
      written += 1;
    }
  };

  for (const f of frames) {
    let plain;
    try {
      plain = zlib.zstdDecompressSync(slice.subarray(f.start, f.end));
    } catch (e) {
      continue; // 单帧坏了就跳过，其余照读
    }
    plaintextBytes += plain.length;
    emit(plain.toString('utf8'));
  }
  if (tornStart !== undefined) {
    // 撕裂的最后一帧：尽力取出其中完整解码的部分
    try {
      const partial = zlib.zstdDecompressSync(slice.subarray(tornStart), {
        finishFlush: zlib.constants.ZSTD_e_flush,
      });
      emit(partial.toString('utf8'));
    } catch (e) { /* 忽略 */ }
  }
  out.end(() => {
    process.stdout.write(JSON.stringify({
      frames: frames.length, torn: tornStart !== undefined,
      rows: written, plaintext_bytes: plaintextBytes,
      consumed: (tornStart !== undefined ? slice.length : slice.length),
    }));
  });
}

if (require.main === module) {
  try {
    main();
  } catch (e) {
    console.error('bridge error: ' + (e && e.message ? e.message : e));
    process.exit(1);
  }
}

module.exports = { scanZstdFrames, contentToText, eventToRow };
