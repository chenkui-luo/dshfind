# -*- coding: utf-8 -*-
"""测试用的子进程：起一个带窗口的实例，标题是独一无二的，方便测试抓窗口。"""
import sys
import time
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server as S

httpd, win, url = S.serve(port=8797, open_window=True)
if httpd is None:
    print("serve() 拒绝启动", flush=True)
    sys.exit(1)
print("TITLE=" + win.title, flush=True)
print("URL=" + url, flush=True)
while True:
    time.sleep(1)
