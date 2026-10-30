#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dshfind —— Windows 下的文件与会话检索小工具。

设计参照 oil-oil/oil-find（MIT，https://github.com/oil-oil/oil-find）：
全部文件名常驻内存、边输入边实时收敛、拼音首字母检索、`ext:`/`kind:`/`size:`/`dm:`
一套查询语法。Windows 侧的差异只有两处——文件系统监听与拼音数据源，
详见 README.md 的「与 oil-find 的对照」。

除了文件，本工具还能检索 DeepSeek Harness 的会话记录
（`~/.dsh/sessions/**/session.v*.jsonl.zstd`，一种拼接的 zstd 多帧日志）。

用法：
    python dshfind.py                # 启动网页界面（本地服务 + Edge 应用窗口）
    python dshfind.py --reindex      # 强制重建文件索引 + 名字索引 + 排序索引
    python dshfind.py --fts          # 只重建名字索引
    python dshfind.py --text "参数"   # 不启动界面，终端里跑一次查询
    python dshfind.py --sessions     # 增量刷新会话标题
    python dshfind.py --selftest     # 跑一遍内置自检
"""

from __future__ import annotations

import base64
import array
import bisect
import ctypes
import ctypes.wintypes as wintypes
import fnmatch
import gzip
import heapq
import itertools
import json
import marshal
import os
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta

APP_NAME = "dshfind"
APP_VERSION = "0.1.0"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
INDEX_PATH = os.path.join(DATA_DIR, "fileindex.jsonl.gz")
DB_PATH = os.path.join(DATA_DIR, "dshfind.db")
PINYIN_PATH = os.path.join(APP_DIR, "pinyindata.pyz")

DSH_HOME = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
DSH_SESSIONS = os.path.join(DSH_HOME, "sessions")
DSH_PROJCACHE = os.path.join(DSH_HOME, "storages", "session_projcache", "sessions")

# 索引范围（可在 data/settings.json 里覆盖）
DEFAULT_ROOTS = ["C:\\", "D:\\", "E:\\", "F:\\"]
# 单个盘的最长扫描时间，防止某个慢盘拖住整体
ROOT_SCAN_BUDGET = 240.0
# 条目总数上限。纯 Python 条目约 150~250 字节/条，外加路径文本，
# 400 万条大约 4~6 GB 常驻内存；到上限就停，避免把机器拖垮。
MAX_ENTRIES = 4_000_000

HOTKEY_MOD_ALT, HOTKEY_MOD_CONTROL, HOTKEY_MOD_SHIFT, HOTKEY_MOD_WIN = 0x1, 0x2, 0x4, 0x8
HOTKEY_MOD_NOREPEAT = 0x4000

# 目录噪音：命中祖先目录名就不再往下展开（等价 oil-find 的 noise 继承标记）
NOISE_DIR_NAMES = {
    "node_modules", ".git", ".svn", ".hg", "__pycache__", ".venv", "venv",
    ".idea", ".vs", ".vscode", "obj", "binaries", "deriveddatacache",
    "library", "temp", "tmp", "cache", "caches", ".cache", ".gradle",
    ".m2", ".nuget", ".npm", ".cargo", "packages", "site-packages",
    "dist-info", ".next", ".nuxt", ".turbo", ".parcel-cache", "target",
    "winxsx", "winsxs", "installer", "assembly", "servicing", "driverstore",
    "$recycle.bin", "system volume information", "recovery", "softwaredistribution",
    "msocache", "onedrivetemp", ".dsh-session-archive", "logs", "log",
}
NOISE_PATH_MARKERS = (
    "\\appdata\\local\\temp\\",
    "\\appdata\\local\\microsoft\\windows\\inetcache\\",
    "\\appdata\\local\\packages\\",
    "\\windows\\winsxs\\",
    "\\windows\\servicing\\",
    "\\windows\\softwaredistribution\\",
    "\\windows\\installer\\",
    "\\windows\\assembly\\",
    "\\programdata\\microsoft\\windows\\",
    "\\node_modules\\",
    "\\system volume information\\",
    "$recycle.bin",
)

# 合成一条正则来判噪音：比在 Python 里对 19 个标记逐个做子串查找快一个数量级
# （213 万条目 × 19 次查找 = 4500 万次调用，实测要多花十几秒）。
NOISE_MARKER_RX = re.compile("|".join(re.escape(m) for m in NOISE_PATH_MARKERS))

# 文件类型分类（对齐 oil-find 的 kind: 名字表）
KIND_EXT = {
    "image": {"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "ico", "tif", "tiff",
              "heic", "heif", "avif", "raw", "psd", "ai", "sketch", "fig"},
    "video": {"mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg",
              "ts", "rmvb", "rm", "3gp", "vob"},
    "audio": {"mp3", "wav", "flac", "aac", "ogg", "m4a", "wma", "ape", "opus", "mid", "midi"},
    "doc": {"txt", "md", "markdown", "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
            "csv", "rtf", "odt", "ods", "odp", "epub", "mobi", "tex", "log", "ini", "cfg",
            "json", "yaml", "yml", "toml", "xml", "html", "htm"},
    "code": {"c", "h", "cpp", "hpp", "cc", "cs", "java", "py", "pyw", "js", "jsx", "ts",
             "tsx", "mjs", "cjs", "go", "rs", "rb", "php", "swift", "kt", "kts", "lua",
             "sh", "bash", "ps1", "bat", "cmd", "sql", "vue", "svelte", "dart", "r",
             "m", "mm", "pl", "scala", "clj", "hs", "asm", "s", "v", "sv", "vhd", "qsf"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst", "cab", "iso", "img",
                "apk", "jar", "war", "whl", "tgz", "lz4", "br"},
    "app": {"exe", "msi", "appx", "msix", "dll", "sys", "com", "scr", "lnk", "app",
            "bat", "cmd", "ps1", "apk", "deb", "rpm", "dmg", "pkg"},
    "appdata": {"unity", "asset", "prefab", "mat", "shader", "fbx", "obj", "blend",
                "uasset", "umap", "anim", "controller"},
}
KIND_NAMES = ["folder"] + sorted(KIND_EXT.keys())
EXT_TO_KIND = {}
for _k, _exts in KIND_EXT.items():
    for _e in _exts:
        EXT_TO_KIND.setdefault(_e, _k)

APP_EXTS = KIND_EXT["app"]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def norm_key(path: str) -> str:
    """路径比较用的规范化键：小写 + 正斜杠。"""
    return path.replace("/", "\\").lower()


def now_ms() -> int:
    return int(time.time() * 1000)


def fmt_size(n: int) -> str:
    if n < 0:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    i = 0
    while v >= 1000 and i < len(units) - 1:
        v /= 1000.0
        i += 1
    return ("%d %s" % (v, units[i])) if i == 0 else ("%.1f %s" % (v, units[i]))


def fmt_time(ms: int) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d %H:%M")


# 有些文件（实测是 WPS 字体目录里的几个）报告的 mtime 是天文数字或负数，
# 排序索引里会变成垃圾值，还会把 bisect 的区间算歪。统一夹到合理范围内。
MTIME_MIN = 0
MTIME_MAX = 4102444800000          # 2100-01-01
MTIME_BOGUS = 946684800000         # 2000-01-01，异常值就记成这个


def clamp_mtime(ms) -> int:
    try:
        v = int(ms)
    except (TypeError, ValueError):
        return MTIME_BOGUS
    if v < MTIME_MIN or v > MTIME_MAX:
        return MTIME_BOGUS
    return v


def fmt_ago(ms: int) -> str:
    if not ms:
        return ""
    d = time.time() - ms / 1000.0
    if d < 60:
        return "刚刚"
    if d < 3600:
        return "%d 分钟前" % (d // 60)
    if d < 86400:
        return "%d 小时前" % (d // 3600)
    if d < 86400 * 30:
        return "%d 天前" % (d // 86400)
    return datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d")


_NUM_RE = re.compile(r"(\d+)")


def natural_key(s: str):
    """file2 排在 file10 前面。"""
    return [int(t) if t.isdigit() else t.lower() for t in _NUM_RE.split(s)]


def load_settings() -> dict:
    p = os.path.join(DATA_DIR, "settings.json")
    cfg = {}
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    cfg.setdefault("roots", list(DEFAULT_ROOTS))
    cfg.setdefault("hotkey", "shift+ctrl+f")
    cfg.setdefault("show_hidden", True)
    # 默认过滤缓存/临时/依赖目录（node_modules、cache、temp、WinSxS 等）。
    # 全盘上这类目录能占到接近一半的条目，不过滤的话结果会被它们淹没。
    # 想连这些目录一起搜就把它设成 false，或在界面里按 Ctrl+E 临时切换。
    cfg.setdefault("exclude_noise", True)
    cfg.setdefault("index_sessions", True)
    cfg.setdefault("session_roots", [DSH_SESSIONS])
    cfg.setdefault("per_root_budget", ROOT_SCAN_BUDGET)
    # 监听缓冲区溢出（丢了文件变更事件）时要不要自动做一次全量重扫。
    # 默认 false：重扫要八分钟，而"刚打开程序"正是文件活动最多的时候，
    # 自动扫会让人以为"每次启动都在重建"。界面上有「重建索引」按钮，由用户决定。
    cfg.setdefault("auto_rescan_on_overflow", False)
    # 应用外观：名字和图标都能在界面里改（第 12 项）
    cfg.setdefault("app_title", "")
    cfg.setdefault("app_icon_ext", "")
    return cfg


def save_settings(cfg: dict) -> None:
    ensure_dirs()
    with open(os.path.join(DATA_DIR, "settings.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 拼音：码点 -> 全拼 + 首字母
# ---------------------------------------------------------------------------
# oil-find 在 macOS 上是运行时用 ICU 的 kCFStringTransformMandarinLatin 生成这张表；
# Windows 没有等价 API，所以这里固化一份等价数据（pinyindata.pyz）。
# 表结构刻意做得和它一样精简：只有"码点 -> 音节"一列，首字母就是音节首字符。

class PinyinTable:
    def __init__(self, path: str = PINYIN_PATH):
        self.map = {}
        self.ok = False
        self.count = 0
        try:
            with zipfile.ZipFile(path) as z:
                raw = z.read("pinyin.tsv").decode("utf-8")
            for line in raw.split("\n"):
                if not line:
                    continue
                cp_hex, syl = line.split("\t", 1)
                self.map[int(cp_hex, 16)] = syl
            self.count = len(self.map)
            self.ok = True
        except Exception as e:  # 数据文件缺失时优雅降级为"只搜中文原文"
            self.map = {}
            self.err = str(e)

    def syllable(self, ch: str):
        return self.map.get(ord(ch))

    def to_pinyin(self, text: str):
        """返回 (首字母串, 全拼串)。非汉字字符原样保留（小写）。"""
        ini = []
        full = []
        get = self.map.get
        for ch in text:
            o = ord(ch)
            if o < 128:
                ini.append(ch)
                full.append(ch)
                continue
            syl = get(o)
            if syl is None:
                ini.append(ch)
                full.append(ch)
            else:
                ini.append(syl[0])
                full.append(syl)
        return ("".join(ini).lower(), "".join(full).lower())


PINYIN = PinyinTable()


def _pinyin_props(self):
    """Entry.pinyin_initials / Entry.pinyin_full（懒计算并缓存）。"""
    ini, full = PINYIN.to_pinyin(self.name)
    self.piny = ini
    self.pyfull = full
    return ini, full


def _get_initials(self):
    if self.piny is None:
        _pinyin_props(self)
    return self.piny


def _get_full(self):
    if self.pyfull is None:
        _pinyin_props(self)
    return self.pyfull


class PinyinIndex:
    """拼音检索索引：首字母 -> 条目，全拼 -> 条目。

    全部 200 多万条目里只有不到 1% 含汉字，所以建一次只要半秒，
    但能把"拼音检索"从"每次查询给所有 ASCII 名字算一遍拼音"（那是几十秒的活）
    变成几次字典查表（毫秒级）。这是整个工具性能上最关键的一处取舍。
    """

    __slots__ = ("initials", "full", "entries", "built")

    def __init__(self):
        self.initials = {}
        self.full = {}
        self.entries = []
        self.built = 0

    def build(self, entries) -> "PinyinIndex":
        ini_idx = {}
        full_idx = {}
        non_ascii = []
        to_pinyin = PINYIN.to_pinyin
        for e in entries:
            name = e.name
            if name.isascii():
                continue
            non_ascii.append(e)
            ini, full = to_pinyin(name)
            low = e.low
            if ini != low:
                e.piny = ini
                e.pyfull = full
                bucket = ini_idx.get(ini)
                if bucket is None:
                    ini_idx[ini] = [e]
                else:
                    bucket.append(e)
                if full != ini:
                    bucket = full_idx.get(full)
                    if bucket is None:
                        full_idx[full] = [e]
                    else:
                        bucket.append(e)
        self.initials = ini_idx
        self.full = full_idx
        self.entries = non_ascii
        self.built = len(non_ascii)
        return self

    def add_entry(self, e) -> None:
        """增量加入一个条目（文件监听后新出现的汉字文件）。"""
        if e.name.isascii():
            return
        ini, full = PINYIN.to_pinyin(e.name)
        e.piny = ini
        e.pyfull = full
        if ini != e.low:
            self.initials.setdefault(ini, []).append(e)
            if full != ini:
                self.full.setdefault(full, []).append(e)
        self.entries.append(e)
        self.built = len(self.entries)

    def candidates(self, needle: str):
        """返回拼音命中 needle 的条目列表（先精确键，再键子串）。"""
        out = []
        seen = set()
        for e in self.candidates_exact(needle):
            k = id(e)
            if k not in seen:
                seen.add(k)
                out.append(e)
        for idx in (self.initials, self.full):
            for key, bucket in idx.items():
                if needle in key:
                    for e in bucket:
                        k = id(e)
                        if k in seen:
                            continue
                        seen.add(k)
                        out.append(e)
        return out

    def candidates_exact(self, needle: str):
        out = []
        seen = set()
        for idx in (self.initials, self.full):
            for e in idx.get(needle, ()):
                k = id(e)
                if k in seen:
                    continue
                seen.add(k)
                out.append(e)
        return out


# ---------------------------------------------------------------------------
# 匹配与打分（对齐 oil-find 的四级匹配质量）
# ---------------------------------------------------------------------------

def name_match_quality(low: str, needle: str, case_sensitive: bool = False):
    """在名字里找 needle，返回 (质量, 起始下标, 命中长度)。

    质量 4=全名精确或"主名.单扩展名"；3=前缀；2=词边界/驼峰边界；1=内部子串。
    """
    n = len(needle)
    if not n:
        return 0, -1, 0
    if case_sensitive:
        pos = low.find(needle)
        names_low = low
    else:
        needle = needle.lower()
        names_low = low
        pos = names_low.find(needle)
    if pos < 0:
        return 0, -1, 0
    stem, dot, ext = names_low.rpartition(".")
    if pos == 0:
        if len(names_low) == n:
            q = 4
        elif dot and stem == needle and names_low.count(".") == 1:
            q = 4
        else:
            q = 3
    else:
        q = 1
        if pos == 1 and names_low[0] in " -_.([+,&@#~":
            q = 2
        elif pos == 2 and names_low[1] in " -_.([+,&@#~":
            q = 2
        else:
            prev = names_low[pos - 1]
            cur = names_low[pos]
            if prev in " -_.([+,&@#~" or prev.isdigit() != cur.isdigit():
                q = 2
            elif prev.islower() and cur.isupper():
                q = 2
    return q, pos, n


def _quality_at(low: str, pos: int, n: int) -> int:
    """已知命中位置时，直接给出四级质量之一（等价 name_match_quality 的判断）。"""
    if pos <= 0:
        return 3
    if pos == 1 and low[0] in " -_.([+,&@#~":
        return 2
    prev = low[pos - 1]
    if prev in " -_.([+,&@#~":
        return 2
    if prev.islower() and low[pos].isupper():
        return 2
    return 1


def name_relevance(name_low: str, quality: int, depth: int, noise: bool = False) -> int:
    """oil-find 的加性打分：匹配质量为主，名字长度/深度/噪音做惩罚。

    路径级别的噪音惩罚已经由 e.noise 这个"继承标记"表达了（扫描时就打好），
    所以这里不再逐条去查路径子串 —— 那是热循环里的重复劳动。
    """
    score = quality * 200 + 200
    score -= 2 * min(max(0, len(name_low) - 4), 64)
    if depth > 0:
        score -= 5 * min(depth, 16)
    if noise:
        score -= 350
    return score


def is_noise_path(path_low: str) -> bool:
    return NOISE_MARKER_RX.search(path_low) is not None


def path_depth(path: str) -> int:
    return path.count("\\") - 1


# ---------------------------------------------------------------------------
# 索引条目
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Entry:
    path: str
    name: str
    low: str            # 小写**文件名**（不含目录），匹配用
    is_dir: bool
    size: int
    mtime_ms: int
    depth: int
    noise: bool
    hidden: bool
    piny: str | None = None
    pyfull: str | None = None
    _path_low: str | None = None
    gone: bool = False          # 文件监听发现它被删了/改名了

    @property
    def path_low(self) -> str:
        v = self._path_low
        if v is None:
            v = self._path_low = self.path.lower()
        return v

    @property
    def ext(self) -> str:
        if self.is_dir:
            return ""
        _, _, e = self.name.rpartition(".")
        return e.lower()

    @property
    def kind(self) -> str:
        if self.is_dir:
            return "folder"
        return EXT_TO_KIND.get(self.ext, "")

    def initials(self) -> str:
        return _get_initials(self)

    def full_pinyin(self) -> str:
        return _get_full(self)


class BlobEntry:
    """从加载进来的大字符串里"切片视图"式的条目。

    213 万条目要是每条都建 path/name/low 三个字符串，光造对象就要 9 秒、
    多占 270MB。所以这里只存 (路径起点, 终点) 两个整数，指向一份
    拼接好的小写路径大字符串 + 一份原样路径大字符串，需要时再切。
    切片很便宜（不复制底层时几乎零成本），换来的是启动时间从 30 秒降到 2 秒。
    """
    __slots__ = ("_s", "_e", "is_dir", "size", "mtime_ms", "depth",
                 "noise", "hidden", "piny", "pyfull", "_low", "_path", "_name",
                 "gone")

    def __init__(self, s, e, is_dir, size, mtime_ms, depth, noise, hidden):
        self._s = s
        self._e = e
        self.is_dir = is_dir
        self.size = size
        self.mtime_ms = mtime_ms
        self.depth = depth
        self.noise = noise
        self.hidden = hidden
        self.piny = None
        self.pyfull = None
        self._low = None
        self._path = None
        self._name = None
        self.gone = False

    @property
    def low(self) -> str:
        """小写文件名（不含目录）。整条小写路径用 path_low。"""
        v = self._low
        if v is None:
            p = _ENGINE.paths_orig
            s = p.rfind("\\", self._s, self._e)
            v = self._low = p[s + 1:self._e]
        return v

    @property
    def path_low(self) -> str:
        """小写整条路径。索引里存的就是小写，所以等于 path。"""
        return self.path

    @property
    def path(self) -> str:
        """索引里存的是小写路径；显示前请用 display_path() 还原真实大小写。"""
        p = self._path
        if p is None:
            p = self._path = _ENGINE.paths_orig[self._s:self._e]
        return p

    @property
    def name(self) -> str:
        n = self._name
        if n is None:
            p = _ENGINE.paths_orig
            s = p.rfind("\\", self._s, self._e)
            n = self._name = p[s + 1:self._e]
        return n

    def display_path(self) -> str:
        """给界面看的路径（真实大小写）。

        GetLongPathNameW 是一次文件系统调用，只对真正要显示的那几行调用；
        绝不能放进打分或者全扫循环里 —— 那会让一次查询变成几万次磁盘访问。
        """
        return actual_path(self.path)

    @property
    def ext(self) -> str:
        if self.is_dir:
            return ""
        n = self.name
        i = n.rfind(".")
        return n[i + 1:].lower() if i >= 0 else ""

    @property
    def kind(self) -> str:
        if self.is_dir:
            return "folder"
        return EXT_TO_KIND.get(self.ext, "")

    def initials(self) -> str:
        return _get_initials(self)

    def full_pinyin(self) -> str:
        return _get_full(self)


_REAL_CACHE = {}
_REAL_CACHE_MAX = 20000


def _real_component(parent: str, name_low: str) -> str:
    """在 parent 目录里找出 name_low 对应的真实大小写。"""
    d = _REAL_CACHE.get(parent)
    if d is None:
        d = {}
        try:
            with os.scandir(parent) as it:
                for e in it:
                    d[e.name.lower()] = e.name
        except OSError:
            pass
        if len(_REAL_CACHE) >= _REAL_CACHE_MAX:
            # 只淘汰最早的四分之一，别整表清空：1000 条结果通常横跨 900 多个目录，
            # 一次查询正好会把小容量的缓存撑爆，清空后又从头扫，来回抖动。
            # 实测（1000 条 readme）：容量 1024 时 805ms 且重复查还是 805ms；
            # 改成 20000 + 局部淘汰后，重复查 7ms。
            for k in list(_REAL_CACHE)[: max(64, _REAL_CACHE_MAX // 4)]:
                _REAL_CACHE.pop(k, None)
        _REAL_CACHE[parent] = d
    return d.get(name_low, name_low)


def actual_path(path_low: str) -> str:
    """把索引里的小写路径还原成磁盘上的真实大小写。

    索引为了省内存只存一份（小写形式），显示时再逐段还原。
    不能用 GetLongPathNameW：它只负责展开 8.3 短名，路径不存在时原样返回，
    大小写也不保证逐段还原（实测 `e:\\hypergryph launcher` 就原样吐回来了）。
    这里按目录 scandir 一次拿全目录名，与磁盘上的存储名精确对上；结果按目录缓存，
    所以一屏 9 条结果通常只做几次目录读取。

    路径若已被删除，就保持小写 —— 那是本来就查不到的路径，不影响使用。
    """
    if not path_low:
        return path_low
    parts = path_low.split("\\")
    cur = parts[0][:1].upper() + parts[0][1:]
    for name_low in parts[1:]:
        if not name_low:
            continue
        real = _real_component(cur, name_low)
        cur = cur + "\\" + real
    return cur


_kernel32 = ctypes.WinDLL("kernel32")


class _EngineRef:
    """给 BlobEntry 一个取大字符串的全局句柄（避免每个条目存一份引擎指针）。"""
    paths_orig = ""
    paths_low = ""


_ENGINE = _EngineRef()


# ---------------------------------------------------------------------------
# 查询语法（对齐 oil-find 的扁平两级结构：空格=AND，| 优先级更高=OR，! =NOT）
# ---------------------------------------------------------------------------

class ParseError(Exception):
    pass


def _parse_size(text: str):
    """size:>10mb / size:1mb..5mb / size:1024  —— 十进制单位，裸数字为精确匹配。"""
    t = text.strip().lower().replace(" ", "")
    if not t:
        raise ParseError("size: 后面没写值")

    def to_num(s):
        """'10' -> 10, '10mb' -> 10000000, '1.5gb' -> 1500000000"""
        s = s.strip()
        mult = 1
        for suf, m in (("tb", 1_000_000_000_000), ("gb", 1_000_000_000),
                       ("mb", 1_000_000), ("kb", 1_000), ("b", 1)):
            if s.endswith(suf):
                mult = m
                s = s[:-len(suf)]
                break
        try:
            return int(float(s) * mult)
        except ValueError:
            raise ParseError("size: 的值看不懂：%s" % text)

    try:
        if ".." in t:
            a, b = t.split("..", 1)
            return ("range", to_num(a), to_num(b))
        if t.startswith(">="):
            return ("min", to_num(t[2:]), None)
        if t.startswith(">"):
            return ("min", to_num(t[1:]) + 1, None)
        if t.startswith("<="):
            return ("max", None, to_num(t[2:]) + 1)
        if t.startswith("<"):
            return ("max", None, to_num(t[1:]))
        v = to_num(t)
        return ("exact", v, v)
    except ValueError:
        raise ParseError("size: 的值看不懂：%s" % text)


def _parse_dm(text: str):
    """dm:today / dm:week / dm:2026-10-01 / dm:>2026-01-01 / dm:7d / dm:2026-01..2026-02"""
    t = text.strip().lower()
    if not t:
        raise ParseError("dm: 后面没写值")
    now = datetime.now()

    def day_start(d):
        return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    def parse_one(s):
        s = s.strip()
        if s in ("today", "今天"):
            return day_start(now), day_start(now + timedelta(days=1))
        if s in ("yesterday", "昨天"):
            return day_start(now - timedelta(days=1)), day_start(now)
        if s in ("week", "本周", "这周"):
            return day_start(now - timedelta(days=6)), day_start(now + timedelta(days=1))
        if s in ("month", "本月", "这个月"):
            return day_start(now - timedelta(days=29)), day_start(now + timedelta(days=1))
        if s in ("year", "今年"):
            return day_start(now - timedelta(days=364)), day_start(now + timedelta(days=1))
        m = re.fullmatch(r"(\d+)([hdwmy])", s)
        if m:
            n = int(m.group(1))
            unit = {"h": 3600, "d": 86400, "w": 604800, "m": 2592000, "y": 31536000}[m.group(2)]
            return now_ms() - n * unit * 1000, None
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
        if m:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return day_start(d), day_start(d + timedelta(days=1))
        m = re.fullmatch(r"(\d{4})-(\d{1,2})", s)
        if m:
            d = datetime(int(m.group(1)), int(m.group(2)), 1)
            nxt = datetime(d.year + (1 if d.month == 12 else 0), 1 if d.month == 12 else d.month + 1, 1)
            return day_start(d), day_start(nxt)
        m = re.fullmatch(r"(\d{4})", s)
        if m:
            d = datetime(int(m.group(1)), 1, 1)
            return day_start(d), day_start(datetime(int(m.group(1)) + 1, 1, 1))
        raise ParseError("dm: 的值看不懂：%s" % text)

    if ".." in t:
        a, b = t.split("..", 1)
        lo = parse_one(a)[0]
        hi = parse_one(b)[1]
        return ("range", lo, hi)
    if t.startswith(">="):
        return ("min", parse_one(t[2:])[0], None)
    if t.startswith(">"):
        return ("min", parse_one(t[1:])[1], None)
    if t.startswith("<="):
        return ("max", None, parse_one(t[2:])[1])
    if t.startswith("<"):
        return ("max", None, parse_one(t[1:])[0])
    if "-" in t and len(t) == 10:
        lo, hi = parse_one(t)
        return ("range", lo, hi)
    lo, hi = parse_one(t)
    if hi is None:
        return ("min", lo, None)
    return ("range", lo, hi)


def _parse_ext(text: str):
    """ext:pdf;docx —— 分号分隔。"""
    exts = set()
    for part in text.split(";"):
        part = part.strip().lower().lstrip(".")
        if part:
            exts.add(part)
    if not exts:
        raise ParseError("ext: 后面没写扩展名")
    return exts


def _parse_kind(text: str):
    k = text.strip().lower()
    if k not in KIND_NAMES and k not in ("file", "files", "目录", "文件夹", "文件"):
        raise ParseError("kind: 只认这些值：%s" % ", ".join(KIND_NAMES))
    if k in ("目录", "文件夹"):
        return "folder"
    if k in ("文件",):
        return "file"
    return k


class Filter:
    """一个过滤条件。"""

    __slots__ = ("kind", "a", "b", "text", "rx", "_cand", "raw", "_verified")

    def __init__(self, kind, a=None, b=None, text=None, raw=None):
        self.kind = kind
        self.a = a
        self.b = b
        self.text = text
        self.rx = None
        self._cand = None
        self.raw = raw          # 原始输入片段，只用于界面显示回显
        self._verified = False  # True = 索引已经保证候选全部命中，不必再逐条验

    def may_skip(self) -> bool:
        """索引已经确认过命中，可以跳过逐条匹配。"""
        return self._verified

    def prepare(self, engine) -> list | None:
        """预处理：能走索引的就走，返回候选列表（可能为空）；否则返回 None。"""
        if self.kind == "name":
            needle = self.text
            if not needle:
                return []
            # 非 ASCII 关键词（中文）只可能命中含汉字的条目，直接用拼音索引的非 ASCII 全集
            if not needle.isascii():
                self._cand = engine.pinyin.entries
                return self._cand
            # ASCII 关键词可能是拼音首字母/全拼：先精确查表（毫秒级）
            exact = engine.pinyin.candidates_exact(needle)
            if exact:
                self._cand = exact
                return exact
            # 再试 FTS trigram（3 字以上才有意义）。
            # FTS 命中即代表路径里含这个子串，后面不用再验一遍。
            if len(needle) >= FTS_MIN_NEEDLE:
                rowids = fts_search(needle, engine)
                if rowids:
                    self._cand = _rowids_to_entries(engine, rowids)
                    self._verified = True
                    return self._cand
            return None
        if self.kind == "path":
            # 路径子串查询：在整块小写路径大字符串上做一次 C 层 find，
            # 再用条目起点数组二分回查是哪个条目。
            # 比在 Python 里逐条 343 万次 in 判断快一个数量级。
            needle = self.text
            if not needle:
                return None
            cand = engine.path_candidates(needle)
            if cand is not None:
                self._cand = cand
                self._verified = True
                return cand
            return None
        if self.kind == "regex":
            if self.rx is None:
                return []
            if re.search(r"[\u4e00-\u9fff]", self.text):
                self._cand = engine.pinyin.entries
                return self._cand
            return None
        if self.kind in ("ext", "kind"):
            # 扩展名索引：惰性建一次，之后 ext:/kind: 都不用全扫。
            # 索引里的条目本身就满足这个条件，所以标记为已确认，后面不必逐条再验。
            engine.build_ext_index()
            if self.kind == "ext":
                cand = []
                for ext in self.a:
                    cand.extend(engine.ext_index.get(ext, ()))
                self._cand = cand
                self._verified = True
                return cand
            if self.a == "folder":
                return None      # 目录数量少，直接全扫更省事
            if self.a == "file":
                return None
            wanted = KIND_EXT.get(self.a)
            if not wanted:
                return None
            cand = []
            idx = engine.ext_index
            for ext in wanted:
                cand.extend(idx.get(ext, ()))
            self._cand = cand
            self._verified = True
            return cand
        if self.kind in ("size", "dm"):
            # 排序索引：把"大于某大小""晚于某时间"变成一次二分
            engine.build_sorted()
            mode, lo, hi = self.a, self.b, self.text
            if mode == "min":
                ids = engine.sorted.by_size("min", lo, None) if self.kind == "size" \
                    else engine.sorted.by_mtime("min", lo, None)
            elif mode == "max":
                ids = engine.sorted.by_size("max", None, hi) if self.kind == "size" \
                    else engine.sorted.by_mtime("max", None, hi)
            else:
                ids = engine.sorted.by_size("range", lo, hi) if self.kind == "size" \
                    else engine.sorted.by_mtime("range", lo, hi)
            self._cand = _rowids_to_entries(engine, ids)
            self._verified = True
            return self._cand
        if self.kind == "glob":
            # 形如 *.png 的通配符：取字面后缀走扩展名索引，命中即已确认
            ext = glob_literal_ext(self.a)
            if ext:
                engine.build_ext_index()
                self._cand = engine.ext_index.get(ext, [])
                self._verified = True
                return self._cand
            return None
        return None

    def match(self, e: Entry) -> bool:
        k = self.kind
        if k == "name":
            # 快路径：先用 C 层的子串查找做闸门，命中才去算质量分。
            # 直接调 name_match_quality 是 1.7us/条，237 万条要 4 秒；
            # `in` 只要 0.4us/条，绝大部分条目在闸门就被挡掉了。
            low = e.low
            n = self.text
            if n not in low:
                if n.isascii() and not e.name.isascii():
                    ini = _get_initials(e)
                    return n in ini or n in _get_full(e)
                return False
            return True
        if k == "glob":
            return _glob_any(e, self.a)
        if k == "regex":
            if self.rx is None:
                return False
            if not e.name.isascii():
                if self.rx.search(_get_initials(e)) or self.rx.search(_get_full(e)):
                    return True
            return bool(self.rx.search(e.low))
        if k == "ext":
            if e.is_dir:
                return False
            return e.ext in self.a
        if k == "kind":
            if self.a == "folder":
                return e.is_dir
            if self.a == "file":
                return not e.is_dir
            return e.kind == self.a
        if k == "files_only":
            return not e.is_dir
        if k == "folders_only":
            return e.is_dir
        if k == "size":
            if e.is_dir:
                return False
            mode, lo, hi = self.a, self.b, self.text
            if mode == "min":
                return e.size >= lo
            if mode == "max":
                return e.size < hi
            return lo <= e.size <= hi
        if k == "dm":
            mode, lo, hi = self.a, self.b, self.text
            if mode == "min":
                return e.mtime_ms >= lo
            if mode == "max":
                return e.mtime_ms < hi
            return lo <= e.mtime_ms < hi
        if k == "path":
            return self.text in e.path_low
        return True

    def describe(self) -> str:
        if self.kind == "name":
            return self.text
        if self.kind == "glob":
            return self.a
        if self.kind == "regex":
            return "regex:" + self.text
        if self.kind == "ext":
            return "ext:" + ";".join(sorted(self.a))
        if self.kind == "kind":
            return "kind:" + self.a
        if self.kind == "size":
            return "size:" + (self.raw or str(self.text))
        if self.kind == "dm":
            return "dm:" + (self.raw or str(self.text))
        if self.kind == "path":
            return "path:" + self.text
        return self.kind


def _glob_any(e: Entry, pattern: str) -> bool:
    """通配符锚定整个文件名（和 oil-find 一致）；同时试拼音键。"""
    if fnmatch.fnmatchcase(e.low, pattern):
        return True
    if pattern.isascii() and not e.name.isascii():
        p = pattern.lower()
        if fnmatch.fnmatchcase(_get_initials(e), p) or fnmatch.fnmatchcase(_get_full(e), p):
            return True
    return False


FILTER_KEYS = ("ext", "kind", "size", "dm", "regex", "path", "case", "file", "folder", "name")


def is_filter_token(tok: str) -> bool:
    """只有前缀真的是已知过滤器名才算过滤器，否则 `C:\\Users` 会被误判。"""
    low = tok.lower()
    for k in FILTER_KEYS:
        if low.startswith(k + ":"):
            return True
    return False


def _looks_like_path_token(tok: str) -> bool:
    if is_filter_token(tok):
        return False
    return "\\" in tok or "/" in tok or tok.startswith("~") or bool(re.match(r"^[a-zA-Z]:", tok))


def _parse_filter(tok: str, quoted: bool) -> Filter | None:
    if not is_filter_token(tok):
        return None
    key, _, val = tok.partition(":")
    key = key.lower()
    if key == "name":
        if not val:
            raise ParseError("name: 后面没写内容")
        return Filter("name", text=val.lower())
    if key == "ext":
        return Filter("ext", a=_parse_ext(val))
    if key == "kind":
        return Filter("kind", a=_parse_kind(val))
    if key == "size":
        mode, lo, hi = _parse_size(val)
        f = Filter("size", a=mode, raw=val)
        f.b = lo
        f.text = hi
        return f
    if key == "dm":
        mode, lo, hi = _parse_dm(val)
        f = Filter("dm", a=mode, raw=val)
        f.b = lo
        f.text = hi
        return f
    if key == "regex":
        if not val:
            raise ParseError("regex: 后面没写内容")
        f = Filter("regex", text=val)
        try:
            f.rx = re.compile(val, re.IGNORECASE)
        except re.error as ex:
            raise ParseError("正则不合法：%s" % ex)
        return f
    if key == "path":
        if not val:
            raise ParseError("path: 后面没写内容")
        return Filter("path", text=val.lower().replace("/", "\\"))
    if key == "case":
        return None  # 由 parse 层处理
    if key == "file":
        f = Filter("files_only")
        f.text = "/" + val.lower() if val else None
        return f
    if key == "folder":
        f = Filter("folders_only")
        f.text = "/" + val.lower() if val else None
        return f
    return None


def _looks_like_glob(tok: str) -> bool:
    return ("*" in tok or "?" in tok) and ":" not in tok


class Query:
    def __init__(self, clauses, raw, diagnostics, case_sensitive=False):
        self.clauses = clauses          # list[list[(negated, Filter)]]，空格分隔的 AND 组
        self.raw = raw
        self.diagnostics = diagnostics
        self.case_sensitive = case_sensitive
        self.empty = not clauses
        # `|` 连接起来的 OR 分支，每个分支内部又是一组 AND 条件。
        # 解析时按 AND 组存、再由 finalize_or() 合并成真正的 OR 结构，
        # 否则 `report | 报告` 会退化成"同时包含 report 和 报告"。
        self.or_groups = []             # list[list[list[(negated, Filter)]]]
        self.and_groups = []            # list[list[(negated, Filter)]]

    def finalize_or(self):
        """把 and_groups 收成"要么单组 AND，要么若干组 OR"。"""
        groups = [g for g in self.and_groups if g]
        self.and_groups = groups
        self.or_groups = []
        if len(groups) > 1:
            self.or_groups = [[g] for g in groups]
        self.empty = not groups

    def describe(self):
        if self.or_groups:
            return " | ".join(_describe_and(g[0]) for g in self.or_groups)
        return " ".join(_describe_and(g) for g in self.and_groups)


def _describe_and(group):
    parts = []
    for neg, f in group:
        parts.append(("!" if neg else "") + f.describe())
    return " ".join(parts) if parts else ""


def parse_query(text: str) -> Query:
    """把输入解析成 oil-find 那种扁平结构。

    空格 = AND（同一个分支内），`|` = OR（把左右两边的分支并联），`!` = NOT。
    `|` 的优先级高于空格，所以 `a b | c !d` 读作 `a AND (b OR c) AND NOT d` 这一类形状时
    这里简化成"每个 `|` 分支是一条 AND 链，分支之间 OR"。
    """
    diagnostics = []
    tokens = []
    cur = ""
    quoted = False
    in_quote = False
    for ch in text:
        if ch == '"':
            in_quote = not in_quote
            if cur:
                tokens.append((cur, quoted))
                cur = ""
            quoted = in_quote
            continue
        if ch.isspace():
            if cur:
                tokens.append((cur, quoted))
                cur = ""
            continue
        if ch == "|" and not in_quote:
            if cur:
                tokens.append((cur, quoted))
                cur = ""
            tokens.append(("|", False))
            continue
        cur += ch
    if cur:
        tokens.append((cur, quoted))

    case_sensitive = False
    branches = [[]]          # 每个 `|` 分支
    for raw_tok, is_quoted in tokens:
        if raw_tok == "|":
            if branches[-1]:
                branches.append([])
            continue
        tok = raw_tok
        negated = False
        if not is_quoted:
            while tok.startswith("!"):
                negated = not negated
                tok = tok[1:]
            if not tok:
                continue
        if not is_quoted and tok.lower() == "case:":
            case_sensitive = True
            continue
        try:
            f = _parse_filter(tok, is_quoted)
        except ParseError as ex:
            diagnostics.append(str(ex))
            continue
        if f is None:
            if is_quoted:
                f = Filter("name", text=tok.lower())
            elif _looks_like_glob(tok):
                f = Filter("glob", a=tok.lower())
            elif _looks_like_path_token(tok):
                f = Filter("path", text=tok.lower().replace("/", "\\"))
            else:
                f = Filter("name", text=tok.lower())
        # file:/folder: 单独出现时展开成"类型 + 名字"两个 AND 条件
        if f.kind in ("files_only", "folders_only") and f.text and not negated:
            name_part = f.text[1:]
            branches[-1].append((False, f))
            branches[-1].append((False, Filter("name", text=name_part)))
            continue
        branches[-1].append((negated, f))

    q = Query([], text, diagnostics, case_sensitive)
    branches = [b for b in branches if b]
    q.and_groups = branches
    if len(branches) == 1:
        q.clauses = branches
    q.finalize_or()
    if not branches and text.strip():
        diagnostics.append("没有解析出任何有效条件")
    q.original = text
    return q


# ---------------------------------------------------------------------------
# 执行查询
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Hit:
    entry: Entry
    score: int
    span: tuple | None = None
    span_kind: str = "name"


# 查询结果缓存：key = (原始查询串, limit, 是否过滤噪音, 索引构建时间戳)
_QUERY_CACHE: dict = {}

# 路径的 FTS5 trigram 镜像库。
# 为什么需要：3 字以上的字面串（`readme`、`settings`）在 Python 里全扫 343 万条约 0.6 秒，
# 放到 SQLite 的 C 层用 trigram 索引筛只要 30～110 毫秒，快 8～20 倍。
# 中文和 2 字以内的关键词用不上它（trigram 至少 3 个字符），但那些本来就走拼音索引、
# 已经是毫秒级，所以这条慢路径正好被它补上。
FTS_PATH = os.path.join(DATA_DIR, "nameindex.db")
# 文件监听产生的增量（新增条目 / 已删除标记）落盘到这里，重启后接着用
DELTA_PATH = os.path.join(DATA_DIR, "delta.json")
SORT_DIR = os.path.join(DATA_DIR, "sortindex")
FTS_MIN_NEEDLE = 3
_fts_con = None
_fts_ok_for = None   # 已校验过同源性的 built_at
# 名字索引既要被查询读、又要被文件监听写。用一个锁把两者串起来，
# 否则可能出现读到半写状态，实测会偶发一次查询 500。
_fts_lock = threading.RLock()


# ---------------------------------------------------------------------------
# 排序索引：按大小 / 按修改时间
# ---------------------------------------------------------------------------
# `size:>100mb` 和 `dm:today` 这类条件原来要全扫 343 万条（2～5 秒）
# —— 因为它们没法定一个"关键词"，只能一条条比。
# 用两份排序好的条目下标数组，就能把"大于某个大小""晚于某个时间"变成
# 一次 bisect 二分：0.05 毫秒。
# 数组用 array('i')/('q')，两份索引合计约 56 MB，比 Python list 省一大截。

class SortedIndex:
    __slots__ = ("size_ids", "size_vals", "mtime_ids", "mtime_vals", "built", "seconds")

    def __init__(self):
        self.size_ids = array.array("i")
        self.size_vals = array.array("q")
        self.mtime_ids = array.array("i")
        self.mtime_vals = array.array("q")
        self.built = 0
        self.seconds = 0.0

    def build(self, entries, overlay=()) -> "SortedIndex":
        t0 = time.perf_counter()
        # id 用 64 位：监听新增的条目用 OVERLAY_BASE+序号 表示，
        # 和名字索引、回查映射共用同一套约定。
        ids = array.array("q")
        sizes = array.array("q")
        mtimes = array.array("q")
        ai = ids.append
        asz = sizes.append
        amt = mtimes.append
        for i, e in enumerate(entries):
            if e.is_dir or e.gone:
                continue
            ai(i)
            asz(e.size)
            amt(clamp_mtime(e.mtime_ms))
        for k, e in enumerate(overlay):
            if e.is_dir or e.gone:
                continue
            ai(OVERLAY_BASE + k)
            asz(e.size)
            amt(clamp_mtime(e.mtime_ms))
        n = len(ids)
        order = sorted(range(n), key=sizes.__getitem__)
        self.size_ids = array.array("q", (ids[k] for k in order))
        self.size_vals = array.array("q", (sizes[k] for k in order))
        order = sorted(range(n), key=mtimes.__getitem__)
        self.mtime_ids = array.array("q", (ids[k] for k in order))
        self.mtime_vals = array.array("q", (mtimes[k] for k in order))
        self.built = now_ms()
        self.seconds = time.perf_counter() - t0
        return self

    def save(self, path_dir: str = SORT_DIR) -> str:
        os.makedirs(path_dir, exist_ok=True)
        p = os.path.join(path_dir, "size_mtime.bin")
        tmp = p + ".tmp"
        payload = marshal.dumps({
            "v": 1, "built": self.built,
            "size_ids": self.size_ids.tobytes(),
            "size_vals": self.size_vals.tobytes(),
            "mtime_ids": self.mtime_ids.tobytes(),
            "mtime_vals": self.mtime_vals.tobytes(),
        }, 4)
        with gzip.open(tmp, "wb", compresslevel=2) as f:
            f.write(payload)
        replace_with_retry(tmp, p)
        return p
    def load(self, path_dir: str = SORT_DIR) -> bool:
        p = os.path.join(path_dir, "size_mtime.bin")
        if not os.path.exists(p):
            return False
        try:
            with gzip.open(p, "rb") as f:
                blob = marshal.loads(f.read())
            if blob.get("v") != 1:
                return False
            self.size_ids = array.array("q")
            self.size_ids.frombytes(blob["size_ids"])
            self.size_vals = array.array("q")
            self.size_vals.frombytes(blob["size_vals"])
            self.mtime_ids = array.array("q")
            self.mtime_ids.frombytes(blob["mtime_ids"])
            self.mtime_vals = array.array("q")
            self.mtime_vals.frombytes(blob["mtime_vals"])
            self.built = blob.get("built", 0)
            return True
        except Exception:
            return False

    def by_size(self, mode, lo, hi):
        """mode: min / max / exact / range。返回 id 列表（array 切片）。"""
        vals = self.size_vals
        n = len(vals)
        if mode == "min":
            s = bisect.bisect_left(vals, lo)
            return self.size_ids[s:]
        if mode == "max":
            s = bisect.bisect_left(vals, hi)
            return self.size_ids[:s]
        s = bisect.bisect_left(vals, lo)
        e = bisect.bisect_right(vals, hi)
        return self.size_ids[s:e]

    def by_mtime(self, mode, lo, hi):
        vals = self.mtime_vals
        if mode == "min":
            s = bisect.bisect_left(vals, lo)
            return self.mtime_ids[s:]
        if mode == "max":
            s = bisect.bisect_left(vals, hi)
            return self.mtime_ids[:s]
        s = bisect.bisect_left(vals, lo)
        e = bisect.bisect_left(vals, hi)
        return self.mtime_ids[s:e]


# 排序索引和文件索引是否对得上（存一个指纹文件，避免索引重建后用着旧排序）
SORT_STAMP = os.path.join(SORT_DIR, "stamp.json")


def sorted_is_stale(si, engine) -> bool:
    try:
        with open(SORT_STAMP, "r", encoding="utf-8") as f:
            stamp = json.load(f)
    except Exception:
        return True
    return int(stamp.get("index_built_at", 0)) != int(engine.built_at)


def sorted_save_stamp(engine) -> None:
    os.makedirs(SORT_DIR, exist_ok=True)
    with open(SORT_STAMP, "w", encoding="utf-8") as f:
        json.dump({"index_built_at": engine.built_at, "written": now_ms()}, f)


def fts_search(needle: str, engine=None, limit: int = 400_000):
    """在路径索引里找包含 needle 的条目下标。返回 list[int]，不可用则 None。

    **必须校验同源性**：FTS 库是按某个文件索引的条目顺序建的 rowid，
    如果文件索引重建过而这里没跟上，rowid 会整体错位 —— 返回的会是完全不相干的条目。
    所以拿库里的 index_built_at / count 和当前引擎对一遍，对不上就不用它。
    """
    global _fts_con, _fts_ok_for
    with _fts_lock:
        return _fts_search_locked(needle, engine, limit)


def _fts_search_locked(needle, engine, limit):
    global _fts_con, _fts_ok_for
    if not os.path.exists(FTS_PATH):
        return None
    if engine is not None:
        ok = _fts_ok_for == engine.built_at and _fts_ok_for is not None
        if not ok:
            m = fts_meta()
            try:
                ok = (int(m.get("index_built_at", "0") or 0) == int(engine.built_at or 0)
                      and int(m.get("count", "-1") or -1) == len(engine.entries))
            except (TypeError, ValueError):
                ok = False
            _fts_ok_for = engine.built_at if ok else None
        if not ok:
            return None
    try:
        if _fts_con is None:
            _fts_con = sqlite3.connect(
                "file:%s?mode=ro" % FTS_PATH.replace("\\", "/"), uri=True,
                timeout=10, check_same_thread=False)
        rows = _fts_con.execute(
            "SELECT rowid FROM ft WHERE ft MATCH ? LIMIT ?",
            ('"%s"' % needle.replace('"', '""'), limit)).fetchall()
        return [r[0] for r in rows]
    except sqlite3.Error as e:
        print("[dshfind] 名字索引查询失败：%s" % e, file=sys.stderr)
        try:
            if _fts_con is not None:
                _fts_con.close()
        except sqlite3.Error:
            pass
        _fts_con = None
        return None


def replace_with_retry(src: str, dst: str, tries: int = 8) -> None:
    """os.replace 偶尔会被杀毒/索引服务短暂占用而报 WinError 5，重试一下。

    FileNotFoundError（源文件已经不在了）也算进来：两个进程/线程同时写同一个
    .tmp 时，先完成 replace 的那个会把 tmp 搬走，后一个就会报 WinError 2。
    这种情况重试也没用，直接让它抛出来由调用方处理；真正解决靠调用方用唯一的 tmp 名。
    """
    last = None
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except FileNotFoundError:
            raise
        except PermissionError as e:
            last = e
            time.sleep(0.25 * (i + 1))
    if last is not None:
        raise last


def close_fts_conn() -> None:
    """关掉那个常驻的只读连接（重建名字索引前必须调）。

    Windows 上「还有别的句柄开着的文件」不能被 os.replace 覆盖 —— SQLite 打开文件时
    没有带 FILE_SHARE_DELETE，所以 `nameindex.db.tmp -> nameindex.db` 会直接报
    [WinError 5] 拒绝访问。这就是"重建索引跑到最后一步失败"的原因（第 11 项）。
    """
    global _fts_con, _fts_ok_for
    with _fts_lock:
        if _fts_con is not None:
            try:
                _fts_con.close()
            except sqlite3.Error:
                pass
            _fts_con = None
        _fts_ok_for = None


def fts_build(engine, path: str = FTS_PATH, on_progress=None) -> str:
    """从内存索引重建名字 FTS 库。按条目下标做 rowid，便于回查。

    只索引**文件名**，不索引整条路径：路径全量索引会让库膨胀到 1.6 GB，
    而文件名通常才是用户真正在找的东西。代价是"只在目录名里出现、文件名里没有"
    的关键词命中不到 —— 这类查询可以加 `path:` 前缀，那条路是 Python 全扫。
    """
    ensure_dirs()
    tmp = path + ".tmp"
    for p in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(p):
            os.unlink(p)
    con = sqlite3.connect(tmp)
    try:
        con.execute("PRAGMA page_size=8192")
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA cache_size=-200000")
        con.execute("CREATE VIRTUAL TABLE ft USING fts5(n, tokenize='trigram')")
        con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        batch = []
        append = batch.append
        total = len(engine.entries)
        # 两种条目都要支持：从磁盘加载的是 BlobEntry（有 _s/_e 偏移），
        # 刚扫描/增量出来的是 Entry。用 e.low（小写文件名）对两者都成立。
        for i, e in enumerate(engine.entries):
            append((i, e.low))
            if len(batch) >= 20000:
                con.executemany("INSERT INTO ft(rowid, n) VALUES(?,?)", batch)
                batch.clear()
                if on_progress:
                    on_progress(i, total)
        # 监听新增的条目也要进名字索引（rowid 用 OVERLAY_BASE+序号，和查询端一致）
        for k, e in enumerate(getattr(engine, "overlay", ())):
            if not e.gone:
                append((OVERLAY_BASE + k, e.low))
                if len(batch) >= 20000:
                    con.executemany("INSERT INTO ft(rowid, n) VALUES(?,?)", batch)
                    batch.clear()
        if batch:
            con.executemany("INSERT INTO ft(rowid, n) VALUES(?,?)", batch)
        con.execute("INSERT INTO meta(k,v) VALUES('built_at',?)", (str(now_ms()),))
        con.execute("INSERT INTO meta(k,v) VALUES('index_built_at',?)",
                    (str(engine.built_at),))
        con.execute("INSERT INTO meta(k,v) VALUES('count',?)", (str(total),))
        con.commit()
        con.execute("INSERT INTO ft(ft) VALUES('optimize')")
        con.commit()
    finally:
        con.close()
    # 关键：先把常驻的只读连接关掉，否则 Windows 不让覆盖这个文件（WinError 5）
    close_fts_conn()
    replace_with_retry(tmp, path)
    return path


def fts_meta(path: str = FTS_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True,
                              timeout=10)
        d = dict(con.execute("SELECT k, v FROM meta").fetchall())
        con.close()
        return d
    except sqlite3.Error:
        return {}


def fts_is_stale(engine) -> bool:
    """名字索引和当前文件索引是否对得上。"""
    if not os.path.exists(FTS_PATH):
        return True
    m = fts_meta()
    try:
        return int(m.get("index_built_at", "0")) != int(engine.built_at)
    except (TypeError, ValueError):
        return True


def glob_literal_ext(pattern: str):
    """把 `*.png` 这种"纯单段后缀通配"提炼成一个扩展名。

    多段后缀（`*.tar.gz`）、带问号或字符类的、以及 `*` 不在开头的，都返回 None
    退回全扫 —— 宁可慢一点，也不能给出漏结果。
    """
    if not pattern.startswith("*.") or pattern.count("*") != 1:
        return None
    tail = pattern[2:]
    if not tail or any(c in "*?[]\\/" for c in tail) or "." in tail:
        return None
    return tail.lower()


def _rowids_to_entries(engine, rowids):
    """把名字索引的 rowid 还原成条目对象。

    rowid < OVERLAY_BASE 的是磁盘索引里的条目（下标即 rowid）；
    更大的是文件监听后新增的条目（rowid = OVERLAY_BASE + overlay 下标）。
    """
    entries = engine.entries
    n = len(entries)
    overlay = engine.overlay
    out = []
    append = out.append
    for r in rowids:
        if not isinstance(r, int):
            continue
        if r >= OVERLAY_BASE:
            k = r - OVERLAY_BASE
            if 0 <= k < len(overlay) and not overlay[k].gone:
                append(overlay[k])
        elif 0 <= r < n:
            e = entries[r]
            if not e.gone:
                append(e)
    return out


def _fts_apply(new_rows, del_ids, engine=None):
    """把增量变更写进名字 FTS 索引（新增插入、删除移除）。

    名字索引只服务"3 字以上的英文子串"查询。不同步的话，
    新建的文件用英文名就搜不到，所以监听拿到变更要同步过来。
    """
    if not new_rows and not del_ids:
        return
    with _fts_lock:
        _fts_apply_locked(new_rows, del_ids)


def _fts_apply_locked(new_rows, del_ids):
    if not os.path.exists(FTS_PATH):
        return
    con = sqlite3.connect(FTS_PATH, timeout=10)
    try:
        for rid in del_ids:
            if rid >= 0:
                con.execute("DELETE FROM ft WHERE rowid=?", (rid,))
        for rid, name in new_rows:
            if rid >= 0 and name:
                con.execute("INSERT OR REPLACE INTO ft(rowid, n) VALUES(?,?)",
                            (rid, name))
        con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('live_updates',?)",
                    (str(now_ms()),))
        con.commit()
    finally:
        con.close()
    # 只读连接是长连接，关掉让它下次重新打开才能看到新数据
    global _fts_con, _fts_ok_for
    try:
        if _fts_con is not None:
            _fts_con.close()
    except sqlite3.Error:
        pass
    _fts_con = None
    _fts_ok_for = None


def span_text(e: Entry, span) -> str:
    """取命中片段的文字。kind 决定下标是相对哪个字符串算的。"""
    if not span:
        return ""
    s, en, kind = span
    if kind == "name":
        return e.name[s:en]
    if kind == "initials":
        return _get_initials(e)[s:en]
    if kind == "full":
        return _get_full(e)[s:en]
    if kind == "path":
        return e.path[s:en]
    return ""


def _span_of(e: Entry, neg_filter: Filter):
    """算出命中片段，供界面/命令行高亮。

    下标是**相对被命中的那个字符串**算的（名字 / 拼音键 / 整条路径），
    用 span_text() 取值，别自己拿路径的下标去切名字 —— 那会切出乱码。
    """
    if neg_filter.kind != "name":
        return None
    needle = neg_filter.text
    name_low = e.name.lower()
    q, pos, ln = name_match_quality(name_low, needle)
    if q > 0:
        return (pos, pos + ln, "name")
    # 拼音命中只对含汉字的条目有意义；纯 ASCII 名字的"拼音"就是它自己，不必再试
    if needle.isascii() and not e.name.isascii():
        key = needle.lower()
        ini = _get_initials(e)
        p = ini.find(key)
        if p >= 0:
            return (p, p + len(key), "initials")
        full = _get_full(e)
        p = full.find(key)
        if p >= 0:
            return (p, p + len(key), "full")
    # 只剩"命中在目录部分"这种情况（名字里没有，但路径里有）
    p = e.path_low.find(needle)
    if p >= 0:
        return (p, p + len(needle), "path")
    return None


def collect_filters(q: Query):
    """把 Query 摊平成"若干个 AND 分支，分支之间 OR"的形状。

    返回 list[list[(negated, Filter)]]：
      - 单个分支时就是普通的 AND 查询
      - 多个分支时它们之间是 OR（每个分支内部仍是 AND，`!` 在该分支内生效）

    注意 OR 分支里的 `!` 是"该分支的排除条件"，不是全局排除——
    这和 oil-find 把 `!` 放在 clause 内的语义一致。
    """
    groups = []
    for branch in (q.and_groups or q.clauses):
        if branch:
            groups.append(branch)
    return groups


def _branch_ok(e: Entry, branch) -> bool:
    has_pos = False
    for neg, f in branch:
        if neg:
            if f.match(e):
                return False
        else:
            has_pos = True
            if not f.match(e):
                return False
    return has_pos


class QueryAborted(Exception):
    """查询被更新的一次查询取代了（界面上用户又敲了一个字）。

    只在 run_query(should_abort=...) 传了回调时才可能抛出；
    没人监听它就永远不会抛，所以其它调用点（命令行、自检）行为不变。
    """


def run_query(index, q: Query, limit: int = 3000, exclude_noise: bool = True,
              should_abort=None):
    """两级执行：先一次性筛出候选，再算分数。返回 (hits, 耗时毫秒, 候选数)。

    全盘 200 多万条上做一次纯 Python 全扫大约 1–4 秒，这个代价躲不掉
    （oil-find 靠 C + SIMD 才能毫秒级）。所以加了结果缓存：
    同一个查询在索引没变的情况下第二次是瞬开的。

    should_abort：可选的回调。界面边打字边搜时，"打字"本身会不断产生新查询；
    旧查询如果没有及时退出，就会一条条排在服务端的锁后面，最后那条要等前面全部跑完
    —— 这就是"输入了却半天没有任何结果"的由来。传了这个回调，旧查询每扫几千条
    就问一次"我还需要吗"，不需要就立刻抛 QueryAborted 让位。
    """
    if q.empty:
        return [], 0.0, 0
    aborted = should_abort is not None

    def check(n=0):
        """每 4096 步查一次；n 传计数器，省掉一次取模以外的开销。"""
        if aborted and (n & 4095) == 0 and should_abort():
            raise QueryAborted()

    cache_key = (q.raw, limit, exclude_noise, index.built_at,
                 getattr(index, "version", 0))
    cached = _QUERY_CACHE.get(cache_key)
    if cached is not None:
        return cached[0], 0.0, cached[1]
    t0 = time.perf_counter()
    index.build_pinyin()
    branches = collect_filters(q)
    if not branches:
        return [], 0.0, 0
    is_or = len(branches) > 1

    # 让过滤器各自报出"我能给出的候选集"（拼音索引 / trigram 索引 / 扩展名索引），
    # 然后挑最小的那个当先筛起点，其余条件在这一小撮上验一遍。
    # 比"只挑一个条件当先筛、其它条件各自全扫"快得多。
    prepped = [[(f, f.prepare(index)) for neg, f in branch] for branch in branches]

    primary = None
    primary_cand = None
    direct = False
    if not is_or:
        branch = branches[0]
        best = None
        for (f, cand), (neg, _) in zip(prepped[0], branch):
            if neg or cand is None:
                continue
            n = len(cand)
            if best is None or n < best:
                best, primary, primary_cand = n, f, cand
        if primary is None and prepped[0]:
            primary = prepped[0][0][0]
        # 只有一个条件时，先筛结果就是最终结果（除非那个条件自己说需要复核）
        direct = (len(branch) == 1 and not branch[0][0])

    entries = index.query_entries()
    candidates = []
    if is_or:
        # OR：每个分支尽量用自己的索引拿候选再合并去重；
        # 某个分支拿不到索引候选时才退回全扫。
        seen = set()
        append_c = candidates.append

        def take_from(src, branch):
            for i, e in enumerate(src):
                check(i)
                if e.noise and exclude_noise:
                    continue
                if not _branch_ok(e, branch):
                    continue
                k = id(e)
                if k in seen:
                    continue
                seen.add(k)
                append_c(e)

        for bi, branch in enumerate(branches):
            has_neg = any(neg for neg, _ in branch)
            cand_lists = [(f, c) for (f, c), (neg, _) in
                          zip(prepped[bi], [b for b in branch]) if not neg and c is not None]
            if not has_neg and len(cand_lists) == 1 \
                    and len([1 for neg, _ in branch if not neg]) == 1:
                take_from(cand_lists[0][1], branch)
            elif cand_lists:
                take_from(min((c for _, c in cand_lists), key=len), branch)
            else:
                take_from(entries, branch)
    elif primary is not None:
        pm = primary.match
        skip_primary = primary.may_skip()
        src = primary_cand if primary_cand is not None else entries
        if direct:
            if primary.may_skip():
                candidates = list(src)
            else:
                for i, e in enumerate(src):
                    check(i)
                    if pm(e):
                        candidates.append(e)
        else:
            for i, e in enumerate(src):
                check(i)
                if not skip_primary and not pm(e):
                    continue
                ok = True
                for neg, f in branches[0]:
                    if f is primary:
                        continue
                    if neg:
                        if f.match(e):
                            ok = False
                            break
                    elif not f.match(e):
                        ok = False
                        break
                if ok:
                    candidates.append(e)
    else:
        branch = branches[0]
        for i, e in enumerate(entries):
            check(i)
            if _branch_ok(e, branch):
                candidates.append(e)

    # 文件监听可能把条目改成"已删除"，而拼音/扩展名/名字索引给出的候选里
    # 还带着它们，这里统一剔掉
    if index.gone_count:
        candidates = [e for e in candidates if not e.gone]

    # 用于打分的"主关键词"与高亮片段
    needle = None
    span_filter = None
    path_filter = None
    for branch in branches:
        for neg, f in branch:
            if neg:
                continue
            if f.kind == "name" and needle is None:
                needle = f.text
                span_filter = f
            elif f.kind == "path" and path_filter is None:
                path_filter = f

    # 路径查询的打分：命中离路径末尾越近越相关（"d:\agent相关对话" 应该先出它的直接子项）
    path_bonus = None
    if path_filter is not None:
        needle_path = path_filter.text.rstrip("\\")

        def path_bonus(e) -> int:
            """路径命中加成。分档差距要足够大：这样"直接位于该目录下"的条目
            一定排在"路径里别处刚好也含这个词"的条目之前，
            前缀命中子集也因此可以安全地代替全集参与打分。"""
            p = e.path_low
            pos = p.find(needle_path)
            if pos < 0:
                return 0
            tail = len(p) - (pos + len(needle_path))
            if tail == 0:
                return 3000           # 就是这个路径本身
            if p.startswith(needle_path):
                rel = p[len(needle_path):]
                if rel.count("\\") <= 1:
                    return 2500       # 直接子项
                return 2000           # 更深的子孙
            return 1000 - min(tail // 8, 300)

    # 候选已定，打分。
    # 若查询里既没有名字关键词、也没有路径加成（纯 size:/dm:/kind: 这类），
    # 所有候选的分数完全相同 —— 那就没必要排序，直接按索引给定顺序取前 N。
    # 这一步把 size:<1kb 这种 84 万候选的查询从 2 秒降到几毫秒。
    exclude = exclude_noise
    use_bonus = path_bonus is not None
    # 没有名字关键词时（纯 size:/dm:/kind:/path: 查询），候选之间差不出多少分。
    # 候选又特别多的话就按索引顺序取前 N，别为几十万条做一遍没有意义的排序。
    if needle is None and (not use_bonus or len(candidates) > 200000):
        hits = []
        n_take = 0
        for i, e in enumerate(candidates):
            check(i)
            if exclude and e.noise:
                continue
            hits.append(Hit(e, name_relevance(e.low, 1, e.depth), None))
            n_take += 1
            if n_take >= limit:
                break
        dt = (time.perf_counter() - t0) * 1000
        if len(_QUERY_CACHE) > 48:
            _QUERY_CACHE.clear()
        _QUERY_CACHE[cache_key] = (hits, len(candidates))
        return hits, dt, len(candidates)

    cs = q.case_sensitive
    verified = bool(needle) and span_filter is not None and span_filter.may_skip()
    nlen = len(needle) if needle else 0

    def score_one(e) -> int:
        name_low = e.low
        if not needle:
            return name_relevance(name_low, 1, e.depth)
        if verified:
            ql = _quality_at(name_low, name_low.find(needle), nlen)
        else:
            mq, _, _ = name_match_quality(name_low, needle, cs)
            ql = mq if mq > 0 else 1
        return name_relevance(name_low, ql, e.depth)

    def coarse(e) -> int:
        return score_one(e)

    pool = []
    for i, e in enumerate(candidates):
        check(i)
        if not (exclude and e.noise):
            pool.append(e)
    if len(pool) > 20000:
        # 粗排取前 4000 条再做精排（粗排分不含路径加成，是真实分的上界，不会漏最优项）
        pool = heapq.nlargest(4000, pool, key=coarse)

    hits = []
    append = hits.append
    for i, e in enumerate(pool):
        check(i)
        s = score_one(e)
        if use_bonus:
            s += path_bonus(e)
        append(Hit(e, s, None))

    key = lambda h: (-h.score, h.entry.path_low)
    if len(hits) > limit:
        # 候选很多时用 nlargest 控制内存，别对几十万条整体排序
        hits = heapq.nlargest(limit, hits, key=lambda h: (h.score, ))
        hits.sort(key=key)
    else:
        hits.sort(key=key)
    if span_filter is not None:
        for h in hits:
            h.span = _span_of(h.entry, span_filter)
    dt = (time.perf_counter() - t0) * 1000
    if len(_QUERY_CACHE) > 48:
        _QUERY_CACHE.clear()
    _QUERY_CACHE[cache_key] = (hits, len(candidates))
    return hits, dt, len(candidates)


# ---------------------------------------------------------------------------
# 文件索引引擎
# ---------------------------------------------------------------------------
# 三件事：
#   1. scandir 遍历卷（跳过重解析点，避免软链接环和跨盘递归）
#   2. 每个条目一个 Entry 对象，名字常驻内存 —— 靠 Python 的 str.find 做子串匹配
#   3. JSONL+gzip 持久化，启动时秒级加载；delta 刷新按 mtime 比对

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE = 0x400

HOTKEY_MSG_ID = 0x0312  # WM_HOTKEY

SKIP_ROOT_NAMES = {"$recycle.bin", "system volume information", "$windows.~ws", "$windows.~bt"}


def _classify(path_low: str, name_low: str, parent_noise: bool, is_dir: bool):
    """算 (noise, hidden)。noise 一旦命中就继承给整棵子树（对齐 oil-find 的 noise 标记）。"""
    hidden = name_low.startswith(".") or name_low in ("desktop.ini", "thumbs.db")
    noise = parent_noise
    if not noise:
        if is_dir and name_low in NOISE_DIR_NAMES:
            noise = True
        elif is_noise_path(path_low):
            noise = True
    return noise, hidden


# 增量更新用的常量：
# 新增的条目（不在磁盘索引里）放进 overlay 列表，FTS 里的 rowid 用 OVERLAY_BASE+序号，
# 这样"索引来的条目"和"后来新出现的条目"能在同一套查询里共存。
OVERLAY_BASE = 1 << 40

# 文件监听：每个卷一个句柄、递归监听，收到变更后只更新受影响的那几条。
FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
FILE_NOTIFY_CHANGE_SIZE = 0x00000008
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
WATCH_FLAGS = (FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME
               | FILE_NOTIFY_CHANGE_SIZE | FILE_NOTIFY_CHANGE_LAST_WRITE)


class FileWatcher(threading.Thread):
    """用 ReadDirectoryChangesW 递归监听若干根目录，把变更交给引擎增量应用。

    为什么不用"定期全盘重扫"：全盘一次要八分钟，机器大部分时间都在扫盘。
    监听则只在真有变化时才动，代价几乎为零。

    缓冲区溢出（短时间大量变更）时无法知道丢了什么，就让引擎安排一次全量重扫兜底。
    """

    def __init__(self, engine, roots, on_change=None, debounce=0.5,
                 max_batch=3000, stop_event=None):
        super().__init__(daemon=True, name="dshfind-watcher")
        self.engine = engine
        self.roots = [os.path.abspath(r) for r in roots]
        self.on_change = on_change
        self.debounce = debounce
        self.max_batch = max_batch
        self.stop_event = stop_event or threading.Event()
        self.pending = set()
        self.pending_lock = threading.Lock()
        self.applied = 0
        self.overflowed = False

    def run(self):
        threads = []
        for root in self.roots:
            t = threading.Thread(target=self._watch_root, args=(root,),
                                 daemon=True, name="watch:%s" % root)
            t.start()
            threads.append(t)
        # 汇总线程：把短时间内收到的变更攒起来，一把交给引擎
        while not self.stop_event.is_set():
            time.sleep(self.debounce)
            batch = self._take()
            if not batch:
                continue
            if self.overflowed:
                self.overflowed = False
                if self.on_change:
                    self.on_change({"overflow": True, "paths": []})
                continue
            try:
                res = self.engine.apply_changes(batch)
                self.applied += res.get("changed", 0)
                if self.on_change:
                    self.on_change(res)
            except Exception:
                traceback.print_exc()

    def _take(self):
        with self.pending_lock:
            if not self.pending:
                return []
            batch = list(self.pending)
            self.pending.clear()
            if len(batch) > self.max_batch:
                self.overflowed = True
                return []
        return batch

    def _watch_root(self, root):
        """阻塞式读一个卷的变更。句柄按目录打开（FILE_FLAG_BACKUP_SEMANTICS）。"""
        import ctypes
        from ctypes import wintypes as wt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        GENERIC_READ = 0x80000000
        FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

        k32.CreateFileW.restype = wt.HANDLE
        k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID,
                                    wt.DWORD, wt.DWORD, wt.HANDLE]
        h = k32.CreateFileW(root, GENERIC_READ, FILE_SHARE_ALL, None,
                            OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
        if h == wt.HANDLE(-1).value or not h:
            print("[dshfind] 监听 %s 失败（句柄打不开）" % root, file=sys.stderr)
            return
        buf = ctypes.create_string_buffer(64 * 1024)
        returned = wt.DWORD(0)
        try:
            while not self.stop_event.is_set():
                ok = k32.ReadDirectoryChangesW(
                    h, buf, len(buf), True, WATCH_FLAGS,
                    ctypes.byref(returned), None, None)
                if not ok:
                    err = ctypes.get_last_error()
                    if err == 1223:      # ERROR_CANCELLED，句柄被关
                        return
                    # 缓冲区不够等情况：交给上层做一次全量重扫
                    with self.pending_lock:
                        self.overflowed = True
                        self.pending.add(root)
                    time.sleep(1.0)
                    continue
                if returned.value == 0:
                    continue
                with self.pending_lock:
                    self._parse(buf.raw[:returned.value], root)
        finally:
            try:
                k32.CloseHandle(h)
            except Exception:
                pass

    def _parse(self, data, root):
        """解析 FILE_NOTIFY_INFORMATION 链，把完整路径塞进 pending。"""
        import struct
        off = 0
        n = len(data)
        while off + 12 <= n:
            next_off, action, name_len = struct.unpack_from("<III", data, off)
            name = data[off + 12:off + 12 + name_len].decode("utf-16-le", "replace")
            if name:
                self.pending.add((root.rstrip("\\") + "\\" + name).lower())
            if next_off == 0:
                break
            off += next_off


class ScanStats:
    __slots__ = ("files", "dirs", "skipped", "bytes", "seconds", "truncated", "errors")

    def __init__(self):
        self.files = 0
        self.dirs = 0
        self.skipped = 0
        self.bytes = 0
        self.seconds = 0.0
        self.truncated = []
        self.errors = 0

    def as_dict(self):
        return {
            "files": self.files, "dirs": self.dirs, "skipped": self.skipped,
            "gb": round(self.bytes / 1e9, 1), "seconds": round(self.seconds, 1),
            "truncated": self.truncated, "errors": self.errors,
        }


class IndexEngine:
    """内存文件名索引。"""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or load_settings()
        self.entries: list[Entry] = []
        self.by_key: dict[str, Entry] = {}      # 小写路径 -> Entry，delta 刷新用
        self._by_key_ready = True
        self.paths_orig = ""
        self.paths_low = ""
        self.built_at = 0
        self.stats = ScanStats()
        self.lock = threading.RLock()
        self.pinyin = PinyinIndex()
        self.pinyin_ready = False
        self.ext_index = {}
        self.ext_ready = False
        self.sorted = SortedIndex()
        self.sorted_ready = False
        # 文件监听带来的增量：新增/变化的条目放这里；删除的条目标 gone。
        # 这样"一次全盘扫出来的静态索引"也能持续反映磁盘变化，不需要手动重扫。
        self.overlay: list[Entry] = []
        self.overlay_by_path: dict[str, Entry] = {}
        self.pending_changes = 0
        self.last_change_ms = 0
        self.gone_count = 0
        # 增量更新的次数。查询结果缓存要把它算进 key，否则监听改了索引之后
        # 同一个查询还会返回旧结果（实测踩过）。
        self.version = 0
        self._hash_ready = False
        self._hash_sorted = None      # array('q') 升序的路径哈希
        self._hash_ids = None         # array('i') 对应的条目下标
        # 条目在「路径大字符串」里的起点（升序），供路径查询二分回查。
        # 用 'i'（4 字节）省内存；路径文本总量超 2GB 时放弃（退化成全扫）。
        self.starts = None
        self.progress = {"phase": "idle", "root": "", "files": 0, "dirs": 0,
                         "elapsed": 0.0, "done": 0, "total": 0}
        self.cancel = threading.Event()

    def path_candidates(self, needle: str):
        """路径里含 needle 的条目。

        在整块小写路径大字符串上做一次 C 层 find（391MB 也只要几百毫秒），
        再用条目起点数组二分回查下标 —— 比逐条 343 万次 in 快一个数量级。

        顺带把"命中就落在路径开头"（也就是该条目本身就在 needle 指的目录下）
        的条目单独收集。这类条目在打分时必然排在最前，数量够多时只算它们就够，
        不必为几十万条无关条目再跑一遍打分。
        """
        blob = self.paths_low
        starts = self.starts
        if not blob or not starts:
            return None
        out = []
        pref = []
        entries = self.entries
        n = len(entries)
        cap = 300_000     # 上限：反正只显示前几十条，没必要把上百万条都搬出来
        i = 0
        while True:
            i = blob.find(needle, i)
            if i < 0:
                break
            k = bisect.bisect_right(starts, i) - 1
            if 0 <= k < n:
                e = entries[k]
                out.append(e)
                if i == starts[k]:
                    pref.append(e)
            if len(out) >= cap:
                break
            i += 1
        # 前缀命中够多时，后面的条目分数不可能超过它们（见 path_bonus 的分档）
        if len(pref) >= 4000:
            return pref
        return out

    def ensure_by_key(self):
        """按需建「小写路径 -> 条目」字典。

        加载索引时不建（343 万条要好几秒、几百 MB，只有整批刷新才用得到）；
        真需要时再花这个时间。键必须是整条路径（path_low），不是文件名 ——
        文件名会重名，拿它当键会让刷新比对全乱。
        """
        if self._by_key_ready:
            return
        by_key = {}
        for e in self.entries:
            by_key[e.path_low] = e
        self.by_key = by_key
        self._by_key_ready = True

    # -- 增量更新（文件监听用）--------------------------------------------
    def lookup_path(self, path_low: str):
        """按小写整条路径找条目，返回 (entry, 来源)；来源 'blob' / 'overlay'。

        优先走**名字索引**定位：每个磁盘条目在名字 FTS 里都有一行，
        所以"按文件名查出候选 rowid、再比一次完整路径"就能确定它是否已索引，
        单次约 1～2 毫秒。
        名字短于 3 字符（trigram 用不了）或名字索引与文件索引不同源时，
        退回在「所有路径拼成的大字符串」上直接 find —— 慢（约 0.5 秒）
        但保证正确。
        """
        e = self.overlay_by_path.get(path_low)
        if e is not None:
            return e, "overlay"
        name = path_low.rsplit("\\", 1)[-1]
        if len(name) >= FTS_MIN_NEEDLE:
            rowids = fts_search(name, self, limit=400)
            if rowids is not None:
                entries = self.entries
                overlay = self.overlay
                n = len(entries)
                for r in rowids:
                    if r >= OVERLAY_BASE:
                        k = r - OVERLAY_BASE
                        if 0 <= k < len(overlay):
                            cand = overlay[k]
                            if cand.path_low == path_low:
                                return cand, "overlay"
                    elif 0 <= r < n:
                        cand = entries[r]
                        if cand.path_low == path_low:
                            return cand, "blob"
                return None, None
        # 兜底：大字符串直接找
        blob = self.paths_low
        starts = self.starts
        if not blob or starts is None:
            return None, None
        entries = self.entries
        n = len(entries)
        i = blob.find(path_low)
        while i >= 0:
            k = bisect.bisect_right(starts, i) - 1
            if 0 <= k < n:
                s = starts[k]
                cand = entries[k]
                if i == s and blob[s:s + len(path_low)] == path_low:
                    return cand, "blob"
            i = blob.find(path_low, i + 1)
        return None, None

    def apply_changes(self, paths_low) -> dict:
        """把一批变更路径反映到索引里（新增 / 更新大小与时间 / 标记删除）。

        这是"不用手动刷新"的核心：监听线程收到变更，这里只动受影响的那几条，
        不重扫全盘。

        耗时的 stat/查找都在**锁外**做完，最后只持锁做一小段纯内存修改，
        这样用户在索引更新时输入搜索不会被卡住。
        """
        prepared = []
        for p_low in paths_low:
            try:
                st = os.stat(p_low)
                is_dir = os.path.isdir(p_low)
                info = (True, is_dir, 0 if is_dir else st.st_size,
                        clamp_mtime(st.st_mtime * 1000))
            except OSError:
                info = (False, False, 0, 0)
            entry, src = self.lookup_path(p_low)
            prepared.append((p_low, info, entry, src))

        added = updated = removed = 0
        fts_new, fts_del = [], []
        with self.lock:
            for p_low, (exists, is_dir, size, mtime), entry, src in prepared:
                if not exists:
                    if entry is not None and not entry.gone:
                        entry.gone = True
                        self.gone_count += 1
                        removed += 1
                        fts_del.append(self._blob_id(entry) if src == "blob"
                                       else OVERLAY_BASE + self.overlay.index(entry))
                    continue
                if entry is not None:
                    if entry.gone:
                        entry.gone = False
                        self.gone_count -= 1
                        rid = (self._blob_id(entry) if src == "blob"
                               else OVERLAY_BASE + self.overlay.index(entry))
                        fts_new.append((rid, entry.low))
                    if entry.size != size or entry.mtime_ms != mtime:
                        entry.size = size
                        entry.mtime_ms = mtime
                        updated += 1
                    continue
                # 新条目
                name = p_low.rsplit("\\", 1)[-1]
                parent = p_low[:p_low.rfind("\\")]
                pe, _ = self.lookup_path(parent)
                pnoise = bool(pe.noise) if pe is not None else False
                depth = (pe.depth + 1) if pe is not None else max(0, p_low.count("\\") - 1)
                noise = pnoise or (is_dir and name in NOISE_DIR_NAMES) \
                    or (NOISE_MARKER_RX.search(p_low) is not None)
                e = Entry(path=p_low, name=name, low=name, is_dir=is_dir,
                          size=size, mtime_ms=mtime, depth=depth,
                          noise=noise, hidden=name.startswith("."))
                self.overlay.append(e)
                self.overlay_by_path[p_low] = e
                fts_new.append((OVERLAY_BASE + len(self.overlay) - 1, name))
                if self.ext_ready and not is_dir:
                    ext = e.ext
                    if ext:
                        self.ext_index.setdefault(ext, []).append(e)
                if self.pinyin_ready and not name.isascii():
                    self.pinyin.add_entry(e)
                added += 1
            if added or removed:
                self.sorted_ready = False      # 排序索引得重建（惰性，用到才建）
            self.pending_changes += added + updated + removed
            self.last_change_ms = now_ms()
            if added or updated or removed:
                self.version += 1
        if fts_new or fts_del:
            try:
                _fts_apply(fts_new, fts_del)
            except Exception:
                pass
        return {"changed": added + updated + removed, "added": added,
                "removed": removed, "updated": updated}

    def _blob_id(self, entry) -> int:
        """blob 条目的下标。用起点偏移二分回查，不建额外字典。"""
        if not self.starts:
            return -1
        k = bisect.bisect_left(self.starts, entry._s)
        return k if 0 <= k < len(self.entries) else -1

    # -- 增量落盘 ---------------------------------------------------------
    def save_delta(self, path: str = None) -> bool:
        """把"监听产生的增量"（新增条目 + 已删除标记）存到一个小文件。

        不存的话，重启后新建的文件会从搜索结果里消失，得等下次全量重扫才回来。
        这个文件通常只有几十 KB。
        """
        path = path or DELTA_PATH
        try:
            with self.lock:
                overlay = [[e.path, 1 if e.is_dir else 0, int(e.size),
                            int(e.mtime_ms), int(e.depth), 1 if e.noise else 0]
                           for e in self.overlay if not e.gone]
                gone = []
                if self.gone_count:
                    for e in self.entries:
                        if e.gone:
                            gone.append(e.path)
                    for e in self.overlay:
                        if e.gone:
                            gone.append(e.path)
                data = {"v": 1, "saved": now_ms(),
                        "overlay": overlay, "gone": gone}
            ensure_dirs()
            # tmp 名带上进程与线程号：两个 dshfind 同时跑（比如调试时另起一个）时，
            # 共用一个 "delta.json.tmp" 会互相把对方的临时文件搬走，报 WinError 2
            tmp = "%s.%d-%d.tmp" % (path, os.getpid(), threading.get_ident())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            replace_with_retry(tmp, path)
            return True
        except Exception as e:
            print("[dshfind] 增量落盘失败：%s" % e, file=sys.stderr)
            return False

    def load_delta(self, path: str = None) -> int:
        """启动时把上次的增量读回来（放在 build_ext_index 之前调用）。"""
        path = path or DELTA_PATH
        if not os.path.exists(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return 0
        n = 0
        with self.lock:
            for row in data.get("overlay", []):
                try:
                    p, is_dir, size, mtime, depth, noise = row
                except (TypeError, ValueError):
                    continue
                p_low = p.lower()
                if p_low in self.overlay_by_path:
                    continue
                e = Entry(path=p_low, name=p_low.rsplit("\\", 1)[-1], low=p_low.rsplit("\\", 1)[-1],
                          is_dir=bool(is_dir), size=int(size), mtime_ms=clamp_mtime(mtime),
                          depth=int(depth), noise=bool(noise),
                          hidden=p_low.rsplit("\\", 1)[-1].startswith("."))
                self.overlay.append(e)
                self.overlay_by_path[p_low] = e
                n += 1
            for p in data.get("gone", []):
                e, _ = self.lookup_path(p.lower())
                if e is not None and not e.gone:
                    e.gone = True
                    self.gone_count += 1
        return n

    def query_entries(self):
        """查询用的条目来源：磁盘索引（跳过已删除的）+ 增量新增的。"""
        if not self.overlay and not self.gone_count:
            return self.entries
        return itertools.chain(
            (e for e in self.entries if not e.gone),
            (e for e in self.overlay if not e.gone))

    def build_pinyin(self) -> None:
        """预建拼音索引（343 万条目里只有 1 万左右含汉字，几秒的事）。"""
        if self.pinyin_ready:
            return
        t0 = time.perf_counter()
        self.pinyin.build(self.entries)
        # 文件监听新增的条目也要进拼音索引（含汉字时才真正加入）
        for e in self.overlay:
            if not e.gone:
                self.pinyin.add_entry(e)
        self.pinyin_ready = True
        self.pinyin_seconds = time.perf_counter() - t0

    def build_ext_index(self) -> None:
        """扩展名 -> 条目列表。有了它 `kind:image` / `ext:png` / `*.png` 不用全扫。

        343 万条目里文件约 294 万，建一次约 2～4 秒。
        结果按扩展名缓存成一个 dict，后续查询直接切片，几乎零成本。
        """
        if self.ext_ready:
            return
        t0 = time.perf_counter()
        idx = {}
        path_low = self.paths_low
        # 两种情况都要支持：从磁盘加载的 BlobEntry（有 _s/_e，可直接在小写路径大串上
        # 定位）和刚扫描出来的 Entry（只有 name）。前者走切片更快。
        sliced = bool(path_low)
        for e in self.entries:
            if e.is_dir:
                continue
            if sliced:
                s = e._s
                en = e._e
                dot = path_low.rfind(".", s, en)
                if dot <= s:
                    continue
                if dot < path_low.rfind("\\", s, en):
                    continue
                ext = path_low[dot + 1:en]
            else:
                n = e.name
                dot = n.rfind(".")
                if dot <= 0:
                    continue
                ext = n[dot + 1:].lower()
            bucket = idx.get(ext)
            if bucket is None:
                idx[ext] = [e]
            else:
                bucket.append(e)
        # 监听新增的文件同样要能被 ext:/kind:/通配符 找到
        for e in self.overlay:
            if e.is_dir or e.gone:
                continue
            ext = e.ext
            if ext:
                idx.setdefault(ext, []).append(e)
        self.ext_index = idx
        self.ext_ready = True
        self.ext_seconds = time.perf_counter() - t0

    def build_sorted(self) -> None:
        """按大小/修改时间排序的索引。惰性建，建一次约 12 秒，之后缓存到磁盘。"""
        if self.sorted_ready:
            return
        if self.sorted.load() and not sorted_is_stale(self.sorted, self):
            self.sorted_ready = True
            return
        self.sorted.build(self.entries, self.overlay)
        self.sorted_ready = True
        self.sorted.save()
        sorted_save_stamp(self)

    # -- 构建 -------------------------------------------------------------
    def build(self, roots=None, budget=None, on_progress=None, cap=None):
        roots = roots or self.settings["roots"]
        budget = budget or self.settings.get("per_root_budget", ROOT_SCAN_BUDGET)
        cap = cap or int(self.settings.get("max_entries") or MAX_ENTRIES)
        t_all = time.perf_counter()
        entries: list[Entry] = []
        by_key: dict[str, Entry] = {}
        stats = ScanStats()
        self.cancel.clear()

        for root in roots:
            root = os.path.abspath(root)
            if not os.path.isdir(root):
                stats.truncated.append(root + " (不存在)")
                continue
            if cap and len(entries) >= cap:
                stats.truncated.append(root + " (已达条目上限 %s)" % "{:,}".format(cap))
                break
            root_low = norm_key(root).rstrip("\\")   # "D:\" -> "d:"，"D:\a" -> "d:\a"
            # 卷根本身也进索引，这样搜 "D:" 能看到盘符条目
            try:
                st = os.stat(root)
                root_entry = Entry(
                    path=root, name=root.rstrip("\\") or root, low=root_low,
                    is_dir=True, size=0, mtime_ms=clamp_mtime(st.st_mtime * 1000), depth=-1,
                    noise=False, hidden=False)
                entries.append(root_entry)
                by_key[norm_key(root)] = root_entry
            except OSError:
                pass

            t_root = time.perf_counter()
            stack = [(root, root_low, False, -1)]
            while stack:
                if self.cancel.is_set():
                    stats.truncated.append(root + " (已取消)")
                    break
                if time.perf_counter() - t_root > budget:
                    stats.truncated.append(root + " (超时预算)")
                    break
                d, d_low, d_noise, d_depth = stack.pop()
                prefix = d_low if d_low.endswith("\\") else d_low + "\\"
                try:
                    with os.scandir(d) as it:
                        for e in it:
                            try:
                                st = e.stat(follow_symlinks=False)
                            except OSError:
                                stats.errors += 1
                                continue
                            attrs = getattr(st, "st_file_attributes", 0)
                            name = e.name
                            name_low = name.lower()
                            path_low = prefix + name_low
                            is_dir = bool(attrs & 0x10) or e.is_dir(follow_symlinks=False)
                            if attrs & FILE_ATTRIBUTE_REPARSE:
                                stats.skipped += 1
                                continue
                            if is_dir and d_depth < 0 and name_low in SKIP_ROOT_NAMES:
                                stats.skipped += 1
                                continue
                            noise, hidden = _classify(path_low, name_low, d_noise, is_dir)
                            depth = d_depth + 1
                            path = e.path
                            entries.append(Entry(
                                path=path, name=name, low=name_low, is_dir=is_dir,
                                size=0 if is_dir else st.st_size,
                                mtime_ms=clamp_mtime(st.st_mtime * 1000),
                                depth=depth, noise=noise, hidden=hidden))
                            by_key[path_low] = entries[-1]
                            if is_dir:
                                stats.dirs += 1
                                stack.append((path, path_low + "\\", noise, depth))
                            else:
                                stats.files += 1
                                stats.bytes += st.st_size
                except OSError:
                    stats.errors += 1
                # 进度回调（按目录节流）
                if on_progress and (stats.files & 0x1FFF) == 0:
                    on_progress(root, stats.files, stats.dirs,
                                time.perf_counter() - t_root)
            if self.cancel.is_set():
                break

        stats.seconds = time.perf_counter() - t_all
        with self.lock:
            # 卷根条目统一放宽打分别用
            self.entries = entries
            self.by_key = by_key
            self.stats = stats
            self.built_at = now_ms()
            self.pinyin_ready = False
            self.pinyin = PinyinIndex()
        return stats

    # -- 持久化 -----------------------------------------------------------
    def save(self, path: str = INDEX_PATH):
        """列式二进制：一份路径大字符串 + 几条扁平列，整体 gzip。

        为什么用扁平列而不是"每个条目一个对象"：
        - 存的时候不用逐条建 dict/tuple，省十几秒
        - 载入时 marshal 一次拿到所有列，几乎零解析成本
        - 对象延迟到 load 里一次性建，且可以只建需要的部分
        注意列必须是 list 而不是 array.array：marshal 会把 array 存成裸 bytes，
        读回来就不是序列类型了。

        **路径一律小写后存盘**：索引只保留一份路径字符串（省 300 多 MB），
        匹配又必须大小写不敏感，所以落盘就存小写形式，显示时用
        GetLongPathNameW 向系统要回真实大小写（见 display_path）。
        这里漏转小写会导致"小写关键词匹配不到大写文件名"，这是个很容易漏掉的坑。
        """
        ensure_dirs()
        tmp = path + ".tmp"
        n = len(self.entries)
        paths = [None] * n
        starts = [0] * n
        ends = [0] * n
        dirs = [0] * n
        sizes = [0] * n
        mtimes = [0] * n
        depths = [0] * n
        pos = 0
        for i, e in enumerate(self.entries):
            p = e.path.lower()
            paths[i] = p
            ln = len(p)
            starts[i] = pos
            ends[i] = pos + ln
            pos += ln + 1
            if e.is_dir:
                dirs[i] = 1
            else:
                sizes[i] = e.size
            mtimes[i] = e.mtime_ms
            d = e.depth
            depths[i] = d if -2000000000 < d < 2000000000 else 0
        meta = {
            "v": 2, "lower": 1, "built_at": self.built_at,
            "roots": self.settings["roots"], "stats": self.stats.as_dict(),
        }
        payload = marshal.dumps({
            "meta": meta, "paths": "\n".join(paths),
            "starts": starts, "ends": ends, "dirs": dirs,
            "sizes": sizes, "mtimes": mtimes, "depths": depths,
        }, 4)
        with gzip.open(tmp, "wb", compresslevel=4) as f:
            f.write(payload)
        replace_with_retry(tmp, path)
        return path

    def load(self, path: str = INDEX_PATH) -> bool:
        """加载索引。

        213 万条目是纯 Python 对象，单线程造对象要 6.5 秒。
        拆成四段并行后约 2.6 秒，加上解压/反序列化/转小写，总计 5～6 秒。
        """
        if not os.path.exists(path):
            return False
        t0 = time.perf_counter()
        try:
            with gzip.open(path, "rb") as f:
                blob = marshal.loads(f.read())
        except Exception as e:
            print("[dshfind] 索引加载失败：%s" % e, file=sys.stderr)
            return False
        if not isinstance(blob, dict) or blob.get("meta", {}).get("v") != 2:
            return self._load_legacy(path)
        if "starts" not in blob:
            return self._migrate_v2_offsets(blob)

        joined = blob["paths"]
        starts = blob["starts"]
        ends = blob["ends"]
        dirs = blob["dirs"]
        sizes = blob["sizes"]
        mtimes = blob["mtimes"]
        depths = blob["depths"]
        n = len(starts)
        # 列长度必须一致，否则是坏文件（比如早期把 array 存成裸 bytes 的那版）
        if not (isinstance(starts, list) and len(ends) == n and len(dirs) == n
                and len(sizes) == n and len(mtimes) == n and len(depths) == n):
            print("[dshfind] 索引列长度不一致，当作损坏处理，请重新 --reindex",
                  file=sys.stderr)
            return False

        # 路径只存一份小写形式。Windows 路径不区分大小写，检索时用这一份，
        # 显示时再用系统 API（GetLongPathNameW）取回真实大小写。
        # 存两份（原文 + 小写）会多占约 330MB，对 300 多万条目的规模来说不划算。
        #
        # 老格式（meta 里没有 lower 标记）存的是原始大小写，这里补转一次。
        # 注意不能直接 joined.lower() —— 少数 Unicode 字符（如 U+0130）小写后
        # 长度会变，偏移量就全错位了。按条目切分后逐段转，再重算偏移。
        if not blob["meta"].get("lower"):
            parts = joined.split("\n")
            if len(parts) != n:
                print("[dshfind] 老索引无法安全转换，请重新 --reindex", file=sys.stderr)
                return False
            parts = [p.lower() for p in parts]
            joined = "\n".join(parts)
            starts = []
            ends = []
            pos = 0
            for p in parts:
                starts.append(pos)
                ends.append(pos + len(p))
                pos += len(p) + 1
        _ENGINE.paths_orig = joined
        _ENGINE.paths_low = joined
        self.paths_orig = joined
        self.paths_low = joined

        noise_names = NOISE_DIR_NAMES
        noise_rx = NOISE_MARKER_RX
        low_src = joined
        mk = BlobEntry

        # 噪音标记：**按父目录判定，不能靠顺序继承**。
        # 扫描是"一个目录的子项连续排列"，不是深度优先 —— 目录和它的子孙并不相邻，
        # 所以任何基于栈的继承都会错位。这里改成查表：每个目录的噪音状态只算一次，
        # 子目录递归到父目录（带缓存，全盘只有 48 万个目录）。
        dir_noise_cache = {}

        def dir_noise_of(p_low: str) -> bool:
            v = dir_noise_cache.get(p_low)
            if v is not None:
                return v
            cut = p_low.rfind("\\")
            nm = p_low[cut + 1:]
            own = (nm in noise_names) or (noise_rx.search(p_low) is not None)
            if not own and cut > 0:
                own = dir_noise_of(p_low[:cut])
            dir_noise_cache[p_low] = own
            return own

        entries = [None] * n
        for i in range(n):
            s = starts[i]
            e_i = ends[i]
            cut = low_src.rfind("\\", s, e_i)
            if dirs[i]:
                noise = dir_noise_of(low_src[s:e_i])
            else:
                noise = dir_noise_of(low_src[s:cut]) if cut > s else False
            entries[i] = mk(s, e_i, bool(dirs[i]), sizes[i], mtimes[i],
                            depths[i], noise, False)

        with self.lock:
            self.entries = entries
            self.by_key = {}
            self._by_key_ready = False
            self.built_at = blob["meta"].get("built_at", 0)
            st = blob["meta"].get("stats") or {}
            s = ScanStats()
            s.files = st.get("files", 0)
            s.dirs = st.get("dirs", 0)
            s.bytes = int(st.get("gb", 0) * 1e9)
            s.seconds = st.get("seconds", 0)
            s.truncated = st.get("truncated", [])
            self.stats = s
            self.pinyin_ready = False
            self.pinyin = PinyinIndex()
            self.ext_index = {}
            self.ext_ready = False
        self.load_seconds = time.perf_counter() - t0
        # 路径查询要用的起点数组（升序）。总量超 2GB 就不建，走全扫兜底。
        if len(joined) < 2 ** 31 - 1:
            try:
                self.starts = array.array("i", starts)
            except (OverflowError, TypeError, ValueError) as e:
                print("[dshfind] 路径起点数组没建成（路径查询会退化为全扫）：%s" % e,
                      file=sys.stderr)
                self.starts = None
        # 先把上次的增量（监听产生的新增/删除）读回来，
        # 再建扩展名索引，保证下面几个索引都包含增量条目
        try:
            self.delta_loaded = self.load_delta()
        except Exception:
            self.delta_loaded = 0
        # 扩展名索引建一次约 2.5 秒，但能换来 *.png / kind:image 从几秒到几毫秒。
        # 在加载末尾顺手建掉，免得第一次查询卡一下。
        try:
            self.build_ext_index()
        except Exception:
            pass
        return True

    def _migrate_v2_offsets(self, blob) -> bool:
        """上一版（offsets/cols 两个 list）的格式。

        那版存的是原始大小写路径，转成现在的小写单份结构需要重算偏移，
        成本不比重新扫盘低，直接放弃让调用方走 --reindex，避免出错。
        """
        return False

    def _migrate_v2_offsets_disabled(self, blob) -> bool:
        joined = blob["paths"]
        offsets = blob["offsets"]
        cols = blob["cols"]
        joined_low = joined.lower()
        _ENGINE.paths_orig = joined
        _ENGINE.paths_low = joined_low
        self.paths_orig = joined
        self.paths_low = joined_low
        noise_names = NOISE_DIR_NAMES
        noise_rx = NOISE_MARKER_RX
        mk = BlobEntry
        entries = [None] * len(offsets)
        dir_noise_stack = [(0, False)]
        for i in range(len(offsets)):
            s, e = offsets[i]
            is_dir, size, mtime, depth = cols[i]
            while len(dir_noise_stack) > 1 and s >= dir_noise_stack[-1][0]:
                dir_noise_stack.pop()
            inherited = dir_noise_stack[-1][1]
            if is_dir:
                low = joined_low[s:e]
                noise = inherited or (low[low.rfind("\\") + 1:] in noise_names) \
                    or noise_rx.search(low) is not None
                dir_noise_stack.append((e + 1, noise))
            else:
                noise = inherited
            entries[i] = mk(s, e, is_dir, size, mtime, depth, noise, False)
        with self.lock:
            self.entries = entries
            self.by_key = {}
            self._by_key_ready = False
            self.built_at = blob["meta"].get("built_at", 0)
            st = blob["meta"].get("stats") or {}
            s = ScanStats()
            s.files = st.get("files", 0)
            s.dirs = st.get("dirs", 0)
            s.bytes = int(st.get("gb", 0) * 1e9)
            s.seconds = st.get("seconds", 0)
            s.truncated = st.get("truncated", [])
            self.stats = s
            self.pinyin_ready = False
            self.pinyin = PinyinIndex()
        self.needs_resave = True
        return True

    def _load_legacy(self, path: str) -> bool:
        """兼容更早的两种格式（marshal 元组列表 / JSONL）。"""
        try:
            with gzip.open(path, "rb") as f:
                raw = f.read()
            try:
                old = marshal.loads(raw)
            except Exception:
                return self._load_jsonl(path)
            if not old or old[0][0] != "_meta":
                return False
            meta = old[0]
            entries = []
            for t in old[1:]:
                path_s, is_dir, size, mtime, depth = t
                name = path_s.rsplit("\\", 1)[-1] or path_s
                low = name.lower()
                path_low = path_s.lower()
                hidden = low.startswith(".") or low in ("desktop.ini", "thumbs.db")
                noise = (low in NOISE_DIR_NAMES) if is_dir else is_noise_path(path_low)
                entries.append(Entry(path=path_s, name=name, low=low, is_dir=bool(is_dir),
                                     size=size, mtime_ms=mtime, depth=depth,
                                     noise=noise, hidden=hidden))
            with self.lock:
                self.entries = entries
                self.by_key = {e.path.lower(): e for e in entries}
                self._by_key_ready = True
                self.built_at = meta[2]
                st = meta[4] or {}
                s = ScanStats()
                s.files = st.get("files", 0)
                s.dirs = st.get("dirs", 0)
                s.bytes = int(st.get("gb", 0) * 1e9)
                s.seconds = st.get("seconds", 0)
                self.stats = s
                self.pinyin_ready = False
                self.pinyin = PinyinIndex()
            return True
        except Exception as e:
            print("[dshfind] 旧格式索引也读不了：%s" % e, file=sys.stderr)
            return False

    def _load_jsonl(self, path: str) -> bool:
        """兼容早期的 JSONL 索引格式。"""
        entries = []
        by_key = {}
        meta = None
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                if meta is None:
                    obj = json.loads(line)
                    meta = obj.get("_meta")
                    if not meta or meta.get("version") != 1:
                        return False
                    continue
                path_s, is_dir, size, mtime, depth, noise, hidden = json.loads(line)
                name = path_s.rsplit("\\", 1)[-1] or path_s
                e = Entry(path=path_s, name=name, low=name.lower(), is_dir=bool(is_dir),
                          size=size, mtime_ms=mtime, depth=depth,
                          noise=bool(noise), hidden=bool(hidden))
                entries.append(e)
                by_key[norm_key(path_s)] = e
        with self.lock:
            self.entries = entries
            self.by_key = by_key
            self.built_at = meta.get("built_at", 0)
            st = meta.get("stats") or {}
            s = ScanStats()
            s.files = st.get("files", 0)
            s.dirs = st.get("dirs", 0)
            s.bytes = int(st.get("gb", 0) * 1e9)
            s.seconds = st.get("seconds", 0)
            self.stats = s
            self.pinyin_ready = False
            self.pinyin = PinyinIndex()
        return True

    # -- 增量刷新 ---------------------------------------------------------
    def refresh(self, on_progress=None) -> dict:
        """重新扫一遍卷，比对内存索引，只改动变化的条目。

        这里不做"监听增量"，而是周期性全量比对 —— 因为 Windows 的
        ReadDirectoryChangesW 有溢出丢事件的老问题，而全盘扫在你机器上只要 15 秒。
        """
        t0 = time.perf_counter()
        fresh = IndexEngine(self.settings)
        fresh.build(self.settings["roots"], on_progress=on_progress)
        self.ensure_by_key()
        added = updated = removed = 0
        with self.lock:
            old = self.by_key
            new = fresh.by_key
            for k, ne in new.items():
                oe = old.get(k)
                if oe is None:
                    added += 1
                elif oe.mtime_ms != ne.mtime_ms or oe.size != ne.size:
                    updated += 1
            for k in old.keys() - new.keys():
                removed += 1
            self.entries = fresh.entries
            self.by_key = new
            self.stats = fresh.stats
            self.built_at = now_ms()
            self.pinyin_ready = False
            self.pinyin = PinyinIndex()
        return {"added": added, "updated": updated, "removed": removed,
                "seconds": round(time.perf_counter() - t0, 1),
                "total": len(self.entries)}

    def is_empty(self) -> bool:
        return not self.entries

    def age_seconds(self) -> float:
        if not self.built_at:
            return float("inf")
        return now_ms() / 1000.0 - self.built_at / 1000.0


# ---------------------------------------------------------------------------
# DSH 会话索引（SQLite + FTS5 trigram）
# ---------------------------------------------------------------------------
# 只读 ~/.dsh，绝不写入 DSH 自己的任何派生库。
# 全文索引用 trigram 分词器：unicode61 匹配不了中文子串（"文档" 匹配不到 "查文档记录"），
# 而 trigram 天然支持任意位置子串，代价是索引大一点。

BRIDGE_JS = os.path.join(APP_DIR, "bridge.js")
ZSTD_SUFFIXES = (".jsonl.zstd",)

DB_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  sid TEXT PRIMARY KEY,
  session_dir TEXT,
  proj_dir TEXT,
  cwd TEXT,
  title TEXT,
  created_ms INTEGER DEFAULT 0,
  last_ms INTEGER DEFAULT 0,
  events INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS session_files (
  path TEXT PRIMARY KEY,
  sid TEXT,
  size INTEGER DEFAULT 0,
  mtime_ms INTEGER DEFAULT 0,
  indexed_bytes INTEGER DEFAULT 0,
  indexed_rows INTEGER DEFAULT 0,
  updated_ms INTEGER DEFAULT 0,
  error TEXT
);
-- 说明：「对话」模式按需求只搜**会话标题**，不搜正文。
-- 所以库里只存会话元数据（标题/cwd/时间 + 日志路径），不再存对话正文，
-- 也不再建正文全文索引。正文只在预览某一条会话时按需解压一次。
"""


