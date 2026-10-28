# -*- coding: utf-8 -*-
"""端到端验证「彻底退出」：真起一个实例（带窗口），POST /api/quit，
然后确认进程没了、Edge 窗口也关了。

（这次不合成按键，所以不会打扰你在用的东西；只是会短暂开一个窗口出来。）
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "_quit_child.log")
u = ctypes.WinDLL("user32")

# 你机器上可能正开着正式的 dshfind：用独立的互斥体名字 + 不注册热键，
# 免得这次测试被单实例限制挡下，也免得抢走你在用的热键。
ENV = dict(os.environ)
ENV["DSHFIND_INSTANCE"] = "Local\\dshfind-quit-test"
ENV["DSHFIND_NO_HOTKEY"] = "1"
# 实例信息文件也换个位置，别覆盖正式实例的 data/instance.json
ENV["DSHFIND_INSTANCE_FILE"] = os.path.join(HERE, "_quit_instance.json")


def windows_named(prefix="dshfind"):
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _l):
        if not u.IsWindowVisible(h):
            return True
        n = u.GetWindowTextLengthW(h)
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, b, n + 1)
            if b.value.lower().startswith(prefix):
                found.append((h, b.value))
        return True

    u.EnumWindows(cb, 0)
    return found


log = open(LOG, "w", encoding="utf-8", errors="replace")
proc = subprocess.Popen([sys.executable, "server.py", "--port", "8799"],
                        cwd=HERE, stdout=log, stderr=log, env=ENV)
url = None
try:
    for _ in range(120):
        time.sleep(0.5)
        txt = open(LOG, encoding="utf-8", errors="replace").read()
        m = re.search(r"http://127\.0\.0\.1:\d+/\?t=[\w\-]+", txt)
        if m:
            url = m.group(0)
            break
    if not url:
        print("没拿到地址，子进程输出：")
        print(open(LOG, encoding="utf-8", errors="replace").read()[:800])
        sys.exit(1)
    print("子进程已起：", url[:40] + "…")

    # 等窗口出来（索引还在后台载入，窗口一般 2～3 秒内就有）
    hw = []
    for _ in range(40):
        hw = windows_named()
        if hw:
            break
        time.sleep(0.5)
    print("窗口：", hw)

    token = url.split("t=")[1]
    print("窗口存在：", bool(hw))

    # 走真正的用户路径：python dshfind.py --quit（读 instance.json 再请求退出）
    qlog = open(os.path.join(HERE, "_quit_cmd.log"), "w", encoding="utf-8")
    q = subprocess.run([sys.executable, "dshfind.py", "--quit"], cwd=HERE,
                       stdout=qlog, stderr=qlog, env=ENV, timeout=40)
    qlog.close()
    print("--quit 退出码：", q.returncode)
    print("--quit 输出：",
          open(os.path.join(HERE, "_quit_cmd.log"), encoding="utf-8").read().strip())

    exited = False
    for _ in range(40):
        time.sleep(0.25)
        if proc.poll() is not None:
            exited = True
            break
    time.sleep(1.0)
    left = windows_named()
    print("进程已退出：", exited, "｜退出码：", proc.poll())
    print("残留窗口：", left)
    ok = exited and not left
    print("结果：", "彻底退出 ✅" if ok else "还有东西没关 ❌")
    sys.exit(0 if ok else 2)
finally:
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    log.close()
