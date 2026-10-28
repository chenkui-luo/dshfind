# -*- coding: utf-8 -*-
"""单独验证「找不到 instance.json 时的硬杀兜底」。

用一个临时的 pythonw 假进程（命令行里带一个独一无二的标记），
把匹配串换成那个标记，只杀它 —— 不会碰到你正在用的 dshfind。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TAG = "dshfind-quit-fallback-probe"

# 起一个假的"后台进程"：pythonw 且命令行里带标记
dummy = subprocess.Popen(
    ["pythonw", "-c", "import time; time.sleep(120)  # " + TAG],
    cwd=HERE)
time.sleep(1.5)
print("假进程 PID：", dummy.pid)

env = dict(os.environ)
env["DSHFIND_QUIT_MATCH"] = TAG
# 故意指向一个不存在的 instance 文件，让它走兜底分支
env["DSHFIND_INSTANCE_FILE"] = os.path.join(HERE, "_no_such_instance.json")

r = subprocess.run([sys.executable, "dshfind.py", "--quit"], cwd=HERE,
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace", env=env, timeout=60)
print("退出码：", r.returncode)
print("输出：", (r.stdout or "").strip())
if (r.stderr or "").strip():
    print("stderr：", r.stderr.strip()[:300])

time.sleep(1.0)
alive = dummy.poll() is None
print("假进程还活着：", alive)
if alive:
    dummy.kill()
print("结果：", "兜底能杀掉 ✅" if not alive else "兜底没杀掉 ❌")
sys.exit(0 if not alive else 2)