def db_connect(path: str = DB_PATH) -> sqlite3.Connection:
    ensure_dirs()
    con = sqlite3.connect(path, timeout=15, check_same_thread=False)
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(DB_SCHEMA)
    # 旧版存过全部对话正文 + 正文全文索引（events / events_fts），能占几百 MB。
    # 现在只搜标题了，这两张表没有用处，一次性清掉并回收空间。
    try:
        stale = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('events_fts', 'events')")]
        if stale:
            for name in stale:
                con.execute("DROP TABLE %s" % name)
            con.commit()
            # WAL 模式下光 VACUUM 不会立刻缩小文件，先 checkpoint 把 WAL 并回去
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            con.execute("VACUUM")
            print("[dshfind] 已移除对话正文表与正文索引（现在只搜会话标题）并回收空间",
                  file=sys.stderr)
    except sqlite3.Error:
        pass
    return con


def session_id_from_path(p: str) -> str:
    d = os.path.basename(os.path.dirname(p))
    return d


def find_session_logs(roots) -> list:
    """找出所有会话日志文件（每个会话取版本号最高的那个 generation）。"""
    found = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            for fn in filenames:
                if not fn.endswith(tuple(ZSTD_SUFFIXES)):
                    continue
                # session.v3.jsonl.zstd / session.v2.jsonl.zstd -> 版本号
                m = re.match(r"session(?:\.v(\d+))?\.jsonl\.zstd$", fn)
                if not m:
                    continue
                ver = int(m.group(1) or 0)
                sid = session_id_from_path(os.path.join(dirpath, fn))
                old = found.get(sid)
                if old is None or ver > old[0]:
                    found[sid] = (ver, os.path.join(dirpath, fn))
    return [(sid, p) for sid, (ver, p) in found.items()]


