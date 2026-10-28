# -*- coding: utf-8 -*-
"""验证一个假设：RegisterHotKey 注册在 A 线程、poll 在 B 线程，能不能收到 WM_HOTKEY。

对照组：注册和轮询都在主线程（自检里就是这么测的，所以自检一直是绿的）。
实验组：注册在主线程、轮询在子线程（serve() 里现在就是这么干的）。

测试用的组合键是 ctrl+shift+f11 / ctrl+shift+f12，避开你日常在用的键。
按键用 keybd_event 合成 —— RegisterHotKey 对合成输入一样会触发。
"""
import ctypes
import sys
import threading
import time

sys.path.insert(0, r"D:\agent相关对话\文件和对话检索")
import dshfind as D

u = ctypes.WinDLL("user32", use_last_error=True)
VK_CONTROL, VK_SHIFT = 0x11, 0x10
KEYEVENTF_KEYUP = 0x0002


def send_combo(vk):
    u.keybd_event(VK_CONTROL, 0, 0, 0)
    u.keybd_event(VK_SHIFT, 0, 0, 0)
    u.keybd_event(vk, 0, 0, 0)
    time.sleep(0.04)
    u.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    u.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)
    u.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def drain(hk, seconds):
    fired = 0
    t0 = time.time()
    while time.time() - t0 < seconds:
        if hk.poll():
            fired += 1
        time.sleep(0.02)
    return fired


def same_thread_case(label, spec, vk):
    hk = D.GlobalHotkey(lambda: None)
    if not hk.register(spec):
        print("%-10s 注册失败：%s" % (label, hk.error))
        return
    time.sleep(0.2)
    send_combo(vk)                  # 主线程：注册 + 发键 + 轮询，都在同一个线程
    n = drain(hk, 1.0)
    hk.unregister()
    print("%-10s 注册与轮询同线程：%s" % (label, "收到 WM_HOTKEY ✅" if n else "没收到 ❌"))


def cross_thread_case(label, spec, vk):
    hk = D.GlobalHotkey(lambda: None)
    if not hk.register(spec):
        print("%-10s 注册失败：%s" % (label, hk.error))
        return
    box = {"n": 0}

    def poller():
        box["n"] = drain(hk, 1.6)

    th = threading.Thread(target=poller, daemon=True)
    th.start()
    time.sleep(0.25)
    send_combo(vk)                  # 主线程注册，子线程轮询
    th.join()
    hk.unregister()
    print("%-10s 注册在主线程、轮询在子线程：%s"
          % (label, "收到（跨线程没问题）" if box["n"] else "没收到 —— 就是这个问题"))


same_thread_case("对照 F12", "shift+ctrl+f12", 0x7B)
cross_thread_case("实验 F11", "shift+ctrl+f11", 0x7A)
