# -*- coding: utf-8 -*-
"""QA 用静态服务：只服务 web/ 目录 + 假的 /api 响应（不加载真实索引）。
专门用来复现「底栏状态文字太长」这类纯布局问题。

用法：python _qa_serve_stub.py 8795 [--short]
    --short  用短状态（去掉 needs_rescan），用来对比
"""
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
SHORT = "--short" in sys.argv

STATUS = {
    "entries": 3195185,
    "sessions": 42,
    "hotkey": "ctrl+shift+d",
    "noise_on": True,
    "watch_running": True,
    "watch_changes": 23859,
    "needs_rescan": not SHORT,
    "truncated": [] if SHORT else ["F:\\"],
    "loading": False,
    "ready": True,
    "quick_root": "F:\\",
    "refreshing": False,
}
SEARCH = {"hits": [], "ms": 1, "candidates": 0, "total": 0}
SESSION = {"hits": [], "ms": 1, "sessions": 0}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/"):
            if path == "/api/status":
                return self._json(STATUS)
            if path == "/api/search":
                return self._json(SEARCH)
            if path == "/api/session":
                return self._json(SESSION)
            return self._json({})
        # 真实服务里 /static/x 映射到 web/x，这里照做，否则样式和脚本全 404
        if path.startswith("/static/"):
            self.path = self.path[len("/static"):]
        elif path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            try:
                self.rfile.read(n)
            except OSError:
                pass
        self._json({})


if __name__ == "__main__":
    port = 8795
    for a in sys.argv[1:]:
        if a.isdigit():
            port = int(a)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("http://127.0.0.1:%d/?t=stub" % port, flush=True)
    httpd.serve_forever()