def load_projcache_meta() -> dict:
    """从 DSH 的投影缓存里读出标题/cwd/创建时间 —— 比解压日志便宜得多。"""
    out = {}
    if not os.path.isdir(DSH_PROJCACHE):
        return out
    for fn in os.listdir(DSH_PROJCACHE):
        if not fn.endswith(".json"):
            continue
        sid = fn[:-5]
        try:
            with open(os.path.join(DSH_PROJCACHE, fn), "r", encoding="utf-8") as f:
                rec = json.load(f).get("record") or {}
        except Exception:
            continue
        ident = rec.get("identity") or {}
        rows = rec.get("rows") or {}
        title = ((rows.get("title") or {}).get("val") or "")
        stats = (rows.get("sessionStats") or {}).get("val") or {}
        listmeta = (rows.get("sessionListMetadata") or {}).get("val") or {}
        out[sid] = {
            "cwd": ident.get("cwd") or "",
            "created_ms": ident.get("createdAt") or 0,
            "title": title if isinstance(title, str) else "",
            "last_ms": (listmeta.get("lastPromptAt") or 0) if isinstance(listmeta, dict) else 0,
            "turns": (stats.get("turns") or 0) if isinstance(stats, dict) else 0,
        }
    return out


def _run_bridge(log_path: str, offset: int = 0, timeout: int = 300):
    """调 Node 解压会话日志到临时文件，返回 (临时文件路径, 桥接统计)。

    刻意走"写文件再读"而不是捕获 stdout：这样不依赖管道，沙箱下更稳。
    """
    if not os.path.exists(BRIDGE_JS):
        raise RuntimeError("找不到 bridge.js：%s" % BRIDGE_JS)
    node = shutil_which("node")
    if not node:
        raise RuntimeError("找不到 node，无法解压 DSH 会话日志")
    fd, tmp = tempfile.mkstemp(prefix="dshfind-bridge-", suffix=".jsonl")
    os.close(fd)
    try:
        proc = subprocess.run(
            [node, BRIDGE_JS, "--in", log_path, "--out", tmp, "--offset", str(offset)],
            capture_output=True, timeout=timeout)
        stats = {}
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        if out:
            try:
                stats = json.loads(out.splitlines()[-1])
            except Exception:
                stats = {}
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError("bridge.js 失败(%d)：%s" % (proc.returncode, err[:400]))
        return tmp, stats
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def shutil_which(name: str):
    for p in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(p, name + ".exe")
        if os.path.isfile(cand):
            return cand
        cand = os.path.join(p, name)
        if os.path.isfile(cand):
            return cand
    return None


