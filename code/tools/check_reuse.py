#!/usr/bin/env python3
"""复用验证台：证明既有的注入签名机制原样可用于事实级弱点。

## 这个工具要回答的问题

任务集设计声称：既有的注入签名机制可以转置，注入对象从"文书文本"改为"案件事实"，
机制本身不变。这个声称若不实测，就只是一句话；而它承载了全部主指标的合法性，
所以必须变成可执行的检查。

本工具按操作者给出的路径加载判定机（`--matcher-module`），不重实现任何判定逻辑。
这一点是刻意的：

- 若本工具自己写一套判定，那证明的是"我写得出判定"，不是"既有的判定可用"；
- 判定机已随发布冻结，本工具只读它，不改它。

默认路径由 `code/tools/matcher_path.py` 按三档解析（环境变量 → 本仓钉版副本 →
上层仓库内的冻结副本）；独立导出的仓库靠 `code/vendor/signature.py` 工作。

## 用法

    python3 code/tools/check_reuse.py
    python3 code/tools/check_reuse.py --matcher-module <path>/signature.py \\
                                      --specimen experiment/tasks/specimens/F1-LIM-01-demo.json

    python3 code/tools/check_reuse.py --report   # 只报告实际判定，不比对期望（用于探查口径）

## 为什么标本里带了几个"看着像反例、其实是刻意口径"的项

判定口径有两层（tier1 宽松 / tier2 严格），二者对某些输入故意给出不同结果。
把这类输入连同理由写进标本，是为了让"口径"这件事可复核，否则后人看到
"只抄日期也授信"会以为是 bug，改掉它，然后在论文里报出一个无法解释的数字。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
PAPER_ROOT = HERE.parents[2]              # papers/F1-adjudication/

DEFAULT_SPECIMEN = PAPER_ROOT / "experiment" / "tasks" / "specimens" / "F1-LIM-01-demo.json"

def _load_matcher_path():
    """按文件路径加载同目录的解析器（不做 `import`）。

    本模块可能被测试套件以文件路径加载，那时脚本目录不在 `sys.path` 上，
    `import matcher_path` 会失败——而失败会被误读成"没有解析器可用"。
    """
    spec = importlib.util.spec_from_file_location(
        "f1_matcher_path", str(Path(__file__).resolve().with_name("matcher_path.py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_resolve_matcher = _load_matcher_path().resolve


class ReuseError(RuntimeError):
    pass


def load_matcher(path):
    """按路径加载判定机模块（不设 sys.path，不做相对引用）。"""
    if not path.is_file():
        raise ReuseError(
            "判定机不存在：%s\n"
            "  用 --matcher-module 指定，或设 CTD_LA_MATCHER 环境变量。\n"
            "  注：独立导出时需带自己的钉版副本，见 code/vendor/PROVENANCE.md。" % path)
    spec = importlib.util.spec_from_file_location("ctd_la_signature_matcher", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Finding:
    """最小 finding 替身：判定机只读 evidence / locus / message 三个属性。"""

    __slots__ = ("evidence", "locus", "message", "code", "defect_layer", "doc_id")

    def __init__(self, texts, code="f1", defect_layer="validity", doc_id=None):
        self.evidence, self.locus, self.message = list(texts) + [None] * (3 - len(texts))
        self.code = code
        self.defect_layer = defect_layer
        self.doc_id = doc_id


def check_specimen(matcher, specimen, report_only=False):
    """返回 (n_checked, failures, notes)。"""
    case_text = specimen["case_text"]
    weak, clean = case_text["weak"], case_text["clean"]

    # ── 第一步：物化签名（这一步本身就做痕迹核验）──────────────────
    try:
        signature = matcher.materialize(
            specimen["signature"], {}, weak, clean,
            where=specimen["specimen_id"], extra_vars=None)
    except matcher.SignatureError as exc:
        return 0, ["物化失败（痕迹核验不过）：%s" % exc], []

    n_grounded = sum(1 for c in signature["clauses"] if c.get("grounded"))
    notes = ["物化成功：%d 个子句，其中 grounded %d 条"
             % (len(signature["clauses"]), n_grounded)]
    notes.append("签名摘要：" + json.dumps(matcher.describe(signature), ensure_ascii=False))

    # 痕迹片段（供人复核"到底按什么算命中"）
    for i, clause in enumerate(signature["clauses"]):
        for term in clause["terms"]:
            if term["kind"] == "witness":
                notes.append("  子句%d 痕迹片段：%s"
                             % (i + 1, " / ".join(term["runs"])))
            elif term["kind"] == "lexeme":
                dropped = term.get("unquotable_dropped") or []
                extra = "（底本里已存在故剔除：%s）" % "、".join(dropped) if dropped else ""
                notes.append("  子句%d 词表：%s%s" % (i + 1, " / ".join(term["alts"]), extra))

    n, failures = 0, []
    for case in specimen["expected_findings"]["cases"]:
        n += 1
        finding = Finding(case["texts"])
        credited_t1 = matcher.match(matcher.finding_texts(finding), signature, tier=1) is not None
        credited_t2 = matcher.match(matcher.finding_texts(finding), signature, tier=2) is not None
        actual = "credited" if credited_t2 else "not_credited"
        # 主口径取 tier2（判定机侧的口径说明见 code/vendor/PROVENANCE.md）
        line = "  %-44s tier1=%-5s tier2=%-5s" % (
            case["label"][:44], credited_t1, credited_t2)
        if report_only:
            notes.append(line)
            continue
        if actual != case["expect"]:
            failures.append(
                "%s\n      期望 %s（tier%d），实际 %s（tier1=%s, tier2=%s）\n      理由：%s"
                % (case["label"], case["expect"], case["expect_tier"], actual,
                   credited_t1, credited_t2, case["why"]))
        else:
            notes.append(line + "  ✓")

    # ── 超长引文守卫（必须真的越界才算测到）─────────────────────
    guard = specimen.get("guards", {}).get("overbroad")
    if guard:
        n += 1
        cap = matcher.MAX_EVIDENCE_CHARS
        pad_to = int(guard["case"].get("pad_to") or (cap + 60))
        # 把底本重复到超过上限，保证切片后确实越界。
        # v1 的标本底本只有约 160 字，`weak[:300]` 切出来的仍是短串，
        # 于是守卫静默未触发而测试"通过"，这正是"看着装上了其实层是哑的"那类失效，
        # 故此处不靠标量常数，直接由 cap 推出所需长度，并在长度不足时报错而非放过。
        long_text = (weak * (pad_to // max(len(weak), 1) + 1))[:max(pad_to, cap + 1)]
        if len(long_text) <= cap:
            failures.append(
                "%s：构造的引文只有 %d 字，未超过上限 %d，**测不到守卫**"
                "（标本底本 %d 字）" % (guard["case"]["label"], len(long_text), cap, len(weak)))
        else:
            finding = Finding([long_text, "诉讼时效", "本案已过诉讼时效"])
            blocked = matcher.is_overbroad(finding)
            line = "  %-44s 引文 %d 字 / 上限 %d → is_overbroad=%s" % (
                guard["case"]["label"][:44], len(long_text), cap, blocked)
            if report_only:
                notes.append(line)
            elif not blocked:
                failures.append(
                    "%s：超长引文（%d 字 > 上限 %d）未被守卫拦下"
                    % (guard["case"]["label"], len(long_text), cap))
            else:
                notes.append(line + "  ✓")
    return n, failures, notes


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--matcher-module",
                    default=os.environ.get("CTD_LA_MATCHER"),
                    help="判定机模块路径（缺省按 matcher_path 的三档解析；也可用 CTD_LA_MATCHER）")
    ap.add_argument("--specimen", default=str(DEFAULT_SPECIMEN))
    ap.add_argument("--report", action="store_true",
                    help="只报告实际判定，不比对期望（用于探查口径）")
    args = ap.parse_args(argv)

    matcher_module = args.matcher_module
    if not matcher_module:
        resolved = _resolve_matcher(HERE.parent)
        if resolved is None:
            sys.stderr.write(
                "\n找不到注入签名判定机。用 --matcher-module 指定路径，"
                "或在 code/vendor/ 放置钉版副本（见 code/vendor/PROVENANCE.md）。\n")
            return 2
        matcher_module = str(resolved)

    try:
        matcher = load_matcher(Path(matcher_module))
    except ReuseError as exc:
        sys.stderr.write("\n%s\n" % exc)
        return 2

    specimen_path = Path(args.specimen)
    if not specimen_path.is_file():
        sys.stderr.write("标本不存在：%s\n" % specimen_path)
        return 1
    specimen = json.loads(specimen_path.read_text(encoding="utf-8"))

    print("=" * 72)
    print("复用验证：判定机 %s" % matcher_module)
    print("  标本 %s" % specimen_path.name)
    print("=" * 72)

    n, failures, notes = check_specimen(matcher, specimen, report_only=args.report)
    for line in notes:
        print(line)

    print("-" * 72)
    if args.report:
        print("仅报告模式：未比对期望（%d 项已列出）" % n)
        return 0
    if failures:
        print("失败 %d / %d 项：\n" % (len(failures), n))
        for f in failures:
            print("  ✗ " + f + "\n")
        return 1
    print("全部通过（%d 项断言）：既有判定机原样适用于事实级弱点。" % n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())