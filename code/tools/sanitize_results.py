#!/usr/bin/env python3
"""把结果 JSON 里的本机绝对路径改写为仓库相对记号（公开前的清洗步骤）。

## 为什么需要它

`harness.py` 把运行环境写进每份结果文件的 `environment` 段（判定机路径、臂模块
路径、运行目录等），这些值是本机上算出来的绝对路径。结果是实验证据，会被公开：
留着它们，就把开发机的目录结构与用户名一并发布了。

实测口径：全量结果里带绝对路径的字段是 `environment.matcher_module`
（50 份），另有 5 份在同类字段上带路径。

## 改写规则（信息不丢，只丢本机前缀）

    /…/papers/<篇目录>/…        →  <repo>/papers/<篇目录>/…   （保留可核查的相对位置）
    <家目录>/…                   →  <home>/…
    其他绝对路径                 →  <abs>/<文件名>

## 为什么不走 `json.load` + `json.dumps`

那会把整份文件的缩进、键序、数字字面量全部重排，几十份结果文件产生巨大的纯格式
差异，把真正的改动埋掉。这里只对"值以 `/` 开头"的字符串做文本级替换，其余字节不动。

## 用法

    python3 code/tools/sanitize_results.py --check      # 只报告，不写
    python3 code/tools/sanitize_results.py              # 就地清洗
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
PAPER_ROOT = HERE.parents[2]
DEFAULT_RESULTS_DIR = PAPER_ROOT / "experiment" / "results"

# "key": "/absolute/path" ， 只匹配值以 `/` 开头的字符串值
ABS_VALUE_RE = re.compile(r'"(?P<key>[^"]+)":(?P<sep>\s*)"(?P<value>/[^"]*)"')
# 兜底：任何残留的家目录写法（含嵌在长句里的）
LEAK_RE = re.compile(r"(?:/Users/|/home/)")


def scrub_value(value):
    """把一条绝对路径改写成不含本机信息的相对记号；返回 (新值, 是否改动)。"""
    m = re.search(r"/(papers|rules)/", value)
    if m:
        return "<repo>" + value[m.start():], True
    home = os.path.expanduser("~")
    if home and value.startswith(home):
        return "<home>" + value[len(home):], True
    tail = value.rstrip("/").rsplit("/", 1)[-1]
    return "<abs>/" + tail, True


def scrub_text(text):
    """改写一份结果文件的文本；返回 (新文本, 替换条数)。"""
    n = 0

    def repl(m):
        nonlocal n
        new, changed = scrub_value(m.group("value"))
        if not changed:
            return m.group(0)
        n += 1
        return '"%s":%s%s' % (m.group("key"), m.group("sep"),
                              json.dumps(new, ensure_ascii=False))

    text = ABS_VALUE_RE.sub(repl, text)

    # 兜底：嵌在长字符串里的家目录前缀
    home = os.path.expanduser("~")
    if home and home in text:
        n += text.count(home)
        text = text.replace(home, "<home>")
    return text, n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR),
                    help="结果目录（默认 <篇目录>/experiment/results）")
    ap.add_argument("--check", action="store_true",
                    help="只检查并报告，不写回；有残留时退出码 1")
    args = ap.parse_args(argv)

    root = Path(args.results_dir)
    if not root.is_dir():
        sys.stderr.write("结果目录不存在：%s\n" % root)
        return 2

    files = sorted(root.rglob("*.json"))
    if not files:
        sys.stderr.write("没有 .json 结果文件：%s\n" % root)
        return 2

    touched, total, leaked = [], 0, []
    for f in files:
        text = f.read_text(encoding="utf-8")
        new, n = scrub_text(text)
        if args.check:
            if n:
                touched.append((f, n))
            if LEAK_RE.search(new):
                leaked.append(f)
            continue
        if n:
            f.write_text(new, encoding="utf-8")
            touched.append((f, n))
            total += n

    if args.check:
        if touched:
            print("待清洗 %d 份（共 %d 处本机路径）："
                  % (len(touched), sum(n for _, n in touched)))
            for f, n in touched:
                print("  %s（%d 处）" % (f.relative_to(root), n))
        if leaked:
            print("\n仍有 /Users/ 或 /home/ 残留 %d 份：" % len(leaked))
            for f in leaked:
                print("  %s" % f.relative_to(root))
            return 1
        if not touched:
            print("干净：%d 份结果文件里没有本机绝对路径。" % len(files))
        return 0

    if not touched:
        print("无需改动：%d 份结果文件里没有本机绝对路径。" % len(files))
        return 0
    print("已清洗 %d 份、共 %d 处：" % (len(touched), total))
    for f, n in touched:
        print("  %s（%d 处）" % (f.relative_to(root), n))
    if LEAK_RE.search("".join(f.read_text(encoding="utf-8") for f in files)):
        sys.stderr.write("警告：仍有 /Users/ 或 /home/ 残留，请复查。\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())