def index_sessions(con: sqlite3.Connection, roots=None, rebuild: bool = False,
                   on_progress=None) -> dict:
    """索引会话**标题**（以及日志文件路径/时间）。

    按需求，「对话」模式只搜标题、不搜正文，所以这里完全不解压日志：
    标题从 DSH 的投影缓存里读，路径从目录里扫，剩下的只是 stat。
    103 个会话的刷新因此是**毫秒级**（以前解压全部正文要 3 秒、首次 74 秒）。
    正文只在你要预览某一条会话时才按需解压一次（见 session_context）。
    """
    roots = roots or [DSH_SESSIONS]
    t0 = time.perf_counter()
    meta = load_projcache_meta()
    logs = find_session_logs(roots)

    known = {r[0]: (r[1], r[2])
             for r in con.execute("SELECT path, size, mtime_ms FROM session_files")}

    updated = 0
    skipped = 0
    added = 0
    for i, (sid, path) in enumerate(logs):
        try:
            st = os.stat(path)
        except OSError:
            continue
        size = st.st_size
        mtime_ms = int(st.st_mtime * 1000)
        if not rebuild and known.get(path) == (size, mtime_ms):
            skipped += 1
            continue
        pm = meta.get(sid, {})
        con.execute(
            "INSERT OR REPLACE INTO sessions"
            "(sid, session_dir, proj_dir, cwd, title, created_ms, last_ms, events) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (sid,
             os.path.dirname(os.path.dirname(path)),
             os.path.basename(os.path.dirname(os.path.dirname(path))),
             pm.get("cwd") or "",
             pm.get("title") or "",
             pm.get("created_ms") or 0,
             pm.get("last_ms") or mtime_ms,
             0))
        con.execute(
            "INSERT OR REPLACE INTO session_files"
            "(path, sid, size, mtime_ms, indexed_bytes, indexed_rows, updated_ms, error) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (path, sid, size, mtime_ms, 0, 0, now_ms(), None))
        updated += 1
        added += 1
        if on_progress and i % 20 == 0:
            on_progress(i, len(logs), sid)

    # 会话日志被删掉的，把对应记录也清掉
    alive = {p for _, p in logs}
    for path in list(known):
        if path not in alive:
            con.execute("DELETE FROM session_files WHERE path=?", (path,))
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('sessions_indexed_at',?)",
                (str(now_ms()),))
    con.commit()
    return {
        "sessions": len(logs), "updated": updated, "unchanged": skipped,
        "rows": added, "errors": [],
        "seconds": round(time.perf_counter() - t0, 2),
    }


