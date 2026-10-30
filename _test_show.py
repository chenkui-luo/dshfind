# -*- coding: utf-8 -*-
"""验证 `--show`：已经在跑的实例被叫到前台（不是再开一个）。

用一个标题独一无二的测试实例，避免碰到你正在用的那个窗口。
"""
import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "_show_child.log")
u = ctypes.WinDLL("user32", use_last_error=True)
sys.path.insert(0, HERE)
import dshfind as D

env = dict(os.environ)
env["DSHFIND_INSTANCE"] = "Local\\dshfind-show-test"
env["DSHFIND_NO_HOTKEY"] = "1"
env["DSHFIND_INSTANCE_FILE"] = os.path.join(HERE, "_show_instance.json")
env["PYTHONIOENCODING"] = "utf-8"

SETTINGS = os.path.join(D.DATA_DIR, "settings.json")
settings_backup = open(SETTINGS, "rb").read() if os.path.exists(SETTINGS) else None


def windows_titled(title):
    hits = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lp):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.strip() == title:
                hits.append(hwnd)
        return True

    u.EnumWindows(cb, 0)
    return hits


log = open(LOG, "w", encoding="utf-8", errors="replace")
# 窗口标题来自 settings.app_title（页面会把 document.title 设成它），
# 所以临时改成一个独一无二的名字，测完再原样写回去。
try:
    cfg = D.load_settings()
    cfg["app_title"] = "dshfind-show-test"
    D.save_settings(cfg)
except Exception as e:
    print("改不了 settings（%s），测试可能抓不到窗口" % e)

proc = subprocess.Popen([sys.executable, "-B", "_show_child.py"],
                        cwd=HERE, stdout=log, stderr=log, env=env)
ok_all = False
try:
    title = None
    hw = []
    for _ in range(90):
        time.sleep(0.5)
        txt = open(LOG, encoding="utf-8", errors="replace").read()
        m = re.search(r"TITLE=(.+)", txt)
        if m:
            title = m.group(1).strip()
            hs = windows_titled(title)
            if hs:
                hw = hs
                break
    if not title or not hw:
        print("窗口没出来，子进程日志：")
        print(open(LOG, encoding="utf-8", errors="replace").read()[:600])
        sys.exit(1)
    print("窗口标题：", title, "｜hwnd:", hw)

    u.ShowWindow(hw[0], 6)          # 最小化
    time.sleep(0.9)
    print("最小化后：", [(h, "最小化" if u.IsIconic(h) else "正常") for h in hw])

    r = subprocess.run([sys.executable, "-B", "dshfind.py", "--show"], cwd=HERE,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=60)
    print("--show 退出码：", r.returncode)
    print("--show 输出：", (r.stdout or "").strip())
    if (r.stderr or "").strip():
        print("--show stderr：", r.stderr.strip()[:200])

    restored = False
    t0 = time.time()
    while time.time() - t0 < 8:
        live = [h for h in windows_titled(title) if not u.IsIconic(h)]
        fg = u.GetForegroundWindow()
        if live and fg in live:
            restored = True
            break
        time.sleep(0.05)
    count = len(windows_titled(title))
    print("窗口回到前台：", "是 ✅" if restored else "否 ❌")
    print("窗口个数：", count, "（应该还是 1 个）")
    ok_all = restored and count == 1
    print("结果：", "桌面快捷方式的行为正确 ✅" if ok_all else "不对 ❌")
finally:
    try:
        proc.kill()
    except Exception:
        pass
    log.close()
    # 把 settings.json 原样写回去（连同用户原来的 app_title）
    try:
        if settings_backup is not None:
            with open(SETTINGS, "wb") as f:
                f.write(settings_backup)
    except Exception as e:
        print("恢复 settings 失败：%s" % e)
sys.exit(0 if ok_all else 2)
