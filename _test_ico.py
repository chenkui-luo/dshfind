# -*- coding: utf-8 -*-
"""多尺寸图标打包的单元验证（不需要浏览器、不需要索引）。"""
import base64
import os
import struct
import sys
import zlib

sys.path.insert(0, r"D:\agent相关对话\文件和对话检索")
import server as S

ok = True


def check(name, cond, detail=""):
    global ok
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    if not cond:
        ok = False


def png(size):
    """造一张 size×size 的纯色 PNG（用来当各档尺寸）。"""
    rows = b"".join(b"\x00" + bytes((200, 120, 60, 255)) * size for _ in range(size))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


print("== 打 ICO ==")
sizes = [16, 32, 48, 256]
pairs = [(s, png(s)) for s in sizes]
ico = S.build_ico(pairs)
check("文件头是 ICONDIR", ico[:4] == b"\x00\x00\x01\x00", ico[:4].hex())
count = struct.unpack("<H", ico[4:6])[0]
check("条目数正确", count == len(sizes), str(count))
check("能读回各档尺寸", S.ico_sizes(ico) == sorted(sizes), str(S.ico_sizes(ico)))

# 逐条核对目录里的偏移与长度，确认每档都指向正确的 PNG 起点
for i in range(count):
    b = 6 + i * 16
    w, h, _, _, planes, bpp, nbytes, off = struct.unpack("<BBBBHHII", ico[b:b + 16])
    real = 256 if w == 0 else w
    payload = ico[off:off + nbytes]
    good = (payload[:8] == b"\x89PNG\r\n\x1a\n"
            and struct.unpack(">I", payload[16:20])[0] == real
            and bpp == 32 and planes == 1)
    check("第 %d 档（%dpx）指向正确的 PNG" % (i + 1, real), good,
          "偏移 %d、%d 字节" % (off, nbytes))

check("256 档在目录里记作 0", ico[6 + 3 * 16] == 0, str(ico[6 + 3 * 16]))
try:
    S.build_ico([])
    check("空列表应报错", False)
except ValueError:
    check("空列表会报错", True)

print("\n结果：" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