# ---------------------------------------------------------------------------
# 会话检索：只搜标题
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SessionHit:
    sid: str
    title: str
    snippet: str
    cwd: str
    proj_dir: str
    time_ms: int
    events: int
    score: int
    log_path: str = ""


def _title_snippet(title: str, needle: str) -> str:
    """把命中处用 [ ] 标出来，前端会渲染成高亮。"""
    p = title.lower().find(needle.lower())
    if p < 0:
        return title
    return title[:p] + "[" + title[p:p + len(needle)] + "]" + title[p + len(needle):]


def search_sessions(con: sqlite3.Connection, text: str, limit: int = 200,
                    role_filter: str = "") -> list:
    """按**标题**搜会话。不碰正文。

    role_filter 参数保留只为接口兼容，标题检索下没有意义，会被忽略。
    """
    t0 = time.perf_counter()
    text = text.strip()
    if not text:
        return [], 0.0
    like = "%" + text.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    prefix = text.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    sql = (
        "SELECT s.sid, s.title, s.cwd, s.proj_dir, "
        "       COALESCE(NULLIF(s.last_ms,0), s.created_ms) AS t, "
        "       COALESCE(f.path, '') "
        "FROM sessions s LEFT JOIN session_files f ON f.sid = s.sid "
        "WHERE s.title LIKE ? ESCAPE '\\' "
        "ORDER BY "
        "  CASE WHEN lower(s.title) = lower(?) THEN 0 "
        "       WHEN s.title LIKE ? ESCAPE '\\' THEN 1 "
        "       ELSE 2 END, "
        "  t DESC "
        "LIMIT ?"
    )
    rows = con.execute(sql, (like, text, prefix, limit)).fetchall()
    hits = []
    for sid, title, cwd, proj, t, log_path in rows:
        title = title or ""
        needle = text.lower()
        if title.lower() == needle:
            score = 300
        elif title.lower().startswith(needle):
            score = 200
        else:
            score = 100
        hits.append(SessionHit(
            sid=sid, title=title, snippet=_title_snippet(title, text),
            cwd=cwd or "", proj_dir=proj or "", time_ms=int(t or 0),
            events=0, score=score, log_path=log_path))
    dt = (time.perf_counter() - t0) * 1000
    return hits, dt


