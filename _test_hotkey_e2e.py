# -*- coding: utf-8 -*-
"""端到端验证：真起一次 serve()（不开窗口），合成按一下热键，看窗口唤出逻辑有没有被调用。

这一步是第 8 项那次 bug 的关键 —— 以前只验证了"注册成功"，没验证"按下去会触发"。
"""
import sys
import time

sys.path.insert(0, r"D:\agent相关对话\文件和对话检索")
import server as S

calls = []
httpd, win, url = S.serve(port=8796, open_window=False)
if httpd is None:
    print("serve() 拒绝启动（已有实例在跑），换个时间再测")
    sys.exit(1)

win.focus = lambda: calls.append(time.time())      # 拦截"把窗口提起来"这一步
print("实际生效的热键：", repr(S.Handler.hotkey))

time.sleep(0.4)
sender = S.D.GlobalHotkey(None)
sender.synth_press(S.Handler.hotkey)               # 真的按一下

t0 = time.time()
while time.time() - t0 < 2.5 and not calls:
    time.sleep(0.02)

print("按下去之后 focus() 被调用：", "%d 次 ✅" % len(calls) if calls else "0 次 ❌")
sys.exit(0 if calls else 2)
