"""端到端验证：文件监听让新建/删除的文件自动出现在搜索结果里（不需要手动刷新）。"""
import json
import os
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
tok = "watchtest"
S.Handler.service = svc
S.Handler.token = tok
threading.Thread(target=httpd.serve_forever, daemon=True).start()
svc.load_async()          # 正常启动时由 server.serve() 调用
base = "http://127.0.0.1:%d" % port


def get(path, **params):
    params["t"] = tok
    url = base + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Dsh-Token": tok})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [OK]   %s  %s" % (name, detail))
    else:
        fail += 1
        print("  [FAIL] %s  %s" % (name, detail))


t0 = time.time()
while time.time() - t0 < 180:
    st = get("/api/status")
    if st["ready"]:
        break
    time.sleep(0.5)
check("索引就绪", st["ready"], "%d 条 / %.1fs" % (st["entries"], st.get("load_seconds") or 0))
check("文件监听已启动", st.get("watch_running"), "watch_running=%s" % st.get("watch_running"))

d = r"D:\agent相关对话\文件和对话检索\_wtest"
os.makedirs(d, exist_ok=True)
uniq = "zzz监听验证%d" % int(time.time())
report = os.path.join(d, uniq + ".txt")


def hits_for(kw):
    r = get("/api/search", q=kw, limit=50)
    return [h for h in r["hits"] if uniq in h["disp"].lower()]


print("\n== 新建文件 ==")
open(report, "w", encoding="utf-8").write("x")
found = False
t0 = time.time()
for _ in range(40):          # 最多等 8 秒
    time.sleep(0.2)
    if hits_for(uniq):
        found = True
        break
check("新建文件自动可搜到（未手动刷新）", found, "用时 %.1fs" % (time.time() - t0))

print("\n== 删除文件 ==")
os.unlink(report)
gone = False
t0 = time.time()
for _ in range(40):
    time.sleep(0.2)
    if not hits_for(uniq):
        gone = True
        break
check("删除后自动消失（未手动刷新）", gone, "用时 %.1fs" % (time.time() - t0))

print("\n== 改名 ==")
a = os.path.join(d, uniq + "旧.txt")
b = os.path.join(d, uniq + "新.txt")
open(a, "w", encoding="utf-8").write("x")
# 等它先被监听收进去
t0 = time.time()
while time.time() - t0 < 8 and not hits_for(uniq + "旧"):
    time.sleep(0.2)
os.replace(a, b)
new_ok = old_ok = False
t0 = time.time()
for _ in range(40):
    time.sleep(0.2)
    r = get("/api/search", q=uniq + "新", limit=50)
    if any(uniq in h["disp"].lower() and "新" in h["disp"] for h in r["hits"]):
        new_ok = True
        break
t0b = time.time()
for _ in range(40):
    time.sleep(0.2)
    r2 = get("/api/search", q=uniq + "旧", limit=50)
    if not any("旧" in h["disp"] for h in r2["hits"]):
        old_ok = True
        break
check("改名后新名字可搜到", new_ok, "用时 %.1fs" % (time.time() - t0))
check("改名后旧名字消失", old_ok, "用时 %.1fs" % (time.time() - t0b))

os.unlink(b)
time.sleep(1.0)
os.rmdir(d)

st = get("/api/status")
check("状态里能看到监听统计", (st.get("watch_changes") or 0) > 0,
      "累计变更 %s 次" % st.get("watch_changes"))

print("\n%s  通过 %d / 失败 %d" % ("全部通过" if fail == 0 else "有失败项", ok, fail))
sys.stdout.flush()
sys.stderr.flush()
os._exit(0 if fail == 0 else 1)