def session_stats(con: sqlite3.Connection) -> dict:
    try:
        n_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        n_files = con.execute("SELECT COUNT(*) FROM session_files").fetchone()[0]
        errs = con.execute(
            "SELECT COUNT(*) FROM session_files WHERE error IS NOT NULL").fetchone()[0]
        with_title = con.execute(
            "SELECT COUNT(*) FROM sessions WHERE title IS NOT NULL AND title != ''"
        ).fetchone()[0]
        indexed_at = con.execute(
            "SELECT v FROM meta WHERE k='sessions_indexed_at'").fetchone()
        return {"sessions": n_sessions, "titled": with_title, "files": n_files,
                "errors": errs, "indexed_at": int(indexed_at[0]) if indexed_at else 0}
    except sqlite3.Error:
        return {"sessions": 0, "titled": 0, "files": 0, "errors": 0, "indexed_at": 0}


# 预览用：按会话 id 解压它的日志，取出前后若干事件。结果做个小缓存，
# 因为翻上翻下时同一会话会被反复请求。
_SESSION_CTX_CACHE: dict = {}
_SESSION_CTX_MAX = 8


def session_context(log_path: str, limit: int = 40) -> dict:
    """按需解压一个会话日志，返回开头若干事件用于预览。

    这里才真正用到 bridge.js —— 只在你要看某条会话时解一次，
    不再在索引阶段把全部正文都解压一遍。
    """
    if not log_path or not os.path.exists(log_path):
        return {"ok": False, "error": "会话日志不存在", "events": []}
    hit = _SESSION_CTX_CACHE.get(log_path)
    if hit is None:
        try:
            tmp, bstats = _run_bridge(log_path, 0)
        except Exception as e:
            return {"ok": False, "error": str(e), "events": []}
        rows = []
        try:
            total = 0
            with open(tmp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    total += 1
                    if len(rows) < limit:
                        rows.append({
                            "seq": int(r.get("seq") or 0),
                            "time_ms": int(r.get("time_ms") or 0),
                            "role": r.get("role") or "",
                            "tool": r.get("tool") or "",
                            "text": (r.get("text") or "")[:2000],
                        })
            hit = {"ok": True, "events": rows, "total": total,
                   "plaintext_bytes": bstats.get("plaintext_bytes", 0)}
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if len(_SESSION_CTX_CACHE) >= _SESSION_CTX_MAX:
            _SESSION_CTX_CACHE.clear()
        _SESSION_CTX_CACHE[log_path] = hit
    return hit


# ---------------------------------------------------------------------------
# 全局热键
# ---------------------------------------------------------------------------
# oil-find 在 macOS 上用 Carbon 的 RegisterEventHotKey；Windows 上的直接对应物
# 就是 RegisterHotKey —— 系统级"吃掉"这个组合键，不需要低级键盘钩子、不需要额外权限。
# 消息泵用 PeekMessageW 每 50ms 清一次队列，不能用 GetMessage（那会卡住 Tk 事件循环）。

class GlobalHotkey:
    def __init__(self, callback):
        self.callback = callback
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                               wintypes.UINT, wintypes.UINT]
        self.user32.RegisterHotKey.restype = wintypes.BOOL
        self.user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.UnregisterHotKey.restype = wintypes.BOOL
        self.msg = wintypes.MSG()
        self.id = 0xB001
        self.registered = False
        self.error = ""

    # 命名键 -> 虚拟键码。以前只支持单字符和 F1..F12，
    # 配 shift+ctrl+space 这种会直接报"格式看不懂"。
    NAMED_KEYS = {
        "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
        "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "insert": 0x2D,
        "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21,
        "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
        "`": 0xC0, "-": 0xBD, "=": 0xBB, ",": 0xBC, ".": 0xBE, "/": 0xBF,
        ";": 0xBA, "'": 0xDE, "[": 0xDB, "\\": 0xDC, "]": 0xDD,
    }

    def parse(self, spec: str):
        """把 "shift+ctrl+f" 解析成 (mods, vk)。失败返回 (0, None)。"""
        mods = 0
        key = None
        for part in spec.lower().replace(" ", "").split("+"):
            if part == "shift":
                mods |= HOTKEY_MOD_SHIFT
            elif part == "ctrl" or part == "control":
                mods |= HOTKEY_MOD_CONTROL
            elif part == "alt":
                mods |= HOTKEY_MOD_ALT
            elif part == "win":
                mods |= HOTKEY_MOD_WIN
            elif part in self.NAMED_KEYS:
                key = part
            elif len(part) == 1:
                key = part.upper()
            elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
                key = part
        if key is None:
            return 0, None
        if key in self.NAMED_KEYS:
            vk = self.NAMED_KEYS[key]
        elif len(key) == 1:
            vk = ord(key)
        else:
            vk = 0x70 + int(key[1:]) - 1
        return mods, vk

    def register(self, spec: str = "shift+ctrl+f") -> bool:
        mods, vk = self.parse(spec)
        if vk is None:
            self.error = "快捷键格式看不懂：%s" % spec
            return False
        ok = self.user32.RegisterHotKey(None, self.id, mods | HOTKEY_MOD_NOREPEAT, vk)
        if not ok:
            err = ctypes.get_last_error()
            self.error = "注册 %s 失败（错误码 %d，可能已被别的程序占用）" % (spec, err)
            return False
        self.registered = True
        self.mods, self.vk = mods, vk
        self.error = ""
        return True

    # 合成按键用的虚拟键码
    _SYNTH_MOD_VKS = [(HOTKEY_MOD_SHIFT, 0x10), (HOTKEY_MOD_CONTROL, 0x11),
                      (HOTKEY_MOD_ALT, 0x12), (HOTKEY_MOD_WIN, 0x5B)]

    def synth_press(self, spec: str = None):
        """真的把某个组合键"按"一次（keybd_event 合成）。

        只给自检/自测用：RegisterHotKey 注册成功 ≠ 按键会触发，
        所以要用它验证"消息真的会到"。合成输入和真人按键在系统看来是一样的。
        """
        if spec:
            mods, vk = self.parse(spec)
        else:
            mods, vk = getattr(self, "mods", 0), getattr(self, "vk", None)
        if vk is None:
            return False
        u = self.user32
        u.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD,
                                  ctypes.c_size_t]
        keys = [k for flag, k in self._SYNTH_MOD_VKS if mods & flag]
        for k in keys:
            u.keybd_event(k, 0, 0, 0)
        u.keybd_event(vk, 0, 0, 0)
        time.sleep(0.03)
        u.keybd_event(vk, 0, 0x0002, 0)          # KEYEVENTF_KEYUP
        for k in reversed(keys):
            u.keybd_event(k, 0, 0x0002, 0)
        return True

    def poll(self) -> int:
        """清空消息队列，返回收到的 WM_HOTKEY 次数（0 或 1）。"""
        if not self.registered:
            return 0
        fired = 0
        while self.user32.PeekMessageW(ctypes.byref(self.msg), None, 0, 0, 1):
            if self.msg.message == HOTKEY_MSG_ID and fired == 0:
                fired = 1
        return fired

    def unregister(self):
        if self.registered:
            self.user32.UnregisterHotKey(None, self.id)
            self.registered = False


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def require_windows():
    if os.name != "nt":
        raise SystemExit("dshfind 目前只支持 Windows（依赖 RegisterHotKey、"
                         "st_file_attributes 与资源管理器定位）。")


