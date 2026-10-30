#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dshfind 的本地 HTTP 服务 + Edge 应用模式窗口。

为什么有这一层：界面要换成网页（才能真正用上 GSAP），那就需要有人把
Python 这边的检索能力暴露出去。这里起一个只监听 127.0.0.1 的小服务，
前端是纯静态页面，通过 JSON 接口拿结果。

几点设计取舍：
- 只绑 127.0.0.1，并且要求一个每次启动随机生成的 token（放在 URL 里），
  避免本机其他程序随手来调接口。
- 窗口用 Edge 的 `--app` 模式（无地址栏、无标签页），不依赖任何第三方包；
  用独立的 user-data-dir，不碰你日常的 Edge 配置。
- 索引在后台异步加载，页面先出来显示进度，而不是干等十几秒白屏。
"""

from __future__ import annotations

import atexit
import ctypes
import json
import mimetypes
import os
import re
import secrets
import struct
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import dshfind as D

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")

# 单实例互斥体的名字。用环境变量可以给测试/调试开一个"平行实例"，
# 平时不用管它（默认就是全局唯一那一个）。
INSTANCE_NAME = os.environ.get("DSHFIND_INSTANCE", "Local\\dshfind-single-instance")
NO_HOTKEY = os.environ.get("DSHFIND_NO_HOTKEY", "") not in ("", "0")
# 正在跑的实例把端口和 token 记在这儿，`dshfind.py --quit` 靠它找到进程并让它干净退出
INSTANCE_FILE = os.environ.get("DSHFIND_INSTANCE_FILE",
                               os.path.join(APP_DIR, "data", "instance.json"))
EDGE_PROFILE = os.path.join(D.APP_DIR if hasattr(D, "APP_DIR") else APP_DIR, "data", "edgeprofile")

TEXT_EXTS = {
    "txt", "md", "markdown", "json", "yaml", "yml", "toml", "ini", "cfg", "conf",
    "log", "csv", "tsv", "py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "c", "h",
    "cpp", "hpp", "cc", "cs", "java", "go", "rs", "rb", "php", "swift", "kt",
    "lua", "sh", "bash", "ps1", "bat", "cmd", "xml", "html", "htm", "css", "scss",
    "less", "sql", "unity", "prefab", "asset", "meta", "gitignore", "editorconfig",
    "properties", "gradle", "cmake", "makefile", "dockerfile",
}
MAX_PREVIEW = 24_000


class SearchService:
    """把检索引擎包装成一组可直接给 HTTP 用的操作。"""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or D.load_settings()
        self.engine = D.IndexEngine(self.settings)
        self.con = D.db_connect()
        # 两把锁分开：会话库读写和文件索引操作互不相干。
        # 用一把粗锁的话，启动时那几秒的会话增量会把第一次搜索堵住（实测堵到 9 秒）。
        self.lock = threading.RLock()      # 保护 engine 的惰性索引构建
        self.db_lock = threading.RLock()   # 保护 sqlite 连接
        self.ready = threading.Event()
        self.loading = False
        self.load_error = None
        self.load_seconds = None
        self.refreshing = False
        self.refresh_phase = ""
        self.refresh_detail = ""
        self.watcher = None
        self.watch_stats = {"changes": 0, "last": 0, "overflow": 0, "running": False}
        # 监听丢过事件 → 索引可能不完整。默认只提示，不自动全量重扫（第 11 项）
        self.needs_rescan = False
        self.needs_rescan_at = 0
        # 查询代号：界面每敲一个字就发一次搜索，新请求把代号 +1，
        # 还在跑的旧请求看到代号变了就立刻收工。否则它们会排在 self.lock 后面，
        # 最后一个查询要等前面全部跑完 —— 表现就是"打了字却半天没有任何结果"。
        self._search_gen = 0
        self._gen_lock = threading.Lock()

    # -- 文件监听：让索引自动跟着磁盘变化，不用手动刷新 ---------------------
    def start_watcher(self):
        if self.watcher is not None:
            return
        try:
            self.watcher = D.FileWatcher(self.engine, self.settings["roots"],
                                         on_change=self._on_fs_change)
            self.watcher.start()
            self.watch_stats["running"] = True
            print("[dshfind] 已开始监听文件变化（%d 个卷），索引会自动更新"
                  % len(self.settings["roots"]), flush=True)
        except Exception as e:
            self.watch_stats["running"] = False
            print("[dshfind] 文件监听启动失败：%s（仍可手动刷新）" % e, file=sys.stderr)

    def _on_fs_change(self, res):
        self.watch_stats["last"] = D.now_ms()
        if res.get("overflow"):
            # 短时间内变更太多，系统缓冲区溢出、丢了事件 —— 只有全量重扫能纠正。
            # 但重扫要八分钟，而"开机 / 打开程序"这一刻恰好是文件活动最多的时候，
            # 天天自动扫一遍用户会以为"每次都重建"（第 11 项）。
            # 所以默认**不自动扫**：只记一个"索引可能不完整"的提示，让用户自己按按钮。
            self.watch_stats["overflow"] += 1
            self.needs_rescan = True
            self.needs_rescan_at = D.now_ms()
            if self.settings.get("auto_rescan_on_overflow", False):
                now = D.now_ms()
                if now - getattr(self, "_last_overflow_refresh", 0) > 15 * 60 * 1000:
                    self._last_overflow_refresh = now
                    print("[dshfind] 变更过于密集，按设置自动全量重扫（限 15 分钟一次）",
                          file=sys.stderr)
                    self.refresh()
            return
        n = res.get("changed", 0)
        if n:
            self.watch_stats["changes"] += n
            self._save_delta_throttled()

    def _save_delta_throttled(self):
        """把增量落盘（限流到 3 秒一次），免得重启后新建的文件从搜索里消失。"""
        now = D.now_ms()
        if now - getattr(self, "_delta_saved_at", 0) < 3000:
            return
        self._delta_saved_at = now
        try:
            self.engine.save_delta()
        except Exception:
            pass

    # -- 载入 -------------------------------------------------------------
    def load_async(self):
        if self.loading or self.ready.is_set():
            return
        self.loading = True
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        t0 = time.perf_counter()
        try:
            with self.lock:
                ok = self.engine.load()
                if not ok:
                    self.loading = False
                    self.ready.set()
                    self.load_error = "没有索引文件"
                    return
                self.engine.build_pinyin()
                self.load_seconds = time.perf_counter() - t0
            self.loading = False
            self.ready.set()
            # 索引就绪后开始监听文件变化：之后新建/删除/改名都能自动反映到搜索结果
            self.start_watcher()
            # 再顺手刷一次会话标题（毫秒级），然后让它一直定时刷
            threading.Thread(target=self._refresh_sessions, daemon=True).start()
            self.start_session_refresher()
        except Exception as e:
            self.loading = False
            self.load_error = str(e)
            self.ready.set()

    def _refresh_sessions(self):
        try:
            with self.db_lock:
                res = D.index_sessions(self.con, self.settings.get(
                    "session_roots", [D.DSH_SESSIONS]))
            print("[dshfind] 会话增量：更新 %d，新增 %d 条，%.1fs"
                  % (res["updated"], res["rows"], res["seconds"]), flush=True)
        except Exception as e:
            print("[dshfind] 会话增量失败：%s" % e, file=sys.stderr)

    def start_session_refresher(self, interval: float = None):
        """定时把"新会话/新标题"补进会话库，不用重启也不用按 Ctrl+R。

        文件那边有 ReadDirectoryChangesW 顶着，会话这边没有 —— DSH 的新会话
        只是往 ~/.dsh/sessions 里多一个目录，没有任何通知。所以只能每隔一阵子
        扫一遍（`index_sessions` 只读标题，100 多个会话约 0.05 秒，很便宜）。
        """
        if getattr(self, "_session_thread", None) is not None:
            return
        if not self.settings.get("index_sessions", True):
            return
        interval = interval or float(os.environ.get("DSHFIND_SESSION_REFRESH", "30"))
        if interval <= 0:
            return

        def loop():
            while True:
                time.sleep(interval)
                if self.refreshing:      # 正在全量重建，别插队
                    continue
                self._refresh_sessions()

        self._session_thread = threading.Thread(target=loop, daemon=True)
        self._session_thread.start()
        print("[dshfind] 会话索引每 %.0f 秒自动刷新一次（新会话不用重启就能搜到）"
              % interval, flush=True)

    # -- 状态 -------------------------------------------------------------
    def status(self) -> dict:
        with self.db_lock:
            st = D.session_stats(self.con)
            n = len(self.engine.entries)
            files = None
            return {
                "ready": self.ready.is_set() and not self.loading,
                "loading": self.loading,
                "load_error": self.load_error,
                "entries": n,
                "files": self.engine.stats.files if n else 0,
                "dirs": self.engine.stats.dirs if n else 0,
                "noise_on": bool(self.settings.get("exclude_noise", True)),
                "noise_count": getattr(self, "_noise_count", None),
                "sessions": st["sessions"],
                "titled": st.get("titled", 0),
                "titles_only": True,
                "built_at": self.engine.built_at,
                "load_seconds": self.load_seconds,
                "refreshing": self.refreshing,
                "refresh_phase": self.refresh_phase,
                "refresh_detail": self.refresh_detail,
                "version": D.APP_VERSION,
                "hotkey": Handler.hotkey,
                "watch_running": bool(self.watch_stats.get("running")),
                "watch_changes": self.watch_stats.get("changes", 0),
                "watch_last": self.watch_stats.get("last", 0),
                "watch_overflow": self.watch_stats.get("overflow", 0),
                # 监听丢过事件时给界面一个提示，扫不扫由用户点按钮决定
                "needs_rescan": bool(self.needs_rescan),
                "needs_rescan_at": self.needs_rescan_at,
            }

    # -- 搜索 -------------------------------------------------------------
    def search_files(self, text: str, limit: int = 1000, noise: bool | None = None) -> dict:
        t0 = time.perf_counter()
        with self._gen_lock:
            self._search_gen += 1
            my_gen = self._search_gen

        def stale() -> bool:
            return my_gen != self._search_gen

        with self.lock:
            if stale():
                return {"hits": [], "candidates": 0, "ms": 0, "total_ms": 0,
                        "diagnostics": [], "parsed": "", "aborted": True}
            q = D.parse_query(text)
            use_noise = (not self.settings.get("exclude_noise", True)) if noise is None \
                else (not noise)
            try:
                hits, dt, cand = D.run_query(self.engine, q, limit=limit,
                                             exclude_noise=not use_noise,
                                             should_abort=stale)
            except D.QueryAborted:
                # 用户又敲了一个字，这个查询不必再算了。前端那时也已经把请求取消了。
                return {"hits": [], "candidates": 0, "ms": 0, "total_ms": 0,
                        "diagnostics": [], "parsed": "", "aborted": True}
            out = []
            for i, h in enumerate(hits):
                if (i & 63) == 0 and stale():
                    return {"hits": [], "candidates": 0, "ms": 0, "total_ms": 0,
                            "diagnostics": [], "parsed": "", "aborted": True}
                e = h.entry
                span = None
                if h.span:
                    span = {"s": h.span[0], "e": h.span[1], "kind": h.span[2],
                            "text": D.span_text(e, h.span)}
                disp = D.actual_path(e.path)   # 只算一次（它会扫目录，别重复调）
                out.append({
                    "path": e.path,
                    "disp": disp,
                    "name": disp.rsplit("\\", 1)[-1],
                    "is_dir": bool(e.is_dir),
                    "size": int(e.size),
                    "mtime": int(e.mtime_ms),
                    "kind": e.kind if not e.is_dir else "folder",
                    "ext": e.ext,
                    "score": int(h.score),
                    "span": span,
                    "noise": bool(e.noise),
                })
            return {
                "hits": out,
                "candidates": int(cand),
                "ms": round(dt, 1),
                "total_ms": round((time.perf_counter() - t0) * 1000, 1),
                "diagnostics": q.diagnostics,
                "parsed": q.describe(),
            }

    def search_sessions(self, text: str, role: str = "", limit: int = 500) -> dict:
        """「对话」模式：**只搜会话标题**（按需求不搜正文）。"""
        with self.db_lock:
            hits, dt = D.search_sessions(self.con, text, limit=limit)
            return {
                "hits": [{
                    "sid": h.sid, "title": h.title, "snippet": h.snippet,
                    "time": h.time_ms, "cwd": h.cwd, "proj": h.proj_dir,
                    "score": h.score, "log_path": h.log_path,
                } for h in hits],
                "ms": round(dt, 1),
                "titles_only": True,
            }

    def context(self, log_path: str = "", limit: int = 40) -> dict:
        """预览：按需解压这个会话的日志，取开头若干条事件。

        搜索阶段不再读正文，所以这里才真正调用 bridge.js —— 一次只解一个会话。
        """
        res = D.session_context(log_path, limit=limit)
        return {"ok": res.get("ok", False), "events": res.get("events", []),
                "total": res.get("total", 0), "error": res.get("error")}

    def preview(self, path_low: str) -> dict:
        """文件预览。path_low 是索引里的小写路径。"""
        with self.lock:
            entry = self.engine.by_key.get(path_low) if self.engine._by_key_ready else None
            if entry is None:
                # 不建全量字典（几百万条太贵），直接按小写路径回查
                self.engine.ensure_by_key()
                entry = self.engine.by_key.get(path_low)
        if entry is None:
            return {"ok": False, "error": "索引里没有这个路径"}
        disp = D.actual_path(entry.path)
        info = {
            "ok": True, "disp": disp, "name": disp.rsplit("\\", 1)[-1],
            "is_dir": bool(entry.is_dir), "size": int(entry.size),
            "mtime": int(entry.mtime_ms),
            "kind": entry.kind if not entry.is_dir else "folder",
            "initials": None, "pinyin": None, "text": None, "text_truncated": False,
        }
        if not entry.name.isascii():
            try:
                info["initials"] = D._get_initials(entry)
                info["pinyin"] = D._get_full(entry)
            except Exception:
                pass
        if not entry.is_dir and entry.ext in TEXT_EXTS and entry.size <= 2 * 1024 * 1024:
            try:
                with open(disp, "r", encoding="utf-8", errors="replace") as f:
                    head = f.read(MAX_PREVIEW + 1)
                info["text_truncated"] = len(head) > MAX_PREVIEW
                info["text"] = head[:MAX_PREVIEW]
            except OSError as e:
                info["text"] = None
                info["error"] = str(e)
        return info

    # -- 动作 -------------------------------------------------------------
    def open_path(self, path_low: str) -> dict:
        disp = D.actual_path(path_low)
        try:
            os.startfile(disp)
            return {"ok": True}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def reveal(self, path_low: str) -> dict:
        disp = D.actual_path(path_low)
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(disp)])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def toggle_noise(self) -> dict:
        with self.lock:
            cur = bool(self.settings.get("exclude_noise", True))
            self.settings["exclude_noise"] = not cur
            try:
                D.save_settings(self.settings)
            except Exception:
                pass
            return {"ok": True, "noise_on": self.settings["exclude_noise"]}

    def refresh(self) -> dict:
        if self.refreshing:
            return {"ok": False, "error": "正在刷新"}
        self.refreshing = True
        threading.Thread(target=self._refresh_all, daemon=True).start()
        return {"ok": True}

    def _refresh_all(self):
        try:
            def prog(root, files, dirs, el):
                self.refresh_phase = "scan"
                self.refresh_detail = "%s · %s 文件" % (root, "{:,}".format(files))

            self.refresh_phase = "scan"
            stats = self.engine.build(self.settings["roots"],
                                      budget=self.settings.get("per_root_budget"),
                                      on_progress=prog)
            self.refresh_phase = "index"
            self.refresh_detail = "重建拼音/排序索引"
            with self.lock:
                self.engine.build_pinyin()
                try:
                    self.engine.build_sorted()
                    D.sorted_save_stamp(self.engine)
                except Exception as e:
                    print("[dshfind] 排序索引重建失败：%s" % e, file=sys.stderr)
                self.engine.save()
            if D.fts_is_stale(self.engine):
                self.refresh_phase = "fts"
                self.refresh_detail = "重建名字索引（约 1～2 分钟）"
                D.fts_build(self.engine)
                D._fts_ok_for = None
            self.refresh_phase = "sessions"
            self.refresh_detail = "刷新会话索引"
            self._refresh_sessions()
            self.refresh_phase = "done"
            self.refresh_detail = "完成"
            self.needs_rescan = False        # 重扫完成了，"不完整"的提示可以收起来
        except Exception as e:
            self.refresh_phase = "error"
            self.refresh_detail = str(e)
            traceback.print_exc()
        finally:
            self.refreshing = False


def quit_app():
    """真的退出：先关掉应用窗口、把增量落盘，再结束进程。

    注意 `os._exit()` **不会**触发 atexit，所以增量落盘要在这里手动做一次，
    否则这次运行期间新建的文件重启后会"消失"。窗口也要自己关 ——
    Windows 上父进程退出不会带走子进程，Edge 窗口会留在那儿显示"连不上后端"。
    """
    win = getattr(Handler, "window", None)
    if win is not None:
        try:
            if win.proc is not None and win.proc.poll() is None:
                win.proc.terminate()
        except Exception:
            pass
    svc = getattr(Handler, "service", None)
    if svc is not None:
        try:
            svc.engine.save_delta()
        except Exception:
            pass
    os._exit(0)


def _sniff_image(blob: bytes) -> str | None:
    """按文件头判断图片格式，返回扩展名；不认识的返回 None。

    不看用户给的文件名 —— 浏览器里 `accept="image/*"` 也可能拿不到名字，
    而且"改扩展名骗服务器"这种小事没必要防着用户，但至少要存成能正确显示的类型。
    """
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if blob[:3] == b"\xff\xd8\xff":
        return "jpg"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    if blob[:4] == b"\x00\x00\x01\x00":
        return "ico"
    head = blob[:400].lstrip()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "svg"
    return None


def build_ico(entries) -> bytes:
    """把若干张 PNG 打成一个 .ico（Vista 以后 ICO 允许直接放 PNG 数据）。

    entries: [(边长, png_bytes)]，边长 1～256，正方形。
    为什么要自己打包：任务栏/标题栏那个位置只有 16～32 像素，而用户给的常常是
    几百上千像素的图。浏览器把大图压到 16px 又小又糊；如果能按 16/24/32… 逐档
    预先缩好塞进 ICO，系统就能直接挑最合适的一档，边缘会清楚很多。
    """
    entries = sorted([(int(s), d) for s, d in entries if 1 <= int(s) <= 256])
    if not entries:
        raise ValueError("没有可用的尺寸")
    head = struct.pack("<HHH", 0, 1, len(entries))       # reserved, type=icon, count
    offset = 6 + 16 * len(entries)
    dirs, blobs = [], []
    for size, data in entries:
        w = 0 if size >= 256 else size                   # 256 在 ICO 里记作 0
        dirs.append(struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset))
        blobs.append(data)
        offset += len(data)
    return head + b"".join(dirs) + b"".join(blobs)


def ico_sizes(blob: bytes):
    """读出 .ico 里记录的各档边长（给状态/测试用）。"""
    try:
        if len(blob) < 6 or blob[:4] != b"\x00\x00\x01\x00":
            return []
        n = struct.unpack("<H", blob[4:6])[0]
        out = []
        for i in range(n):
            b = 6 + i * 16
            if b + 16 > len(blob):
                break
            w = blob[b]
            out.append(256 if w == 0 else w)
        return out
    except Exception:
        return []


def _icon_file(exts):
    for ext in exts:
        p = os.path.join(APP_DIR, "data", "appicon." + ext)
        if os.path.isfile(p):
            return p
    return None


def all_icon_files():
    """data 下所有可能存在的图标文件（换图/恢复默认时要一起清掉）。"""
    out = []
    for ext in ("ico", "png", "jpg", "jpeg", "webp", "gif", "svg"):
        p = os.path.join(APP_DIR, "data", "appicon." + ext)
        if os.path.isfile(p):
            out.append(p)
    return out


def favicon_path():
    """浏览器要的那个：ICO 优先（里面有多档尺寸），其它格式兜底。"""
    return _icon_file(("ico", "png", "jpg", "jpeg", "webp", "gif", "svg"))


def icon_path():
    """界面预览要的那个：PNG 优先（ICO 在预览框里会糊）。"""
    return _icon_file(("png", "ico", "jpg", "jpeg", "webp", "gif", "svg"))


def icon_version() -> str:
    p = icon_path()
    try:
        return str(int(os.path.getmtime(p) * 1000)) if p else "0"
    except OSError:
        return "0"


def appearance(cfg: dict | None = None) -> dict:
    """当前的外观设置：标题 + 自定义图标（给前端和 /api/appearance 用）。"""
    cfg = cfg if cfg is not None else D.load_settings()
    p = icon_path()
    fav = favicon_path()
    info = None
    if p:
        info = {
            "url": "/appicon?v=" + icon_version(),
            "ext": os.path.splitext(p)[1].lstrip(".").lower(),
            "bytes": os.path.getsize(p),
            "sizes": [],
        }
        if fav and fav.lower().endswith(".ico"):
            try:
                info["sizes"] = ico_sizes(open(fav, "rb").read())
            except OSError:
                info["sizes"] = []
    return {"title": (cfg.get("app_title") or "").strip(),
            "default_title": "dshfind", "icon": info}


class Handler(BaseHTTPRequestHandler):
    server_version = "dshfind/1.0"
    service: SearchService = None
    token: str = ""
    hotkey: str = ""
    window = None          # AppWindow：/api/quit 时要把它一起关掉
    window_title = "dshfind"

    def log_message(self, fmt, *args):
        pass          # 静音；访问日志对单机工具没意义

    # -- 工具 -------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _authorized(self, qs) -> bool:
        tok = self.headers.get("X-Dsh-Token") or (qs.get("t", [""])[0])
        return bool(tok) and secrets.compare_digest(tok, self.token)

    def _args(self):
        u = urlparse(self.path)
        return u.path, parse_qs(u.query)

    # -- 路由 -------------------------------------------------------------
    def do_GET(self):
        path, qs = self._args()
        try:
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            if path in ("/favicon.ico", "/appicon"):
                p = favicon_path() if path == "/favicon.ico" else icon_path()
                if not p:
                    return self._send(204, b"", "image/x-icon")
                ctype = mimetypes.guess_type(p)[0] or "image/png"
                with open(p, "rb") as f:
                    data = f.read()
                return self._send(200, data, ctype)
            if not path.startswith("/api/"):
                return self._send(404, b"not found", "text/plain")
            if not self._authorized(qs):
                return self._json({"error": "bad token"}, 403)
            return self._api(path, qs)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        path, qs = self._args()
        try:
            if not self._authorized(qs):
                return self._json({"error": "bad token"}, 403)
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                body = {}
            svc = self.service
            if path == "/api/open":
                return self._json(svc.open_path(body.get("path", "")))
            if path == "/api/reveal":
                return self._json(svc.reveal(body.get("path", "")))
            if path == "/api/toggle-noise":
                return self._json(svc.toggle_noise())
            if path == "/api/refresh":
                return self._json(svc.refresh())
            if path == "/api/quit":
                self._json({"ok": True})
                threading.Timer(0.4, quit_app).start()
                return None
            if path == "/api/appearance":
                return self._json(self._save_appearance(body))
            if path == "/api/appearance/icon":
                return self._json(self._save_icon(body))
            if path == "/api/appearance/icon/reset":
                return self._json(self._reset_icon())
            if path == "/api/focus":
                win = Handler.window
                if win is None:
                    return self._json({"ok": False,
                                       "error": "没有窗口（--no-window 启动的）"})
                # 提到前台会调 SetForegroundWindow，放到线程里做，别卡住这次响应
                threading.Thread(target=win.focus, daemon=True).start()
                return self._json({"ok": True})
            return self._json({"error": "unknown api"}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def _api(self, path, qs):
        svc = self.service

        def arg(name, default=""):
            return qs.get(name, [default])[0]

        if path == "/api/status":
            return self._json(svc.status())
        if path == "/api/ping":
            return self._json({"ok": True})
        if path == "/api/appearance":
            return self._json(appearance())
        if path == "/api/search":
            if not svc.ready.is_set():
                return self._json({"hits": [], "loading": True, "candidates": 0,
                                   "ms": 0, "total_ms": 0, "diagnostics": [],
                                   "parsed": ""})
            limit = max(1, min(int(arg("limit", "1000") or 1000), 5000))
            noise = arg("noise", "")
            noise_flag = None if noise == "" else (noise == "1")
            return self._json(svc.search_files(arg("q"), limit=limit, noise=noise_flag))
        if path == "/api/session":
            if not svc.ready.is_set():
                return self._json({"hits": [], "loading": True, "ms": 0})
            limit = max(1, min(int(arg("limit", "500") or 500), 2000))
            return self._json(svc.search_sessions(arg("q"), role=arg("role"),
                                                 limit=limit))
        if path == "/api/context":
            return self._json(svc.context(arg("log_path"),
                                          int(arg("limit", "40") or 40)))
        if path == "/api/preview":
            return self._json(svc.preview(arg("path")))
        return self._json({"error": "unknown api"}, 404)

    # -- 外观（改名字 / 换图标） -------------------------------------------
    def _save_appearance(self, body: dict) -> dict:
        """改应用名字。空字符串 = 恢复默认（dshfind）。

        窗口标题会立刻变：前端把 document.title 设成同一个字符串，
        Edge 的 --app 窗口标题是跟着 document.title 走的。
        """
        title = (body.get("title") or "").strip()
        if len(title) > 40:
            return {"ok": False, "error": "名字最多 40 个字"}
        if "\n" in title or "\r" in title:
            return {"ok": False, "error": "名字不能换行"}
        cfg = D.load_settings()
        cfg["app_title"] = title
        try:
            D.save_settings(cfg)
        except Exception as e:
            return {"ok": False, "error": "写配置失败：%s" % e}
        Handler.window_title = title or "dshfind"
        if Handler.window is not None:
            try:
                Handler.window.title = Handler.window_title
            except Exception:
                pass
        return {"ok": True, "appearance": appearance(cfg)}

    def _save_icon(self, body: dict) -> dict:
        """换图标。

        两种来源，前端优先用第一种：
        1. `sizes`：{"16": "<base64 png>", "32": …, "256": …} —— 前端用 canvas 把原图
           **中心裁成正方形**再逐档缩小，服务端把它们打成一个多尺寸 ICO。
           标题栏/任务栏只有 16～32 像素，预先缩好的档位比浏览器挤大图清楚得多。
        2. `data`：单张图片（老的调用方式、命令行脚本用），原样存下来。
        """
        import base64

        def b64(text):
            if "," in text[:80] and text.lstrip().startswith("data:"):
                text = text.split(",", 1)[1]
            return base64.b64decode(text, validate=False)

        sizes = body.get("sizes") or None
        if sizes:
            pairs = []
            for key, val in sizes.items():
                try:
                    size = int(key)
                except (TypeError, ValueError):
                    continue
                if not (1 <= size <= 256):
                    continue
                try:
                    blob = b64(val)
                except Exception:
                    continue
                if _sniff_image(blob) != "png":
                    continue
                pairs.append((size, blob))
            if not pairs:
                return {"ok": False, "error": "没有可用的尺寸（需要 PNG）"}
            try:
                ico = build_ico(pairs)
            except Exception as e:
                return {"ok": False, "error": "打包 ICO 失败：%s" % e}
            biggest = max(pairs, key=lambda x: x[0])[1]
            for p in self._all_icon_files():
                try:
                    os.unlink(p)
                except OSError:
                    pass
            try:
                with open(os.path.join(APP_DIR, "data", "appicon.ico"), "wb") as f:
                    f.write(ico)
                with open(os.path.join(APP_DIR, "data", "appicon.png"), "wb") as f:
                    f.write(biggest)          # 预览/兜底用
            except Exception as e:
                return {"ok": False, "error": "写图标失败：%s" % e}
            cfg = D.load_settings()
            cfg["app_icon_ext"] = "ico"
            try:
                D.save_settings(cfg)
            except Exception:
                pass
            return {"ok": True, "appearance": appearance(cfg)}

        raw = body.get("data") or ""
        try:
            blob = b64(raw)
        except Exception as e:
            return {"ok": False, "error": "图片数据解不开：%s" % e}
        if not blob:
            return {"ok": False, "error": "没有拿到图片数据"}
        if len(blob) > 1_500_000:
            return {"ok": False, "error": "图片太大了（上限 1.5 MB）"}
        ext = _sniff_image(blob)
        if ext is None:
            return {"ok": False, "error": "只支持 PNG / JPEG / ICO / GIF / WebP / SVG"}
        # 先删掉旧格式的，避免 icon_path() 拿到过期的那个
        for p in self._all_icon_files():
            try:
                os.unlink(p)
            except OSError:
                pass
        dst = os.path.join(APP_DIR, "data", "appicon." + ext)
        try:
            with open(dst, "wb") as f:
                f.write(blob)
        except Exception as e:
            return {"ok": False, "error": "写图标失败：%s" % e}
        cfg = D.load_settings()
        cfg["app_icon_ext"] = ext
        try:
            D.save_settings(cfg)
        except Exception:
            pass
        return {"ok": True, "appearance": appearance(cfg)}

    @staticmethod
    def _all_icon_files():
        return all_icon_files()

    def _reset_icon(self) -> dict:
        for p in self._all_icon_files():
            try:
                os.unlink(p)
            except OSError:
                pass
        cfg = D.load_settings()
        cfg.pop("app_icon_ext", None)
        try:
            D.save_settings(cfg)
        except Exception:
            pass
        return {"ok": True, "appearance": appearance(cfg)}

    def _serve_static(self, rel: str):
        rel = rel.replace("\\", "/").lstrip("/")
        if ".." in rel.split("/"):
            return self._send(403, b"forbidden", "text/plain")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.normpath(WEB_DIR)) or not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            data = f.read()
        self._send(200, data, ctype)


def find_edge() -> str | None:
    cands = [
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


class AppWindow:
    """用 Edge 的 --app 模式承载网页界面。

    独立 user-data-dir：不碰你日常的 Edge 配置，也保证窗口里只有这一个页面。
    热键触发时负责把窗口重新提到前台（必要时重启）。
    """

    def __init__(self, url: str, width: int = 1180, height: int = 800,
                 title: str = ""):
        # identity 只是内部标记；title 才是窗口标题（用户可以在界面里改）
        self.identity = "dshfind-%s" % secrets.token_hex(3)
        self.title = (title or "").strip() or self.identity
        # 把窗口标题也传进去：前端会用它做 document.title
        from urllib.parse import quote
        self.url = url + (("&" if "?" in url else "?") + "w=" + quote(self.title))
        self.width = width
        self.height = height
        self.edge = find_edge()
        self.proc = None
        self.hwnd = None

    def _args(self):
        return [
            self.edge,
            "--app=%s" % self.url,
            "--user-data-dir=%s" % EDGE_PROFILE,
            "--window-size=%d,%d" % (self.width, self.height),
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=msEdgeSidebarV2,msEdgeTranslate",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-component-update",
        ]

    def launch(self) -> bool:
        if not self.edge:
            return False
        if self.proc is not None and self.proc.poll() is None:
            return True
        try:
            os.makedirs(EDGE_PROFILE, exist_ok=True)
            self.proc = subprocess.Popen(self._args(), close_fds=True)
            return True
        except Exception as e:
            print("[dshfind] 启动 Edge 失败：%s" % e, file=sys.stderr)
            return False

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def focus(self):
        """把窗口提到前台（找不到窗口才重新拉起）。

        顺序很重要：先找**已经存在的窗口**，最后才 launch。
        以前是"进程不 alive 就直接 launch" —— 而 Edge 的 launcher 进程在把窗口交给
        已有浏览器进程后会退出，于是 `alive()` 变 False，一点热键/快捷方式就又开一个
        窗口（同一时间两个窗口，标题一模一样）。第 14 项就是被这个坑到。
        """
        import ctypes
        u = ctypes.windll.user32

        if self.hwnd and u.IsWindow(self.hwnd):
            self._bring(self.hwnd)
            return
        hwnd = self._find_window_by_pid() or self._find_window_by_title()
        if hwnd:
            self.hwnd = hwnd
            self._bring(hwnd)
            return
        if not self.alive():
            self.launch()

    def _find_window_by_title(self):
        """按窗口标题找自己的窗口（标题就是用户设的应用名字）。"""
        import ctypes
        import ctypes.wintypes as wt
        u = ctypes.windll.user32
        want = (self.title or "").strip().lower()
        if not want:
            return None
        hits = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def cb(hwnd, _lp):
            if not u.IsWindowVisible(hwnd):
                return True
            n = u.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value.strip().lower() == want:
                    hits.append(hwnd)
            return True

        u.EnumWindows(cb, 0)
        return hits[0] if hits else None

    def _bring(self, hwnd):
        """把窗口提到前台并给键盘焦点。

        Windows 有"前台锁"：本进程不是当前前台进程时，SetForegroundWindow
        往往只会闪一下任务栏而不真的切过去。标准解法是临时把本线程的输入队列
        attach 到当前前台窗口所在线程，再调 SetForegroundWindow。
        """
        import ctypes
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        SW_RESTORE = 9
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, SW_RESTORE)

        fg = u.GetForegroundWindow()
        tid_cur = k.GetCurrentThreadId()
        tid_fg = u.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        if tid_fg and tid_fg != tid_cur:
            attached = bool(u.AttachThreadInput(tid_cur, tid_fg, True))
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
            u.SetFocus(hwnd)
        finally:
            if attached:
                u.AttachThreadInput(tid_cur, tid_fg, False)

        # 极短暂"置顶"再取消，避免被其它置顶窗口压住（不改变正常层级）
        SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
        u.SetWindowPos(hwnd, -1, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)
        u.SetWindowPos(hwnd, -2, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)

    def _find_window_by_pid(self):
        import ctypes
        import ctypes.wintypes as wt
        u = ctypes.windll.user32
        pids = set()
        if self.proc is not None:
            pids.add(self.proc.pid)
            try:
                out = subprocess.run(
                    ["wmic", "process", "where", "ParentProcessId=%d" % self.proc.pid,
                     "get", "ProcessId"], capture_output=True, text=True, timeout=5)
                for line in out.stdout.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        pids.add(int(line))
            except Exception:
                pass
        found = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def cb(hwnd, _lp):
            pid = wt.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and u.IsWindowVisible(hwnd):
                length = u.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    u.GetWindowTextW(hwnd, buf, length + 1)
                    cls = ctypes.create_unicode_buffer(256)
                    u.GetClassNameW(hwnd, cls, 256)
                    found.append((hwnd, buf.value, cls.value))
            return True

        u.EnumWindows(cb, 0)
        if not found:
            return None
        # 优先要"看起来像应用窗口"的那个（有标题、不是工具窗口）
        for hwnd, title, cls in found:
            if title.strip():
                return hwnd
        return found[0][0]


_INSTANCE_MUTEX = None


def acquire_single_instance(name: str = None) -> bool:
    """同一个用户会话里只允许跑一个 dshfind。

    多开的代价不只是内存翻倍（每个实例 2.7 GB）：后启动的那个抢不到热键，
    于是顺着候选链往下换，最后把 settings.json 里的热键改写掉 —— 实测热键从
    ctrl+shift+d 变成 g、又变成 k，用户按哪个都不对（第 8 项）。

    用命名互斥体判断：进程退出时系统自动释放，不会留下"僵尸锁文件"。
    name 不给时用 INSTANCE_NAME；测试可以用环境变量 DSHFIND_INSTANCE 换个名字。
    """
    global _INSTANCE_MUTEX
    name = name or INSTANCE_NAME
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # argtypes 必须写：不声明的话 ctypes 不知道第三个参数是 wchar_t*，
        # 传 str 会直接抛 TypeError，被下面的 except 吞掉，互斥体形同虚设（踩过一次）。
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k32.CreateMutexW.restype = ctypes.c_void_p
        h = k32.CreateMutexW(None, 0, name)
        _INSTANCE_MUTEX = h          # 一直持有到进程结束
        return ctypes.get_last_error() != 183   # ERROR_ALREADY_EXISTS
    except Exception as e:
        print("[dshfind] 单实例检查失败（不影响使用）：%s" % e, file=sys.stderr)
        return True                  # 判断不出来就放行，别把人挡在门外


def serve(port: int = 0, open_window: bool = True, settings: dict | None = None):
    """起服务（可选开窗口），返回 (httpd, win, url)。给 dshfind.py 和调试复用。

    已有实例在跑时返回 (None, None, "")，调用方据此退出。
    """
    if not acquire_single_instance():
        print("[dshfind] 已经有一个 dshfind 在跑了（同一时间只允许一个实例）。",
              file=sys.stderr)
        return None, None, ""
    settings = settings or D.load_settings()
    svc = SearchService(settings)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    real_port = httpd.server_address[1]
    token = secrets.token_urlsafe(16)
    Handler.service = svc
    Handler.token = token

    url = "http://127.0.0.1:%d/?t=%s" % (real_port, token)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    svc.load_async()
    print("[dshfind] 界面地址 %s" % url, flush=True)

    win = AppWindow(url, title=settings.get("app_title", ""))
    Handler.window_title = win.title
    Handler.window = win

    # 记一下"我是谁"，好让 `dshfind.py --quit`（或双击「退出 dshfind.bat」）找到我
    try:
        with open(INSTANCE_FILE, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "port": real_port, "token": token,
                       "started": D.now_ms(), "url": url}, f)
    except Exception as e:
        print("[dshfind] 记下实例信息失败：%s" % e, file=sys.stderr)

    def _forget_instance():
        try:
            with open(INSTANCE_FILE, "r", encoding="utf-8") as f:
                if json.load(f).get("pid") != os.getpid():
                    return
        except Exception:
            return
        try:
            os.unlink(INSTANCE_FILE)
        except OSError:
            pass

    atexit.register(_forget_instance)

    # 配置的键可能被别的程序占用。这台机器上实测 Shift+Ctrl+F 以及一批
    # Ctrl+Alt+字母 都被别家软件注册了（RegisterHotKey 报 1409）。
    # 所以依次尝试备选，把第一个能注册的写回配置，下次启动就直接用它。
    #
    # ⚠️ 注册和轮询必须在**同一个线程**里：RegisterHotKey(None, ...) 把热键消息
    # 投给"调用它的那个线程"的消息队列，PeekMessageW 也只取本线程的队列。
    # 以前是主线程注册、poll_hotkey 子线程轮询 —— 子线程永远收不到 WM_HOTKEY，
    # 按键毫无反应（而自检里注册和轮询都在一个线程，所以一直是绿的，见第 8 项）。
    want = settings.get("hotkey", "shift+ctrl+f")
    candidates = [want, "ctrl+shift+g", "ctrl+alt+f", "ctrl+shift+d",
                  "ctrl+shift+k", "ctrl+alt+g", "shift+ctrl+space", "ctrl+shift+j"]
    hk_box = {"active": None, "hk": None, "error": ""}
    hk_ready = threading.Event()

    def run_hotkey():
        if NO_HOTKEY:
            hk_ready.set()
            return
        for cand in candidates:
            h = D.GlobalHotkey(lambda: None)
            if h.register(cand):          # 注册在本线程，轮询也在本线程 ✅
                hk_box["active"], hk_box["hk"] = cand, h
                break
            hk_box["error"] = h.error
        hk_ready.set()
        hk = hk_box["hk"]
        if hk is None:
            return
        while True:
            try:
                if hk.poll():
                    win.focus()
            except Exception:
                pass
            time.sleep(0.05)

    threading.Thread(target=run_hotkey, daemon=True).start()
    hk_ready.wait(5.0)          # 等注册结果，好让界面右上角显示真实生效的键

    active = hk_box["active"]
    if active:
        Handler.hotkey = active
        if active != want:
            settings["hotkey"] = active
            try:
                D.save_settings(settings)
            except Exception:
                pass
            print("[dshfind] 热键 %s 被占用，改用 %s 并已记入 settings.json"
                  % (want, active), flush=True)
        else:
            print("[dshfind] 全局热键已注册：%s" % active, flush=True)
    else:
        Handler.hotkey = ""
        if NO_HOTKEY:
            print("[dshfind] 按环境变量要求跳过热键注册（调试用）", file=sys.stderr)
        else:
            print("[dshfind] 所有候选热键都被占用（最后一次：%s），只能手动打开窗口：%s"
                  % (hk_box["error"], win.url), file=sys.stderr)

    def _flush_delta():
        try:
            svc.engine.save_delta()
        except Exception:
            pass

    atexit.register(_flush_delta)

    if open_window:
        if not win.launch():
            print("[dshfind] 没找到 Edge，改用默认浏览器打开", file=sys.stderr)
            try:
                os.startfile(win.url)
            except OSError:
                print("[dshfind] 请手动在浏览器打开上面的地址", file=sys.stderr)
    return httpd, win, url


def main():
    import argparse
    ap = argparse.ArgumentParser(description="dshfind 的网页界面服务")
    ap.add_argument("--port", type=int, default=0, help="固定端口（默认随机）")
    ap.add_argument("--no-window", action="store_true", help="只起服务，不开窗口")
    args = ap.parse_args()

    httpd, win, url = serve(port=args.port, open_window=not args.no_window)
    if httpd is None:
        return
    print("[dshfind] 服务已就绪，Ctrl+C 退出", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[dshfind] 退出")


if __name__ == "__main__":
    main()
