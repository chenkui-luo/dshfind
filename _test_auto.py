# -*- coding: utf-8 -*-
"""验证「新增文件 / 新增会话，不操作就能搜到」。

文件：真的建一个文件，轮询接口看到它出现/消失要多久。
会话：把刷新间隔调成 3 秒，看日志里是不是自己在那儿反复刷新。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "_auto_child.log")
PORT = 8798
TOK = "qatoken123"
BASE = "http://127.0.0.1:%d" % PORT

env = dict(os.environ)
env["DSHFIND_SESSION_REFRESH"] = "3"
env["PYTHONIOENCODING"] = "utf-8"


def get(path, **params):
    params["t"] = TOK
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Dsh-Token": TOK})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def search(q, limit=20):
    return get("/api/search", q=q, limit=limit, noise="1")


def wait_ready(timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            st = get("/api/status")
            if st.get("ready"):
                return st
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit("索引没能在 %ds 内就绪" % timeout)


log = open(LOG, "w", encoding="utf-8", errors="replace")
proc = subprocess.Popen([sys.executable, "_qa_serve.py", str(PORT)], cwd=HERE,
                        stdout=log, stderr=log, env=env)
try:
    st = wait_ready()
    print("就绪：%d 条 / %d 个会话" % (st["entries"], st["sessions"]))

    # ---- 新增文件 ----
    stamp = str(int(time.time()))
    name = "_新建文件探测_%s.md" % stamp
    path = os.path.join(HERE, name)
    t0 = time.time()
    with open(path, "w", encoding="utf-8") as f:
        f.write("hello\n")
    found = None
    while time.time() - t0 < 30:
        hits = search(name)["hits"]
        if any(h["name"] == name for h in hits):
            found = time.time() - t0
            break
        time.sleep(0.2)
    print("新增文件可搜到：", "%.1f 秒" % found if found else "30 秒内没出现 ❌")

    # ---- 改名 ----
    name2 = "_改名后探测_%s.md" % stamp
    path2 = os.path.join(HERE, name2)
    t0 = time.time()
    os.replace(path, path2)
    renamed = None
    while time.time() - t0 < 30:
        hits = search(name2)["hits"]
        if any(h["name"] == name2 for h in hits):
            renamed = time.time() - t0
            break
        time.sleep(0.2)
    print("改名后可搜到：", "%.1f 秒" % renamed if renamed else "30 秒内没出现 ❌")

    # ---- 删除 ----
    t0 = time.time()
    os.unlink(path2)
    gone = None
    while time.time() - t0 < 30:
        hits = search(name2)["hits"]
        if not any(h["name"] == name2 for h in hits):
            gone = time.time() - t0
            break
        time.sleep(0.2)
    print("删除后消失：", "%.1f 秒" % gone if gone else "30 秒内仍能搜到 ❌")

    # ---- 会话定时刷新 ----
    time.sleep(10)
    txt = open(LOG, encoding="utf-8", errors="replace").read()
    n = len(re.findall(r"会话增量：", txt))
    m = re.search(r"会话索引每 (\d+) 秒自动刷新", txt)
    print("会话定时刷新：日志里出现 %d 次（间隔设置：%s 秒）"
          % (n, m.group(1) if m else "?"))
    ok = bool(found and renamed and gone) and n >= 3
    print("结果：", "不用操作就能搜到 ✅" if ok else "有环节没接上 ❌")
    sys.exit(0 if ok else 2)
finally:
    try:
        proc.kill()
    except Exception:
        pass
    log.close()