def cmd_reindex(args):
    require_windows()
    settings = load_settings()
    eng = IndexEngine(settings)
    print("开始扫描：%s" % ", ".join(settings["roots"]))
    t0 = time.perf_counter()

    def prog(root, files, dirs, elapsed):
        sys.stdout.write("\r  %-16s %9s 文件 / %8s 目录  %.0fs"
                         % (root, "{:,}".format(files), "{:,}".format(dirs), elapsed))
        sys.stdout.flush()

    st = eng.build(settings["roots"], on_progress=prog)
    print("\n扫描完成：%s 文件 / %s 目录，%.1f 秒"
          % ("{:,}".format(st.files), "{:,}".format(st.dirs), st.seconds))
    if st.truncated:
        print("注意，以下卷未扫完：")
        for t in st.truncated:
            print("  -", t)
    p = eng.save()
    print("索引已保存：%s（%.1f MB）" % (p, os.path.getsize(p) / 1e6))
    # 全量重建之后，之前监听积累的增量就不需要了（新索引里已经包含它们）
    try:
        if os.path.exists(DELTA_PATH):
            os.unlink(DELTA_PATH)
            print("已清除增量缓存（%s）" % os.path.basename(DELTA_PATH))
    except OSError:
        pass
    print("\n正在建排序索引（按大小/时间，约 15 秒）…")
    t0 = time.perf_counter()
    eng.build_sorted()
    D_sorted = eng.sorted.save()
    sorted_save_stamp(eng)
    print("  完成，用时 %.1f 秒 → %s（%.1f MB）"
          % (time.perf_counter() - t0, D_sorted, os.path.getsize(D_sorted) / 1e6))
    fts_path = build_fts_from(eng)
    if fts_path:
        print("名字索引已保存：%s（%.1f MB）" % (fts_path, os.path.getsize(fts_path) / 1e6))
    return 0


def build_fts_from(eng) -> str | None:
    """给已有索引补建 FTS5 名字索引（供 --reindex / --fts 调用）。"""
    print("\n正在建名字索引（trigram，约 1～2 分钟）…")
    t0 = time.perf_counter()

    def prog(i, total):
        if i % 200000 == 0:
            sys.stdout.write("\r  %s / %s" % ("{:,}".format(i), "{:,}".format(total)))
            sys.stdout.flush()

    try:
        p = fts_build(eng, on_progress=prog)
    except Exception as e:
        print("\n名字索引建失败：%s" % e, file=sys.stderr)
        return None
    print("\r  完成，用时 %.1f 秒" % (time.perf_counter() - t0))
    # 重置连接，让新库生效
    global _fts_con
    _fts_con = None
    return p


