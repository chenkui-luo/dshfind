"""生成 dshfind 使用的拼音表数据文件。

数据来源：pypinyin 项目的 pinyin_dict.json（MIT License, (c) 2016 mozillazg, 闲耘），
其上游为 Unihan 的 kMandarin 读音。这里固化成"码点 -> 全拼 + 首字母"的紧凑文本表，
使工具运行时不依赖 pypinyin、不联网。

用法: python _build_pinyin.py
产物: pinyindata.pyz  (zip 内含 pinyin.tsv)
"""
import zipfile, json, os, sys, unicodedata, io

HERE = os.path.dirname(os.path.abspath(__file__))
WHL = os.path.join(HERE, "_build", "pypinyin-0.55.0.whl")
OUT = os.path.join(HERE, "pinyindata.pyz")


def strip_tone(syllable: str) -> str:
    """dài -> dai, lüe -> lve，去掉声调符号与非字母字符。"""
    decomposed = unicodedata.normalize("NFD", syllable)
    base = "".join(c for c in decomposed if not unicodedata.combining(c))
    base = base.replace("ü", "v").replace("Ü", "v")
    return "".join(c for c in base.lower() if "a" <= c <= "z")


def build_rows():
    with zipfile.ZipFile(WHL) as z:
        data = json.loads(z.read("pypinyin/pinyin_dict.json").decode("utf-8"))
    rows = []
    for cp_str, readings in data.items():
        cp = int(cp_str)
        if cp < 0x3400 or cp > 0x9FFF:
            continue  # 只保留 CJK 基本区 + 扩展A，够用且表更小
        full = strip_tone(readings.split(",")[0].strip())
        if not full:
            continue
        rows.append((cp, full, full[0]))
    rows.sort()
    return rows


def main():
    rows = build_rows()
    buf = io.StringIO()
    for cp, full, ini in rows:
        # 每行 "<十六进制码点>\t<全拼>"；首字母就是音节首字符。
        buf.write("%X\t%s\n" % (cp, full))
    payload = buf.getvalue().encode("utf-8")
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("pinyin.tsv", payload)
    print("entries:", len(rows))
    print("raw bytes:", len(payload), "-> pyz bytes:", os.path.getsize(OUT))
    covered = sum(1 for cp, _, _ in rows if 0x4E00 <= cp <= 0x9FFF)
    print("basic block U+4E00..9FFF covered: %d / 20992" % covered)
    for cp, full, ini in rows[:3] + rows[-3:]:
        print("  %X %s %s" % (cp, full, ini))


if __name__ == "__main__":
    main()
