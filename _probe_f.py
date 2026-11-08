# -*- coding: utf-8 -*-
"""查索引里 F:\\anime 这一片到底收了多少（用来确认"被扫描预算截断"的猜测）。"""
import sys
from collections import Counter

sys.path.insert(0, r"D:\agent相关对话\文件和对话检索")
import dshfind as D

eng = D.IndexEngine()
ok = eng.load()
print("load() ->", ok, "条目:", len(eng.entries), "built_at:", eng.built_at, flush=True)
st = eng.stats
print("stats:", {k: getattr(st, k, None) for k in ("files", "dirs", "skipped", "gb",
                                                   "seconds", "truncated", "errors")},
      flush=True)

roots = Counter(e.path[:2].upper() for e in eng.entries)
print("按盘统计:", dict(roots), flush=True)

anime = [e for e in eng.entries if e.path_low.startswith("f:\\anime")]
print("F:\\anime 开头的条目:", len(anime), flush=True)
direct = [e for e in anime if e.path_low.count("\\") == 3]
print("直接子项:", len(direct), flush=True)
for e in direct[:15]:
    print("   ", e.path, "dir" if e.is_dir else "file", flush=True)

# 索引里 F 盘最后一条是什么（看截断点在哪）
f_entries = [e for e in eng.entries if e.path_low.startswith("f:\\")]
if f_entries:
    print("F 盘条目数:", len(f_entries), flush=True)
    print("F 盘最后一条:", f_entries[-1].path, flush=True)
    print("F 盘最后 5 条:", [e.path for e in f_entries[-5:]], flush=True)

# 有没有"超超超"
hits = [e.path for e in eng.entries if "超超超" in e.path_low]
print("名字/路径含「超超超」的条目:", len(hits), hits[:5], flush=True)
