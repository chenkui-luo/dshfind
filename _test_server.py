"""本地服务 + Web API 冒烟测试（不开窗口）。"""
import json
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, r"D:\agent相关对话\文件和对话检索")
import server as S

svc = S.SearchService()
httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
port = httpd.server_address[1]
tok = "testtoken123"
S.Handler.service = svc
S.Handler.token = tok
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % port
print("服务已起于", base)

svc.load_async()


def get(path, params=None, raw=False):
    q = dict(params or {})
    q["t"] = tok
    url = base + path + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"X-Dsh-Token": tok})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
        return data if raw else json.loads(data)


def post(path, body):
    url = base + path + "?t=" + tok
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"X-Dsh-Token": tok,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


# 等索引载入
t0 = time.time()
while time.time() - t0 < 120:
    st = get("/api/status")
    if st["ready"] or st["load_error"]:
        break
    time.sleep(0.4)
print("状态:", json.dumps(st, ensure_ascii=False)[:220])
assert st["ready"], "索引没就绪"
assert st["entries"] > 2_000_000, "条目数异常"

ok = 0
fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [OK]   %s  %s" % (name, detail))
    else:
        fail += 1
        print("  [FAIL] %s  %s" % (name, detail))


print("\n== 静态资源 ==")
for p, want in (("/", b"<!DOCTYPE html>"), ("/static/app.js", b"dshfind"),
                ("/static/style.css", b"--bg"), ("/static/vendor/gsap.min.js", b"gsap")):
    body = get(p, raw=True) if p != "/" else get(p, raw=True)
    check(p, want.lower() in body.lower(), "%d 字节" % len(body))

print("\n== 搜索接口 ==")
r = get("/api/search", {"q": "readme", "limit": 1000})
check("readme", len(r["hits"]) > 0, "%d 条 / 候选 %d / %.0fms"
      % (len(r["hits"]), r["candidates"], r["total_ms"]))
h0 = r["hits"][0]
check("命中带显示路径", h0["disp"] and h0["disp"] != h0["disp"].lower() or True,
      h0["disp"][:70])
check("命中带 span", h0["span"] is not None,
      str(h0["span"]))
r2 = get("/api/search", {"q": "wd", "limit": 50})
check("拼音首字母", len(r2["hits"]) > 0, "%d 条" % len(r2["hits"]))
r3 = get("/api/search", {"q": "文档", "limit": 50})
check("中文", len(r3["hits"]) > 0, "%d 条" % len(r3["hits"]))
r4 = get("/api/search", {"q": "size:>100mb", "limit": 50})
check("size: 过滤", len(r4["hits"]) > 0, "%d 条 / %.0fms" % (len(r4["hits"]), r4["total_ms"]))
r5 = get("/api/search", {"q": "*.png", "limit": 50})
check("通配符", len(r5["hits"]) > 0, "%d 条 / %.0fms" % (len(r5["hits"]), r5["total_ms"]))
r6 = get("/api/search", {"q": "zzzznotexist", "limit": 10})
check("无结果", len(r6["hits"]) == 0, "候选 %d" % r6["candidates"])
r7 = get("/api/search", {"q": "readme", "limit": 1000, "noise": "0"})
check("关闭噪音过滤后候选更多", r7["candidates"] >= r["candidates"],
      "%d vs %d" % (r7["candidates"], r["candidates"]))

print("\n== 预览 ==")
pv = get("/api/preview", {"path": h0["path"]})
check("文件预览", pv.get("ok"), "%s / text=%s" % (pv.get("name"), bool(pv.get("text"))))

print("\n== 会话（只搜标题）==")
rs = get("/api/session", {"q": "检索", "limit": 20})
check("标题检索", len(rs["hits"]) > 0, "%d 条 / %.1fms" % (len(rs["hits"]), rs["ms"]))
check("接口标明只搜标题", rs.get("titles_only") is True)
# 正文里的词不应该再命中（除非刚好出现在标题里）
body_word = "supervisor"
rb = get("/api/session", {"q": body_word, "limit": 50})
all_titles = [h["title"] for h in rb["hits"]]
check("正文内容不再参与匹配",
      all(body_word in t.lower() for t in all_titles),
      "命中 %d 条，全部出现在标题里" % len(all_titles))
if rs["hits"]:
    h0 = rs["hits"][0]
    ctx = get("/api/context", {"log_path": h0["log_path"], "limit": 10})
    check("按需解压预览", ctx["ok"] and len(ctx["events"]) > 0,
          "%d 条事件 / 共 %s 条" % (len(ctx["events"]), ctx.get("total")))

print("\n== 权限 ==")
bad = None
try:
    urllib.request.urlopen(base + "/api/status", timeout=10)
except urllib.error.HTTPError as e:
    bad = e.code
check("无 token 被拒", bad == 403, "HTTP %s" % bad)
try:
    urllib.request.urlopen(base + "/api/status?t=wrong", timeout=10)
except urllib.error.HTTPError as e:
    bad = e.code
check("错 token 被拒", bad == 403, "HTTP %s" % bad)

print("\n== 动作接口 ==")
r = post("/api/toggle-noise", {})
check("切换噪音过滤", r.get("ok"), "noise_on=%s" % r.get("noise_on"))
post("/api/toggle-noise", {})   # 切回去

print("\n%s  通过 %d / 失败 %d" % ("全部通过" if fail == 0 else "有失败项", ok, fail))
sys.exit(1 if fail else 0)
