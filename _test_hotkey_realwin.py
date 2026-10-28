# -*- coding: utf-8 -*-
"""最接近真人的验证：真的开一次窗口，把它最小化，然后按热键，看它会不会回到前台。

窗口标题是 AppWindow 随机生成的（dshfind-xxxx），所以不会和已开着的旧实例搞混。
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

sys.path.insert(0, r"D:\agent相关对话\文件和对话检索")
import server as S

u = ctypes.WinDLL("user32", use_last_error=True)


def find_by_title(title):
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lp):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.strip() == title:
                found.append(hwnd)
        return True

    u.EnumWindows(cb, 0)
    return found


def title_of(hwnd):
    n = u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


httpd, win, url = S.serve(port=8797, open_window=True)
if httpd is None:
    print("已有实例在跑，跳过这次真实窗口测试")
    sys.exit(3)
print("热键：", S.Handler.hotkey, "｜窗口标题：", win.title, flush=True)

hwnd = None
for _ in range(60):
    got = find_by_title(win.title)
    if got:
        hwnd = got[0]
        break
    time.sleep(0.5)
if not hwnd:
    print("没找到窗口（Edge 可能没起来）")
    sys.exit(4)

time.sleep(1.5)
for h in find_by_title(win.title):
    u.ShowWindow(h, 6)          # SW_MINIMIZE：先让它退到后台
time.sleep(0.8)
print("最小化后，该标题下的窗口状态：",
      [(h, "最小化" if u.IsIconic(h) else "正常") for h in find_by_title(win.title)], flush=True)

sender = S.D.GlobalHotkey(None)
sender.synth_press(S.Handler.hotkey)     # 真的按一下

ok = False
t0 = time.time()
while time.time() - t0 < 8.0:
    fg = u.GetForegroundWindow()
    live = [h for h in find_by_title(win.title) if not u.IsIconic(h)]
    if live and fg in live:
        ok = True
        break
    time.sleep(0.05)

fg = u.GetForegroundWindow()
print("按热键后，该标题下的窗口状态：",
      [(h, "最小化" if u.IsIconic(h) else "正常") for h in find_by_title(win.title)])
print("前台窗口：", fg, repr(title_of(fg) if fg else ""))
print("结果：", "窗口被还原并提到前台 ✅" if ok else "没有被提到前台 ❌")

try:
    if win.proc is not None:
        win.proc.terminate()
except Exception:
    pass
sys.exit(0 if ok else 2)