def cmd_sessions(args):
    require_windows()
    settings = load_settings()
    con = db_connect()
    roots = settings.get("session_roots", [DSH_SESSIONS])
    print("开始索引 DSH 会话：%s" % ", ".join(roots))
    t0 = time.perf_counter()

    def prog(i, n, name):
        if i % 5 == 0 or i == n - 1:
            sys.stdout.write("\r  %d/%d %-30s" % (i + 1, n, name[:30]))
            sys.stdout.flush()

    res = index_sessions(con, roots, rebuild=("--rebuild" in args), on_progress=prog)
    print("\n完成：会话 %d 个，更新 %d，跳过 %d，新增 %s 条事件，%.1f 秒"
          % (res["sessions"], res["updated"], res["unchanged"],
             "{:,}".format(res["rows"]), res["seconds"]))
    if res["errors"]:
        print("有 %d 个会话出错：" % len(res["errors"]))
        for e in res["errors"][:10]:
            print("  -", e)
    print("库统计：", session_stats(con))
    return 0


def cmd_text(args):
    require_windows()
    text = " ".join(args).strip()
    if not text:
        print('用法：python dshfind.py --text "查询内容"')
        return 2
    settings = load_settings()
    eng = IndexEngine(settings)
    if not eng.load():
        print("没有索引文件，先跑 python dshfind.py --reindex")
        return 1
    print("索引 %s 条，%.1f MB，构建于 %s"
          % ("{:,}".format(len(eng.entries)),
             os.path.getsize(INDEX_PATH) / 1e6, fmt_time(eng.built_at)))
    q = parse_query(text)
    if q.diagnostics:
        print("提示：" + "；".join(q.diagnostics))
    hits, dt, cand = run_query(eng, q, limit=40)
    print("\n查询 %r → %d 个结果 / 候选 %d 条 · %.1f ms\n" % (text, len(hits), cand, dt))
    for h in hits:
        e = h.entry
        print("%7d  %-10s  %s" % (h.score, fmt_size(e.size) if not e.is_dir else "<DIR>",
                                  actual_path(e.path)))
        if h.span:
            print("          └ 命中 %r（%s）" % (span_text(e, h.span), h.span[2]))
    con = db_connect()
    shits, sdt = search_sessions(con, text, limit=10)
    if shits:
        print("\n会话命中 %d 条 · %.1f ms\n" % (len(shits), sdt))
        for h in shits[:8]:
            print("  [%s#%d] %s" % (h.role, h.seq, (h.title or "")[:40]))
            print("      %s" % (h.snippet or "").replace("\n", " ")[:160])
    return 0


def cmd_fts(args):
    """只重建名字 FTS 索引（不重新扫盘）。"""
    require_windows()
    eng = IndexEngine(load_settings())
    if not eng.load():
        print("没有文件索引，先跑 --reindex")
        return 1
    p = build_fts_from(eng)
    return 0 if p else 1


def cmd_gui(args):
    """默认入口：网页界面（server.py）+ Edge 应用模式窗口。

    网页界面能原生处理 DPI 缩放（文字清晰），也才能用上 GSAP 做动效。
    """
    require_windows()
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    import server
    httpd, win, url = server.serve(open_window=True)
    if httpd is None:
        # 已经有一个实例在跑。顺手把实际生效的热键念出来，省得用户猜。
        hk_now = load_settings().get("hotkey", "shift+ctrl+f")
        print("[dshfind] 已经有一个在跑了，按 %s 唤出那个窗口即可（不用再开一个）。"
              % hk_now, flush=True)
        return 1
    print("[dshfind] 界面已启动。关掉窗口后本进程仍在后台，按 %s 可重新唤出。"
          % (server.Handler.hotkey or "（没注册上热键，只能手动打开）"), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[dshfind] 退出")
    return 0


def cmd_selftest(args) -> int:
    """内置自检：不启动界面，验证数据层与解析器。"""
    require_windows()
    state = {"ok": True}

    def check(name, cond, detail=""):
        print(("  [OK]   " if cond else "  [FAIL] ") + name
              + (("  " + detail) if detail else ""))
        if not cond:
            state["ok"] = False

    print("== 拼音表 ==")
    check("拼音表已加载", PINYIN.ok, "%d 字" % PINYIN.count)
    ini, full = PINYIN.to_pinyin("文档")
    check("首字母", ini == "wd", repr(ini))
    check("全拼", full == "wendang", repr(full))

    print("== 查询解析 ==")
    q = parse_query("报告 | 总结 ext:md !node_modules")
    check("OR 分支数", len(q.or_groups) == 2, str([len(b) for b in q.and_groups]))
    check("ext 解析", any(f.kind == "ext" for b in q.and_groups for _, f in b))
    check("否定解析", any(neg for b in q.and_groups for neg, _ in b))
    q_or = parse_query("report | 报告")
    check("单个 | 生成两个 OR 分支", len(q_or.or_groups) == 2,
          repr([len(b[0]) for b in q_or.or_groups]))
    q2 = parse_query("size:>10mb")
    check("size:>10mb", not q2.diagnostics, str(q2.diagnostics))
    q3 = parse_query("dm:today")
    check("dm:today", not q3.diagnostics, str(q3.diagnostics))
    q4 = parse_query("C:\\Users\\test")
    check("盘符路径不当过滤器", any(f.kind == "path" for c in q4.clauses for _, f in c))

    print("== 索引 ==")
    eng = IndexEngine(load_settings())
    loaded = eng.load()
    check("索引文件可加载", loaded,
          ("%s 条" % "{:,}".format(len(eng.entries))) if loaded else "无索引")
    if loaded:
        hits, dt, cand = run_query(eng, parse_query("readme"), limit=5)
        check("能搜到 readme", len(hits) > 0, "%.0f ms" % dt)
        # 索引里路径必须是小写，否则小写关键词会漏掉大写文件名
        sample = eng.entries[::50000]
        check("索引路径为小写", all(e.low == e.low.lower() for e in sample),
              "抽样 %d 条" % len(sample))
        # 噪音标记必须覆盖整棵子树：winsxs / node_modules 下的文件都该被排除。
        # 这一步特别容易错 —— 扫描是"按目录分组"的顺序，不是深度优先，
        # 用栈式继承会整片漏标，所以只能按父目录判定。
        for pat, label in (("\\winsxs\\", "winsxs"), ("\\node_modules\\", "node_modules")):
            deep = [e for e in eng.entries if pat in e.path_low and not e.is_dir]
            if deep:
                marked = sum(1 for e in deep if e.noise)
                check("噪音标记覆盖 %s 整棵子树" % label, marked == len(deep),
                      "%s / %s 个文件" % ("{:,}".format(marked),
                                         "{:,}".format(len(deep))))
        low_hits, _, low_cand = run_query(eng, parse_query("license"), limit=3)
        up_hits, _, up_cand = run_query(eng, parse_query("LICENSE"), limit=3)
        check("大小写不敏感", low_cand == up_cand,
              "license=%s / LICENSE=%s" % ("{:,}".format(low_cand),
                                            "{:,}".format(up_cand)))
        if os.path.exists(FTS_PATH):
            m = fts_meta()
            check("名字索引存在", True,
                  "%s 条 · %.0f MB" % (m.get("count", "?"),
                                      os.path.getsize(FTS_PATH) / 1e6))
            check("名字索引与文件索引同步", not fts_is_stale(eng))
            rowids = fts_search("readme", eng)
            check("名字索引可查询", bool(rowids),
                  "%s 命中" % "{:,}".format(len(rowids or [])))
        else:
            print("  [跳过] 还没有名字索引（可跑 --fts 建一个，英文子串会快 8 倍以上）")
        if os.path.exists(os.path.join(SORT_DIR, "size_mtime.bin")):
            t1 = time.perf_counter()
            eng.build_sorted()
            check("排序索引可用", eng.sorted_ready, "%.1f 秒" % (time.perf_counter() - t1))
            hits2, dt2, cand2 = run_query(eng, parse_query("size:>100mb"), limit=5)
            check("size: 查询走二分", dt2 < 200,
                  "%.1f ms / 候选 %s" % (dt2, "{:,}".format(cand2)))
        else:
            print("  [跳过] 还没有排序索引（可跑 --reindex 生成，size:/dm: 会快上百倍）")

    print("== 全局热键 ==")
    hk = GlobalHotkey(lambda: None)
    if hk.register(load_settings().get("hotkey", "shift+ctrl+f")):
        # 真的按一次这个组合键（合成输入），看消息队列里会不会出现 WM_HOTKEY。
        # 以前的写法是自己 PostThreadMessageW 塞一条假消息，只能证明"消息泵没坏"，
        # 证明不了"注册的热键真能被按到" —— 界面那个 bug 就是这么漏过去的（第 8 项）。
        hk.synth_press()
        got = 0
        t0 = time.time()
        while time.time() - t0 < 1.5:
            if hk.poll():
                got = 1
                break
            time.sleep(0.02)
        hk.unregister()
        check("热键能真的按到", got == 1, "合成按键后收到 WM_HOTKEY")
    else:
        print("  [跳过] 热键注册失败（可能被其他程序/本程序另一实例占用）：%s" % hk.error)

    print("== 会话库 ==")
    con = db_connect()
    st = session_stats(con)
    check("会话库可读", st["sessions"] >= 0, str(st))
    check("会话库只存标题（不存正文）", st.get("titled", 0) > 0,
          "%s / %s 个会话有标题" % (st.get("titled", 0), st.get("sessions", 0)))
    if st.get("titled"):
        row = con.execute(
            "SELECT title FROM sessions WHERE title != '' LIMIT 1").fetchone()
        needle = (row[0] or "")[:2]
        hits, dt = search_sessions(con, needle, limit=5)
        check("标题检索可用", len(hits) > 0,
              "搜 %r → %d 条 · %.1f ms" % (needle, len(hits), dt))
        body_only = "supervisor"      # 正文里很常见、标题里基本没有的词
        bh, _ = search_sessions(con, body_only, limit=50)
        check("正文不参与匹配",
              all(body_only in h.title.lower() for h in bh),
              "搜 %r → %d 条，全部在标题里" % (body_only, len(bh)))

    print("\n结果：" + ("全部通过" if state["ok"] else "有失败项"))
    return 0 if state["ok"] else 1


def cmd_quit(args):
    """让正在跑的那个 dshfind 干净退出（关窗口 + 落盘 + 退进程）。

    做法：读 data/instance.json（启动时写进去的端口和 token），
    给它的 /api/quit 发一个请求。进程是 pythonw 起的、没有控制台，
    所以这是不用任务管理器的退出方式。

    找到不实例信息时（比如那个实例还是旧版本、没写过 instance.json），
    退一步按命令行找 pythonw 进程，走 taskkill 硬杀 —— 会提示这一点，
    因为硬杀会跳过"增量落盘"这一步。
    """
    import json as _json
    import urllib.error
    import urllib.request

    here = os.path.dirname(os.path.abspath(__file__))
    path = os.environ.get("DSHFIND_INSTANCE_FILE") or \
        os.path.join(here, "data", "instance.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            info = _json.load(f)
    except FileNotFoundError:
        print("没有找到 data/instance.json —— 可能是没在跑，或者跑的是更早的版本。")
        return _quit_by_process()
    except Exception as e:
        print("读 %s 失败：%s" % (path, e))
        return 1

    port, token, pid = info.get("port"), info.get("token"), info.get("pid")
    if not port or not token:
        print("实例信息不完整：%s" % info)
        return 1
    url = "http://127.0.0.1:%d/api/quit?t=%s" % (port, token)
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"X-Dsh-Token": token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
    except urllib.error.URLError as e:
        print("连不上正在运行的实例（PID %s，端口 %s）：%s" % (pid, port, e.reason))
        return _quit_by_process()
    except Exception as e:
        # 进程退出得太快时，请求会断在半路 —— 这其实说明它退出成功了
        print("请求已发出，进程可能已经退出（%s）" % e)

    for _ in range(30):
        time.sleep(0.1)
        if not _pid_alive(pid):
            print("已退出（PID %s）。热键和后台监听都停了。" % pid)
            return 0
    print("已发出退出请求，但 PID %s 还在，改试硬杀。" % pid)
    return _quit_by_process()


def _read_instance():
    """读 data/instance.json（正在跑的那个实例的端口/token/pid）。没有就返回 {}。"""
    import json as _json

    here = os.path.dirname(os.path.abspath(__file__))
    path = os.environ.get("DSHFIND_INSTANCE_FILE") or \
        os.path.join(here, "data", "instance.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}


def _ask_focus(info: dict) -> bool:
    """让正在跑的实例把窗口提到前台（POST /api/focus）。"""
    import urllib.error
    import urllib.request

    port, token = info.get("port"), info.get("token")
    if not port or not token:
        return False
    url = "http://127.0.0.1:%d/api/focus?t=%s" % (port, token)
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"X-Dsh-Token": token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            import json as _json
            return bool(_json.loads(r.read()).get("ok"))
    except Exception:
        return False


def focus_window_by_title(title_hint: str = None) -> bool:
    """按窗口标题把应用的窗口提到前台（给旧版本实例的兜底，也方便手动调试）。

    为什么不用 pid：Edge 应用窗口属于 msedge 的子进程，和我们自己的 python 进程
    不是同一个 pid，隔一层就找不准了；而窗口标题是我们自己设的，反而可靠。
    """
    try:
        import ctypes
        import ctypes.wintypes as wt
    except Exception:
        return False
    u = ctypes.WinDLL("user32", use_last_error=True)
    hint = (title_hint or "").strip().lower()
    hits = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lp):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value.strip()
            low = title.lower()
            if (hint and low == hint) or low.startswith("dshfind"):
                hits.append(hwnd)
        return True

    u.EnumWindows(cb, 0)
    if not hits:
        return False
    hwnd = hits[0]
    SW_RESTORE = 9
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, SW_RESTORE)

    # Windows 的"前台锁"：不是当前前台进程时，SetForegroundWindow 往往只闪一下。
    # 和 server.AppWindow._bring 用同一个办法：临时把输入队列接到前台线程上。
    k = ctypes.WinDLL("kernel32")
    u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    fg = u.GetForegroundWindow()
    tid_cur = k.GetCurrentThreadId()
    tid_fg = 0
    if fg:
        pid_buf = wt.DWORD()
        tid_fg = u.GetWindowThreadProcessId(fg, ctypes.byref(pid_buf))
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
    # 极短暂置顶再取消，避免被别的置顶窗口压住
    SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
    u.SetWindowPos(hwnd, -1, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)
    u.SetWindowPos(hwnd, -2, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)
    return True


def cmd_show(args):
    """桌面快捷方式用的入口：已经在跑就把它叫到前台，没跑就正常启动界面。

    这样桌面上那个图标点一下永远"有反应"：
      没在跑 → 开窗口（正常启动）
      在跑   → 把已有窗口提到前台（等同按热键），不会再开第二份、也不会占 2.7 GB 内存
    """
    info = _read_instance()
    pid = info.get("pid")
    if info and _pid_alive(pid):
        if _ask_focus(info):
            print("[dshfind] 已经在跑，已把窗口叫到前台。")
            return 0
        # 旧版本的实例没有 /api/focus：按窗口标题兜底
        title = (load_settings().get("app_title") or "").strip()
        if focus_window_by_title(title or None):
            print("[dshfind] 已经在跑，已把窗口叫到前台（按标题找的）。")
            return 0
        print("[dshfind] 检测到已经在跑（PID %s），但没能把窗口叫到前台。"
              "可以按热键试试。" % pid)
        return 0
    return cmd_gui(args)


def _quit_by_process(match: str = None) -> int:
    """按命令行找 dshfind 的 pythonw 进程并结束它（办法比较硬，但找不着别的路时管用）。

    match 默认匹配命令行里的 "dshfind.py"；测试可以用环境变量
    DSHFIND_QUIT_MATCH 换成别的特征串，免得误杀正在用的实例。
    """
    import subprocess as _sp

    match = (match or os.environ.get("DSHFIND_QUIT_MATCH") or "dshfind.py").lower()
    try:
        out = _sp.run(["wmic", "process", "where", "name like 'pythonw%'",
                       "get", "ProcessId,CommandLine"],
                      capture_output=True, text=True, timeout=15).stdout
    except Exception as e:
        print("查进程失败：%s" % e)
        return 1
    pids = []
    for line in out.splitlines():
        if match in line.lower():
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.append(int(parts[-1]))
    if not pids:
        print("没有找到正在运行的 dshfind 进程（可能已经关掉了）。")
        return 0
    print("找到 %d 个 dshfind 进程：%s —— 硬杀（会跳过增量落盘）" % (len(pids), pids))
    for pid in pids:
        _sp.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True)
    time.sleep(0.6)
    alive = [p for p in pids if _pid_alive(p)]
    if alive:
        print("还有没杀掉的：%s，请用任务管理器结束它们。" % alive)
        return 1
    print("已全部结束。下次启动会重新建索引增量。")
    return 0


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        # 句柄是 64 位的，restype 必须声明，否则会被当成 int 截断
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                           ctypes.POINTER(ctypes.c_ulong)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x1000, 0, int(pid))     # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259        # STILL_ACTIVE
    except Exception:
        return True


def main(argv):
    args = argv[1:]
    if not args:
        return cmd_gui(args)
    flag, rest = args[0], args[1:]
    if flag in ("-h", "--help"):
        print(__doc__)
        return 0
    if flag == "--quit":
        return cmd_quit(rest)
    if flag == "--show":
        return cmd_show(rest)
    if flag == "--reindex":
        return cmd_reindex(rest)
    if flag == "--sessions":
        return cmd_sessions(rest)
    if flag == "--fts":
        return cmd_fts(rest)
    if flag == "--text":
        return cmd_text(rest)
    if flag == "--gui":
        return cmd_gui(rest)
    if flag == "--selftest":
        return cmd_selftest(rest)
    # 不带参数前缀时，当作一次性的文本查询
    return cmd_text(args)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)



