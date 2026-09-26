#!/usr/bin/env python3
"""生成 B2 检索语料（从共享规则库拼接，运行时产物、不入库）。

## 为什么是生成器而不是手工拼一份

手工拼一份的通行做法是把 `rules/` 下的若干文件按序拼成一个文本，头部写明来源。
那能跑，但语料内容随 `rules/` 演进会静默变化，两次实验之间规则库改了字，
检索材料的规模与内容就不同，而结果文件里看不出这一点。

故做成生成器：头部记下每个源文件的 sha256 与拼接顺序，
于是"这一轮 B2 用的语料是哪一版"可从语料文件本身核出来。
与 `derive_t_irr.py` 的"快照 + sha256"是同一套纪律。

## 语料选哪些源

选本案型直接适用的规则源（不是全部 `rules/`）：

| 源 | 为什么在语料里 |
|---|---|
| `commercial_civil.yaml` | 商事/民事诉讼规则（本引擎的主力案型） |
| `gates.yaml` | 闸门注册表：时效、受案范围、主体、证据质控等入口 |
| `appeal_procedure.yaml` | 期限与审级路由（时效类弱点的规则依据） |

刻意不含 `documents.yaml`：那是文书格式规则（属文书质检的领域），
放进来会让 B2 检索到与"案情弱点"无关的材料，等于给基线喂错上下文。

## 用法

    python3 code/tools/build_corpus.py --rules-root <path>/rules \\
        --out experiment/run/corpus/f1-rules.txt
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve()
PAPER_ROOT = HERE.parents[2]

# 拼接顺序即语料内的排列顺序；改动顺序会让检索得分变化，故写死在代码里而非靠目录序。
SOURCES = ("commercial_civil.yaml", "gates.yaml", "appeal_procedure.yaml")


class CorpusError(RuntimeError):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rules-root", default=os.environ.get("CTD_LA_RULES_ROOT"),
                    help="共享规则库目录（仓库内为 rules/）；也可用 CTD_LA_RULES_ROOT")
    ap.add_argument("--out", default=str(PAPER_ROOT / "experiment" / "run" / "corpus"
                                         / "f1-rules.txt"))
    args = ap.parse_args(argv)

    if not args.rules_root:
        sys.stderr.write(
            "缺少 --rules-root（或环境变量 CTD_LA_RULES_ROOT）。\n"
            "  `code/` 是独立导出单元，不假定仓库结构，故规则源路径须显式给出。\n")
        return 2
    root = Path(args.rules_root)
    if not root.is_dir():
        sys.stderr.write("规则库目录不存在：%s\n" % root)
        return 1

    parts, digest_lines, missing = [], [], []
    for name in SOURCES:
        f = root / name
        if not f.is_file():
            missing.append(name)
            continue
        parts.append("# ══════ 源：%s ══════\n%s" % (name, f.read_text(encoding="utf-8")))
        digest_lines.append("#   %-26s sha256 %s" % (name, sha256_file(f)))
    if missing:
        sys.stderr.write("规则源缺失：%s\n" % "、".join(missing))
        return 1

    header = "\n".join([
        "# F1 B2 检索语料（由 code/tools/build_corpus.py 生成，运行时产物、不入库）。",
        "# 源：仓库共享规则库 rules/ 下列文件，按此顺序拼接——",
        *digest_lines,
        "# 生成于 %s" % datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "# ⚠ 语料内容随规则库演进会变；sha256 就是用来核『这一轮用的是哪一版』的。",
        "",
    ])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    body = header + "\n\n".join(parts) + "\n"
    out.write_text(body, encoding="utf-8")

    print("语料已写出：%s" % out)
    print("  源 %d 个 / 合计 %d 字符" % (len(SOURCES), len(body)))
    for line in digest_lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())