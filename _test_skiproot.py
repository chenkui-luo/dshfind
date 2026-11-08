# -*- coding: utf-8 -*-
"""验证两件事（第 16 项）：

1. 扫描时按 `skip_scan_dirs` 跳过整棵子树（目录本身留下，子项不进去）
2. `rebuild_one_root()` 只重扫一个根，其它根的条目原样保留
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, r"D:\agent相关对话\文件和对话检索")
import dshfind as D

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    if not cond:
        ok_all = False


tmp = tempfile.mkdtemp(prefix="dshskip-")
try:
    A = os.path.join(tmp, "A")
    B = os.path.join(tmp, "B")
    # 一个"Unity 工程"的样子：A\library 里塞一堆文件
    for rel in ("library/x/file1.txt", "library/y/deep/file2.txt",
                "src/file3.txt", "PackageCache/pkg/file4.txt"):
        p = os.path.join(A, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("x")
    os.makedirs(os.path.join(B, "keep"), exist_ok=True)
    with open(os.path.join(B, "keep", "keepme.txt"), "w", encoding="utf-8") as f:
        f.write("y")

    settings = {
        "roots": [A, B],
        "skip_scan_dirs": ["library", "packagecache"],
        "max_entries": 100000,
        "per_root_budget": 600,
        "exclude_noise": True,
    }
    eng = D.IndexEngine(settings)
    st = eng.build([A, B])
    paths = {e.path_low for e in eng.entries}
    base_a = D.norm_key(A)
    base_b = D.norm_key(B)

    print("== 1. 扫描时跳过缓存目录 ==")
    check("扫到 A 和 B 的根", base_a in paths and base_b in paths)
    check("普通目录进了索引", (base_a + "\\src") in paths and (base_a + "\\src\\file3.txt") in paths)
    check("library 目录本身还在（能搜到）", (base_a + "\\library") in paths)
    check("library 的子项**没**进去",
          not any(p.startswith(base_a + "\\library\\") for p in paths),
          "共 %d 条以 library 开头" % sum(1 for p in paths
                                          if p.startswith(base_a + "\\library\\")))
    check("PackageCache 的子项也没进去",
          not any(p.startswith(base_a + "\\packagecache\\") for p in paths))
    check("pruned 计数 = 2", st.pruned == 2, "pruned=%d" % st.pruned)
    check("统计里的文件数包含 A 的 src 和 B 的 keep",
          st.files >= 2, "files=%d dirs=%d" % (st.files, st.dirs))

    print("== 2. 只重建一个根，其它根保留 ==")
    with open(os.path.join(A, "src", "file3.txt"), "w", encoding="utf-8") as f:
        f.write("changed")
    with open(os.path.join(A, "src", "newfile.txt"), "w", encoding="utf-8") as f:
        f.write("new")
    before = len(eng.entries)
    st2 = eng.rebuild_one_root(A)
    paths2 = {e.path_low for e in eng.entries}
    check("B 的条目还在", (base_b + "\\keep\\keepme.txt") in paths2)
    check("A 的新文件出现了", (base_a + "\\src\\newfile.txt") in paths2)
    check("A 的旧条目没重复",
          sum(1 for p in paths2 if p == base_a or p.startswith(base_a + "\\")) ==
          len([e for e in eng.entries if e.path_low == base_a
               or e.path_low.startswith(base_a + "\\")]))
    check("总条目数合理（旧 %d → 新 %d）" % (before, len(eng.entries)),
          len(eng.entries) >= before, "差值 %d" % (len(eng.entries) - before))
    check("B 的文件没有被 A 的扫描覆盖", st2.files >= st.files - 2)

    print("== 3. 存盘/读回后仍然一致 ==")
    idx = os.path.join(tmp, "idx.gz")
    eng.save(idx)
    eng2 = D.IndexEngine(settings)
    ok = eng2.load(idx)
    paths3 = {e.path_low for e in eng2.entries}
    check("能读回", ok and len(eng2.entries) == len(eng.entries),
          "%d / %d" % (len(eng2.entries), len(eng.entries)))
    check("读回后 A/B 都在", (base_a + "\\src\\newfile.txt") in paths3
          and (base_b + "\\keep\\keepme.txt") in paths3)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n结果：" + ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
