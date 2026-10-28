# -*- coding: utf-8 -*-
"""两件事的单元验证：

1. 名字索引替换失败的真凶：常驻只读连接没关 → os.replace 报 WinError 5。
2. 监听溢出时的策略：默认只提示（needs_rescan=True），**不自动全量重扫**。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, r"D:\agent相关对话\文件和对话检索")
import dshfind as D

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    if not cond:
        ok_all = False


print("== 1. 只读连接会挡住 os.replace ==")
tmpdir = tempfile.mkdtemp(prefix="dshfts-")
try:
    db = os.path.join(tmpdir, "x.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t(a)")
    con.commit()
    con.close()

    # 复刻 fts_search 的打开方式：mode=ro 的常驻连接
    D._fts_con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True,
                                 timeout=5, check_same_thread=False)
    D._fts_con.execute("SELECT 1 FROM t").fetchall()

    newdb = os.path.join(tmpdir, "x.db.new")
    c2 = sqlite3.connect(newdb)
    c2.execute("CREATE TABLE t(a)")
    c2.commit()
    c2.close()

    blocked = None
    try:
        os.replace(newdb, db)
    except PermissionError as e:
        blocked = e
    check("连接开着时替换会被拒绝（WinError 5）", blocked is not None,
          str(blocked)[:60] if blocked else "没被拒绝")

    # 现在用新加的 close_fts_conn() 关掉它
    shutil.copyfile(newdb, newdb + "2") if not os.path.exists(newdb) else None
    newdb2 = os.path.join(tmpdir, "x.db.new2")
    shutil.copyfile(newdb, newdb2) if os.path.exists(newdb) else None
    if not os.path.exists(newdb2):
        # 上一次 replace 成功过的话 newdb 已经不在了，重建一个
        c3 = sqlite3.connect(newdb2)
        c3.execute("CREATE TABLE t(a)")
        c3.commit()
        c3.close()

    D.close_fts_conn()
    replaced = False
    try:
        os.replace(newdb2, db)
        replaced = True
    except PermissionError as e:
        replaced = False
        print("    替换仍失败：", e)
    check("close_fts_conn() 关掉连接后可以替换", replaced)
    check("关闭后 _fts_con 已置空", D._fts_con is None)
finally:
    try:
        D.close_fts_conn()
    except Exception:
        pass
    shutil.rmtree(tmpdir, ignore_errors=True)

print("== 2. 监听溢出的策略 ==")
import server as S

svc = S.SearchService()
calls = []
svc.refresh = lambda: calls.append("refresh")     # 拦掉真扫盘
svc._on_fs_change({"overflow": True})
check("默认：只标记 needs_rescan", svc.needs_rescan is True)
check("默认：不会自己开扫", not calls, "refresh 调用 %d 次" % len(calls))
check("默认设置里 auto_rescan_on_overflow 是 false",
      S.D.load_settings().get("auto_rescan_on_overflow") is False)

svc2 = S.SearchService()
calls2 = []
svc2.refresh = lambda: calls2.append("refresh")
svc2.settings["auto_rescan_on_overflow"] = True
svc2._on_fs_change({"overflow": True})
check("打开开关时才会自动扫", len(calls2) == 1, "refresh 调用 %d 次" % len(calls2))

print("== 3. fts_build 在'已有常驻连接'的情况下也能替换成功 ==")
try:
    tmpdir2 = tempfile.mkdtemp(prefix="dshfts2-")
    path = os.path.join(tmpdir2, "nameindex.db")

    # 先造一个"旧库"，并像 fts_search 那样挂一个常驻只读连接上去
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    old.execute("CREATE TABLE ft(x)")
    old.commit()
    old.close()
    D._fts_con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True,
                                 timeout=5, check_same_thread=False)

    class FakeEntry:
        __slots__ = ("low",)

        def __init__(self, n):
            self.low = "file_%d.md" % n

    import types
    fake = types.SimpleNamespace(entries=[FakeEntry(i) for i in range(3000)],
                                 overlay=[], built_at=987654321)

    err = None
    try:
        D.fts_build(fake, path=path)
    except Exception as e:
        err = e
    check("fts_build 替换成功（不再 WinError 5）", err is None, str(err)[:70] if err else "")

    if err is None:
        m = D.fts_meta(path)
        check("新库的 index_built_at 对得上", m.get("index_built_at") == "987654321", str(m)[:70])
        check("count 记录了 3000 条", m.get("count") == "3000", "count=%s" % m.get("count"))
        # 查询能重新打开（懒连接）
        got = D.fts_search("file_1", engine=None, limit=10)
        check("重建后还能查询", bool(got), "命中 %d 条" % (len(got) if got else 0))
    shutil.rmtree(tmpdir2, ignore_errors=True)
finally:
    try:
        D.close_fts_conn()
    except Exception:
        pass

print("\n结果：" + ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
