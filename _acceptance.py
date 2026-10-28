"""交付前的汇总验收：逐条核对目标里的六项要求。"""
import sys, os, time
sys.path.insert(0, r"D:\agent相关对话\文件和对话检索")
import dshfind as D

rows = []


def item(name, ok, detail):
    rows.append((name, ok, detail))
    print(("[通过] " if ok else "[失败] ") + name + "  ——  " + detail)


cfg = D.load_settings()

# 1) 全盘索引
eng = D.IndexEngine(cfg)
t0 = time.perf_counter()
loaded = eng.load()
load_s = time.perf_counter() - t0
eng.build_pinyin()
files = sum(1 for e in eng.entries if not e.is_dir)
dirs = len(eng.entries) - files
item("1. C+D+E+F 全盘文件名索引", loaded and len(eng.entries) > 2_000_000,
     "根目录 %s；%s 文件 + %s 目录 = %s 条目；加载 %.1fs"
     % (", ".join(x.rstrip("\\") for x in cfg["roots"]),
        "{:,}".format(files), "{:,}".format(dirs), "{:,}".format(len(eng.entries)), load_s))

# 2) 拼音 / 首字母
D._QUERY_CACHE.clear()
h1, dt1, c1 = D.run_query(eng, D.parse_query("wd"), limit=5)
h2, dt2, c2 = D.run_query(eng, D.parse_query("wendang"), limit=5)
h3, dt3, c3 = D.run_query(eng, D.parse_query("文档"), limit=5)
ok2 = bool(h1) and bool(h2) and bool(h3)
item("2. 拼音首字母 / 全拼 / 中文检索", ok2,
     "wd→%d条(%.0fms) wendang→%d条(%.0fms) 文档→%d条(%.0fms) 例：%s"
     % (len(h1), dt1, len(h2), dt2, len(h3), dt3,
        os.path.basename(h1[0].entry.path) if h1 else "-"))

# 3) 全局热键
import ctypes
hk = D.GlobalHotkey(lambda: None)
reg = hk.register(cfg.get("hotkey", "shift+ctrl+f"))
got = 0
if reg:
    tid = ctypes.windll.kernel32.GetCurrentThreadId()
    ctypes.windll.user32.PostThreadMessageW(tid, D.HOTKEY_MSG_ID, hk.id, 0)
    got = hk.poll()
    hk.unregister()
item("3. Shift+Ctrl+F 全局热键", reg and got == 1,
     "注册成功且收到 WM_HOTKEY（快捷键 %s）" % cfg.get("hotkey"))

# 4) oil-find 查询语法
syntax = ["ext:md", "kind:image", "size:>100mb", "dm:week", r"regex:^read",
          "path:unity", "file:readme", "folder:unity", "readme !node_modules",
          "README | readme", "*.png"]
res = []
all_ok = True
for s in syntax:
    q = D.parse_query(s)
    D._QUERY_CACHE.clear()
    try:
        h, dt, cand = D.run_query(eng, q, limit=5)
        good = (not q.diagnostics) and (len(h) > 0 or s.startswith("regex:"))
    except Exception as e:
        good = False
        h, dt = [], 0
    all_ok = all_ok and good
    res.append("%s%s" % (s, "" if good else "(失败)"))
item("4. oil-find 风格查询语法", all_ok, "；".join(res))

# 5) DSH 会话全文检索
con = D.db_connect()
st = D.session_stats(con)
row = con.execute("SELECT title FROM sessions WHERE title != '' LIMIT 1").fetchone()
needle = (row[0] or "")[:2] if row else ""
shits, sdt = D.search_sessions(con, needle, limit=5) if needle else ([], 0)
ctx = D.session_context(shits[0].log_path, limit=5) if shits else {}
item("5. DSH 会话标题检索", st["sessions"] > 0 and bool(shits),
     "%d 个会话（%s 个有标题）；搜 %r 命中 %d 条（%.1f ms）"
     % (st["sessions"], st.get("titled", 0), needle, len(shits), sdt))
item("   正文不参与匹配（按需求只搜标题）",
     all(needle.lower() in h.title.lower() for h in shits),
     "命中结果全部来自标题")
item("   正文按需解压（bridge.js 逐帧）", bool(ctx.get("ok")),
     "预览取出 %d 条事件 / 共 %s 条" % (len(ctx.get("events", [])),
                                     ctx.get("total", 0)))

# 6) 文档
here = os.path.dirname(os.path.abspath(__file__))
readme = os.path.join(here, "README.md")
attr = os.path.join(here, "ATTRIBUTION.md")
rl = os.path.getsize(readme) if os.path.exists(readme) else 0
al = os.path.getsize(attr) if os.path.exists(attr) else 0
rtext = open(readme, encoding="utf-8").read() if rl else ""
atext = open(attr, encoding="utf-8").read() if al else ""
has_keys = all(k in rtext for k in ("查询语法", "Shift+Ctrl+F", "会话", "已知限制"))
has_attr = "oil-oil/oil-find" in atext and "pypinyin" in atext and "MIT" in atext
item("6. README 与来源归属", bool(rl and al and has_keys and has_attr),
     "README %.1f KB（含语法/热键/会话/限制）；ATTRIBUTION %.1f KB（oil-find + pypinyin + DSH）"
     % (rl / 1024, al / 1024))

print()
bad = [r for r in rows if not r[1]]
print("=" * 60)
print("汇总：%d / %d 项通过" % (len(rows) - len(bad), len(rows)))
if bad:
    print("未通过：" + ", ".join(b[0] for b in bad))
sys.exit(1 if bad else 0)
