# -*- coding: utf-8 -*-
"""观察一次"启动"到底发生了什么：多久 ready、有没有自己开始重建、日志里说了什么。

用法：python -B _probe_startup.py [秒数]
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8791
TOK = "qatoken123"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 90
LOG = os.path.join(HERE, "_startup.log")

env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"

log = open(LOG, "w", encoding="utf-8", errors="replace")
proc = subprocess.Popen([sys.executable, "-B", "_qa_serve.py", str(PORT)],
                        cwd=HERE, stdout=log, stderr=log, env=env)
t0 = time.time()
last = None
try:
    while time.time() - t0 < LIMIT:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/status?t=%s" % (PORT, TOK),
                    timeout=10) as r:
                st = json.loads(r.read())
            cur = (st.get("ready"), st.get("loading"), st.get("refreshing"),
                   st.get("entries"), st.get("refresh_phase") or "")
            if cur != last:
                print("%5.1fs  ready=%-5s loading=%-5s refreshing=%-5s entries=%-9d %s"
                      % (time.time() - t0, cur[0], cur[1], cur[2], cur[3], cur[4]),
                      flush=True)
                last = cur
            if st.get("ready") and not st.get("refreshing") and st.get("load_seconds"):
                print("就绪耗时 %.1f 秒（服务端自报 load_seconds=%.1f）"
                      % (time.time() - t0, st["load_seconds"]), flush=True)
                # 就绪后再盯 20 秒，看会不会"自己开始重建"
                for _ in range(20):
                    time.sleep(1)
                    with urllib.request.urlopen(
                            "http://127.0.0.1:%d/api/status?t=%s" % (PORT, TOK),
                            timeout=10) as r:
                        st = json.loads(r.read())
                    if st.get("refreshing"):
                        print("⚠️ 就绪后又自己开始重建了（phase=%s）"
                              % st.get("refresh_phase"), flush=True)
                        break
                else:
                    print("就绪后 20 秒内没有自己重建 ✅", flush=True)
                break
        except Exception as e:
            print("%5.1fs  （连不上：%s）" % (time.time() - t0, e), flush=True)
        time.sleep(2)
finally:
    proc.kill()
    log.close()
    print("=== 子进程日志 ===")
    print(open(LOG, encoding="utf-8", errors="replace").read().strip())
