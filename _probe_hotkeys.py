# -*- coding: utf-8 -*-
"""看这台机器上哪些候选组合键现在还能注册（区分"被别的程序占了"和"被另一个 dshfind 占了"）。"""
import sys

sys.path.insert(0, r"D:\agent相关对话\文件和对话检索")
import dshfind as D

CANDS = ["shift+ctrl+f", "ctrl+shift+g", "ctrl+alt+f", "ctrl+shift+d",
         "ctrl+shift+k", "ctrl+alt+g", "shift+ctrl+space", "ctrl+shift+j",
         "ctrl+shift+l", "ctrl+alt+d", "ctrl+alt+space"]

for c in CANDS:
    hk = D.GlobalHotkey(lambda: None)
    ok = hk.register(c)
    print("%-18s %s" % (c, "可以注册 ✅" if ok else "被占用 ❌（%s）" % hk.error))
    if ok:
        hk.unregister()
