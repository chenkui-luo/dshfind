"""验证全局热键的消息通路：注册是否成功、poll 能否收到 WM_HOTKEY。"""
import sys, ctypes
import ctypes.wintypes as wt

sys.path.insert(0, r"D:\agent相关对话\文件和对话检索")
import dshfind as D

fired = {"n": 0}
hk = D.GlobalHotkey(lambda: fired.__setitem__("n", fired["n"] + 1))
ok = hk.register("shift+ctrl+f")
print("注册全局热键: ok=%s err=%r" % (ok, hk.error))
if not ok:
    print("（如果失败，多半是被别的程序占用了）")
    sys.exit(1)

# 往本线程消息队列投一个 WM_HOTKEY，模拟系统送达
tid = ctypes.windll.kernel32.GetCurrentThreadId()
posted = ctypes.windll.user32.PostThreadMessageW(tid, D.HOTKEY_MSG_ID, hk.id, 0)
print("投递 WM_HOTKEY: %s" % bool(posted))
print("poll() -> %d" % hk.poll())
print("poll() 再来一次 -> %d（应为 0，队列已空）" % hk.poll())
hk.unregister()
print("注销完成, registered=%s" % hk.registered)

# 再注册一次，确认可重复
hk2 = D.GlobalHotkey(lambda: None)
print("重新注册: %s" % hk2.register("shift+ctrl+f"))
hk2.unregister()
sys.exit(0 if posted else 1)
