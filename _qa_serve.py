# -*- coding: utf-8 -*-
"""QA 用：只起服务（不注册热键、不开窗口），打印带 token 的 URL 后常驻。"""
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, r"D:\agent相关对话\文件和对话检索")
import server as S

svc = S.SearchService()
httpd = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8791),
                            S.Handler)
tok = "qatoken123"
S.Handler.service = svc
S.Handler.token = tok
threading.Thread(target=httpd.serve_forever, daemon=True).start()
svc.load_async()
url = "http://127.0.0.1:%d/?t=%s" % (httpd.server_address[1], tok)
print(url, flush=True)
while True:
    time.sleep(1)
