# -*- coding: utf-8 -*-
"""外观设置（改名字 / 换图标）的接口测试。不需要载入索引，秒级跑完。"""
import base64
import json
import os
import shutil
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, r"D:\agent相关对话\文件和对话检索")
import server as S
import dshfind as D

TOK = "t0k"
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    if not cond:
        ok_all = False


httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
port = httpd.server_address[1]
S.Handler.service = None
S.Handler.token = TOK
S.Handler.window = None
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % port


def get(path, raw=False):
    url = base + path + ("?t=" if "?" not in path else "&t=") + TOK
    req = urllib.request.Request(url, headers={"X-Dsh-Token": TOK})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = r.read()
    return data if raw else json.loads(data)


def post(path, body):
    url = base + path + "?t=" + TOK
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"X-Dsh-Token": TOK,
                                          "Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def status_of(path):
    """返回 HTTP 状态码（204 也要能拿到）。"""
    url = base + path + "?t=" + TOK
    req = urllib.request.Request(url, headers={"X-Dsh-Token": TOK})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF+7SxQ"
    "AAAAAElFTkSuQmCC")

cfg_backup = None
icon_backup = {}
try:
    p = os.path.join(D.DATA_DIR, "settings.json")
    cfg_backup = open(p, "rb").read() if os.path.exists(p) else None
    for f in S.all_icon_files():
        icon_backup[os.path.basename(f)] = open(f, "rb").read()

    # 先把外观恢复成"干净"状态，否则用户已经设过的名字/图标会让"默认状态"那几条断言失败
    post("/api/appearance", {"title": ""})
    post("/api/appearance/icon/reset", {})

    print("== 初始状态 ==")
    a = get("/api/appearance")
    check("默认没有自定义名字", a.get("title") == "", repr(a.get("title")))
    check("默认没有自定义图标", a.get("icon") is None, str(a.get("icon")))
    code, _ = status_of("/favicon.ico")
    check("没图标时 /favicon.ico 返回 204", code == 204, "HTTP %d" % code)

    print("== 改名字 ==")
    r = post("/api/appearance", {"title": "  我的检索  "})
    check("保存返回 ok", r.get("ok"), str(r)[:80])
    check("名字去掉了首尾空格", r["appearance"]["title"] == "我的检索",
          repr(r["appearance"]["title"]))
    cfg = D.load_settings()
    check("写进了 settings.json", cfg.get("app_title") == "我的检索")
    check("GET 也能读到", get("/api/appearance")["title"] == "我的检索")

    r = post("/api/appearance", {"title": "x" * 41})
    check("超过 40 字被拒绝", r.get("ok") is False, r.get("error", ""))

    print("== 换图标 ==")
    r = post("/api/appearance/icon", {"data": "data:image/png;base64,"
                                              + base64.b64encode(PNG_1PX).decode()})
    check("上传 PNG 成功", r.get("ok"), str(r)[:80])
    check("识别成 png", r["appearance"]["icon"]["ext"] == "png",
          str(r["appearance"]["icon"]))
    code, body = status_of("/favicon.ico")
    check("现在 /favicon.ico 返回图片", code == 200 and body == PNG_1PX,
          "HTTP %d, %d 字节" % (code, len(body)))
    code, body = status_of("/appicon")
    check("/appicon 也能拿到", code == 200 and body[:8] == b"\x89PNG\r\n\x1a\n")

    r = post("/api/appearance/icon", {"data": base64.b64encode(b"not an image").decode()})
    check("非图片被拒绝", r.get("ok") is False, r.get("error", ""))

    r = post("/api/appearance/icon/reset", {})
    check("恢复默认图标", r.get("ok") and r["appearance"]["icon"] is None)
    code, _ = status_of("/favicon.ico")
    check("恢复后又是 204", code == 204, "HTTP %d" % code)

    print("== 窗口标题（中文要能编码进 URL）==")
    w = S.AppWindow("http://127.0.0.1:9/?t=x", title="我的 检索")
    check("AppWindow.title 是用户名字", w.title == "我的 检索", w.title)
    check("URL 里的 w 已百分号编码", "w=%E6%88%91" in w.url, w.url[-40:])
    w2 = S.AppWindow("http://127.0.0.1:9/?t=x")
    check("没设名字时退回随机标识", w2.title.startswith("dshfind-"), w2.title)

    print("== 清空名字 ==")
    r = post("/api/appearance", {"title": ""})
    check("清空后 title 为空", r.get("ok") and r["appearance"]["title"] == "")

finally:
    httpd.shutdown()
    # 还原配置和图标（把测试期间产生的都清掉，再把用户原来的写回去）
    for f in S.all_icon_files():
        try:
            os.unlink(f)
        except OSError:
            pass
    for name, blob in icon_backup.items():
        with open(os.path.join(D.DATA_DIR, name), "wb") as f:
            f.write(blob)
    if cfg_backup is not None:
        with open(os.path.join(D.DATA_DIR, "settings.json"), "wb") as f:
            f.write(cfg_backup)

print("\n结果：" + ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
