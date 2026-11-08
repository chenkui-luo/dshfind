# -*- coding: utf-8 -*-
"""体检：F:\anime 收了多少条、缓存目录是否被跳过、超超超 是否可检索。"""
import sys

sys.path.insert(0, r"D:\agent相关对话\文件和对话检索")
import dshfind as D

eng = D.IndexEngine()
eng.load()
print("条目:", len(eng.entries), "| files/dirs:",
      eng.stats.files, eng.stats.dirs, "| truncated:", eng.stats.truncated)

prefix = "f:\\anime"
anime = [e for e in eng.entries if e.path_low.startswith(prefix)]
print("F:\\anime 开头的条目:", len(anime))
print("其中直接子项（深度 2）:", sum(1 for e in anime
                                    if e.path_low.count("\\") == 3))

# 缓存目录：目录本身应该还在（能搜到），但里面不该有东西
for name in ("library", "packagecache"):
    dirs = [e for e in eng.entries if e.path_low.endswith("\\" + name)]
    inside = [e for e in eng.entries if ("\\" + name + "\\") in e.path_low]
    print("%-12s 目录本身 %5d 个，其下条目 %d 条" % (name, len(dirs), len(inside)))
    if dirs[:3]:
        print("    例如:", dirs[0].path)

hits, dt, cand = D.run_query(eng, D.parse_query("超超超"), limit=10)
print("超超超: 候选 %d 返回 %d，%.0f ms" % (cand, len(hits), dt))
for h in hits[:4]:
    print("   ", D.actual_path(h.entry.path))
