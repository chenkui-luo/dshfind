# 来源与许可

本工具是原创代码，**MIT 许可**（见 [LICENSE](LICENSE)）。它借鉴了几份外部资源，
按下面的方式记录出处；每一项都写明**作者、许可和使用方式**，与 [README.md](README.md)
末尾的「参考仓库与致谢」表一致。

---

## 1. oil-oil/oil-find —— 设计参照

- 仓库：https://github.com/oil-oil/oil-find
- 许可：**MIT License**，`Copyright (c) 2026 Lin Zhihuang`
- 用途：整体设计参照。本工具**没有复制它的源代码**（它是 Swift + C，这里是 Python），
  但下面这些设计要点直接来自它的实现与 `docs/ARCHITECTURE.md`：
  - 「全部文件名常驻内存、按输入实时收敛」的整体形态
  - 查询语法的扁平两级结构：空格 = AND、`|` 优先级更高 = OR、`!` = NOT、无括号
  - `ext:` `kind:` `size:` `dm:` `regex:` `case:` `file:` `folder:` 这套过滤器，以及
    「裸数字表示精确大小」「十进制单位而非二进制」「通配符锚定整个文件名」等细节
  - 加性整数打分（四级匹配质量 ×200、名字长度惩罚、噪音/隐藏/深度惩罚）
  - 目录噪音标记父传给子（`noise` 继承）
  - `kind:` 的九个名字（folder/app/doc/image/video/audio/code/archive）
  - 拼音「全拼 + 首字母」双键、查询时也算一遍的做法

MIT 许可要求保留版权与许可声明。上面这段即是对该作品的署名。

## 2. pypinyin —— 拼音数据

- 仓库：https://github.com/mozillazg/pypinyin
- 许可：**MIT License**，`Copyright (c) 2016 mozillazg, 闲耘 <hotoo.cn@gmail.com>`
- 用途：`pinyindata.pyz` 里的「汉字 → 拼音」数据来自 pypinyin 0.55.0 的
  `pypinyin/pinyin_dict.json`（其上游是 Unihan 的 `kMandarin` 读音）。
  本工具只取每个字的**第一个读音**、去掉声调符号，重新打包成
  「码点 → 音节」的紧凑表，使运行时**不依赖 pypinyin、不联网**。
- 覆盖：U+3400–U+9FFF，共 26,703 字（CJK 基本区 20924 / 20992）。

重新生成这张表（需要 `_build/pypinyin-0.55.0.whl`）：

```bat
python _build_pinyin.py
```

## 3. DeepSeek Harness（DSH）—— 会话日志格式

- 用途：本工具读取 DSH 写在 `~/.dsh/sessions/**/session.v*.jsonl.zstd` 的会话日志。
- 日志格式（「拼接的多帧 zstd、每帧带校验和」）由 DSH 自己的
  `@deepseek-ai/dsh-session-persistence-jsonl` 包定义，其 README 有完整说明。
  `bridge.js` 里的 `scanZstdFrames()` 函数是照着该包 `lib/index.js` 里同名函数的
  帧结构逻辑重写的（识别帧魔数、帧头描述符、块头，区分完整帧与撕裂的尾帧）。
- 本工具**只读** DSH 的数据，绝不写入 `~/.dsh` 下的任何文件，
  也不会打开 DSH 自己的派生索引库。

## 4. GSAP —— 界面动效

- 项目：https://gsap.com/
- 版本：**3.13.0**，文件在 `web/vendor/gsap.min.js`（72,435 字节，取自 unpkg）
- 许可：GreenSock 的 **Standard License**（见 gsap.com/standard-license）。
  按 GreenSock 现行政策，GSAP 及其全部插件对一般用途免费，包含商用；
  本工具只是直接引用未修改的发行版文件，未做二次分发以外的用途。
  文件头保留了原始版权声明：
  `Copyright 2025, GreenSock. All rights reserved. Subject to the terms at https://gsap.com/standard-license`
- 运行时**不联网**：GSAP 已本地化，页面不引用任何 CDN。

## 5. 其他

- SQLite FTS5（含 trigram 分词器）：公有领域。
  "名字 trigram 索引"和"会话全文索引"都构建在它上面。
- Python 标准库 `http.server`：本地 HTTP 服务（只监听 127.0.0.1）。
- 拼音表打包用 `zipfile`，文件索引用 `marshal` + `gzip`，都是标准库。
- 全局热键用 `RegisterHotKey`（user32）；路径大小写还原用 `os.scandir` 按目录比对
  （`GetLongPathNameW` 实测不保证逐段还原大小写）；DPI 感知用
  `SetProcessDpiAwarenessContext`（user32 / shcore）。都是 Windows 系统 API。
- 界面字体只用系统自带字体（Segoe UI / Microsoft YaHei UI / Consolas），不引入网络字体。
