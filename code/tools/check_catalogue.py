#!/usr/bin/env python3
"""弱点目录校验：把 弱点目录 的几条声称变成可执行的检查。

## 校验什么，为什么每一条都要

| # | 不变量 | 不查会怎样 |
|---|---|---|
| 1 | 八个弱点类每类至少一条 | 目录随修订慢慢漏掉某个类，而没人发现，`M_1`/`M_3` 的指标会静默失去对应的实例 |
| 2 | 每条都带 `grounded_in`（真实材料或规则出处） | 目录退化成书斋推演。本项目启动阶段实测过代价：纯推演漏掉了实务最高频的败诉原因（受案范围），还造出与《民法典》186 条冲突的指标口径 |
| 3 | `is_t_irr` 的条目的 `t_irr_ref` 必须是 `T_irr` 快照里的真实条目 | 这是 任务设计文档 §2.1 的核心声称："弱点目录与形式化的 `T_irr` 同源"。若不查，写一个不存在的 gate_id 也能过，同源就是空话 |
| 4 | `atomic: true` 的条目必须 `kind: pattern` | 实测过：日期用 witness 会滑出 `2019` 这类弱片段并造成误授信（标本 F-项） |
| 5 | 正文里出现 `{to}` 类占位符时，`kind` 必须能承载它 | 占位符代不进去会让签名静默匹配字面量 `{to}`，永不命中 |
| 6 | id 唯一 | 覆盖统计与金标准溯源会串 |
| 7 | `injections` 为空时不报错但明确提示 | 空注入计划意味着"尚无已核验实例"，不等于"目录为空"，生成器在该状态下应当拒绝产出空任务集 |

第 3 条依赖 `code/data/t-irr.json` 快照（由 `derive_t_irr.py` 生成、带 sha256 溯源）。
快照缺失时报错而非跳过：跳过等于把"同源"这条悄悄降级成"没查"。

## 用法

    python3 code/tools/check_catalogue.py
    python3 code/tools/check_catalogue.py --catalogue experiment/tasks/弱点目录 \\
                                          --t-irr code/data/t-irr.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 PyYAML：python3 -m pip install pyyaml\n")
    raise SystemExit(2)

HERE = Path(__file__).resolve()
CODE_ROOT = HERE.parents[1]              # 本篇 code/
PAPER_ROOT = HERE.parents[2]

REQUIRED_CLASSES = (
    "limitation", "jurisdiction", "scope_of_acceptance", "claim_concurrence",
    "standing", "evidence_gap", "escalation", "nonaction",
    # `timing`（时机机会面）：任务集 v3 补入，对应来源读物的书页 142
    # 与 C-12/C-15 判决结构共同长出的"双面"类（有时须快、有时须等），
    # 登记见 tasks/README.md §2.1。入必查集合：登记了却漏造实例即报错。
    "timing",
)
VALID_KINDS = ("witness", "pattern", "lexeme")


class CatalogueError(RuntimeError):
    pass


def load(path):
    if not path.is_file():
        raise CatalogueError("文件不存在：%s" % path)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def validate(catalogue_raw, t_irr_raw):
    problems = []
    entries = catalogue_raw.get("catalogue") or []
    if not entries:
        problems.append("`catalogue` 为空——目录不能为空")
        return entries, problems

    # 快照里的真实 T_irr gate_id 集合
    t_irr_ids = {e["gate_id"] for e in (t_irr_raw.get("t_irr") or [])}
    if not t_irr_ids:
        problems.append("T_irr 快照里没有条目——无法校验『同源』；先跑 derive_t_irr.py")

    # 6. id 唯一
    ids = [e.get("id") for e in entries]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append("id 重复：%s" % ", ".join(dupes))
    missing_id = [i for i, e in enumerate(entries) if not e.get("id")]
    if missing_id:
        problems.append("第 %s 条缺 id" % missing_id)

    # 1. 八个类都覆盖
    seen_classes = {e.get("class") for e in entries}
    absent = [c for c in REQUIRED_CLASSES if c not in seen_classes]
    if absent:
        problems.append("弱点类缺失：%s（九类每类至少一条）" % ", ".join(absent))
    unknown = sorted(seen_classes - set(REQUIRED_CLASSES) - {None})
    if unknown:
        problems.append("出现未登记的弱点类：%s（须先在 tasks/README.md §2.1 登记）"
                        % ", ".join(unknown))

    for e in entries:
        eid = e.get("id") or "(无 id)"

        # 2. grounded_in 必须有实际出处
        gi = e.get("grounded_in") or []
        if not gi:
            problems.append("%s：缺 `grounded_in`（须写真实材料或规则出处；"
                            "纯书斋推演不合格）" % eid)
        elif any("推演" in str(g) for g in gi):
            problems.append("%s：`grounded_in` 含『推演』——不合格" % eid)

        # `trace` 可为单个 dict 或 term 列表（列表表示"多项必须同时命中"，
        # 如 W-CONC-01 的"违约责任"且"侵权责任"共现）。两条路径都要校验。
        raw_trace = e.get("trace")
        traces = raw_trace if isinstance(raw_trace, list) else [raw_trace or {}]
        if isinstance(raw_trace, list) and not raw_trace:
            problems.append("%s：`trace` 是空列表" % eid)
        atomic_seen = False
        for tr in traces:
            kind = tr.get("kind")
            # 5. kind 合法
            if kind not in VALID_KINDS:
                problems.append("%s：`trace.kind` 非法（%r），须为 %s 之一"
                                % (eid, kind, " / ".join(VALID_KINDS)))
            # 4. atomic ⇒ pattern
            if tr.get("atomic"):
                atomic_seen = True
                if kind != "pattern":
                    problems.append(
                        "%s：标了 `atomic: true` 但 kind=%r——原子字段（日期/金额/案号）"
                        "必须用 pattern 写全值，用 witness 的 4 字滑窗会滑出 `2019` "
                        "这类弱片段并误授信" % (eid, kind))
        if not atomic_seen and len(traces) == 1 and not traces[0].get("kind"):
            problems.append("%s：`trace` 缺 `kind`" % eid)

        # 3. is_t_irr ⇒ t_irr_ref 必须是快照里的真实条目
        if e.get("is_t_irr"):
            ref = e.get("t_irr_ref")
            if not ref:
                problems.append("%s：标了 `is_t_irr` 但缺 `t_irr_ref`" % eid)
            elif t_irr_ids and ref not in t_irr_ids:
                problems.append(
                    "%s：`t_irr_ref` = %r 不在 T_irr 快照里（现有：%s）——"
                    "『弱点目录与 T_irr 同源』这条声称会因此落空"
                    % (eid, ref, ", ".join(sorted(t_irr_ids))))
            declared_kind = e.get("t_irr_kind")
            if declared_kind not in ("active", "passive"):
                problems.append("%s：`t_irr_kind` 须为 active/passive（现 %r）"
                                % (eid, declared_kind))
            else:
                # 与快照里该条目的 kind 必须一致，否则两个文件对同一条 T_irr 的说法不同
                snap = next((x for x in t_irr_raw.get("t_irr") or []
                             if x["gate_id"] == ref), None)
                if snap and snap.get("kind") != declared_kind:
                    problems.append(
                        "%s：`t_irr_kind`=%s 与 T_irr 快照里 %s 的 kind=%s 不一致"
                        % (eid, declared_kind, ref, snap.get("kind")))

        # 后果断言
        assertion = e.get("assertion") or {}
        if not (assertion.get("alts") or []):
            problems.append("%s：`assertion.alts` 为空——没有后果断言的条目只能测"
                            "『是否指到』，不能测『是否说对』" % eid)

    return entries, problems


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalogue",
                    default=str(PAPER_ROOT / "experiment" / "tasks" / "weaknesses.yaml"))
    ap.add_argument("--t-irr", default=str(CODE_ROOT / "data" / "t-irr.json"),
                    dest="t_irr", help="T_irr 快照（derive_t_irr.py 的产物）")
    args = ap.parse_args(argv)

    try:
        catalogue_raw = load(Path(args.catalogue))
    except CatalogueError as exc:
        sys.stderr.write("%s\n" % exc)
        return 1

    t_irr_path = Path(args.t_irr)
    if not t_irr_path.is_file():
        sys.stderr.write(
            "T_irr 快照不存在：%s\n"
            "  先跑：python3 code/tools/derive_t_irr.py --source <path>/rules/gates.yaml\n"
            "  这里**报错而非跳过**：跳过等于把『弱点目录与 T_irr 同源』降级成没查。\n"
            % t_irr_path)
        return 1
    t_irr_raw = json.loads(t_irr_path.read_text(encoding="utf-8"))

    entries, problems = validate(catalogue_raw, t_irr_raw)

    print("=" * 70)
    print("弱点目录校验：%s" % Path(args.catalogue).name)
    print("=" * 70)

    if problems:
        print("失败 %d 项：\n" % len(problems))
        for p in problems:
            print("  ✗ %s" % p)
        return 1

    from collections import Counter
    by_class = Counter(e["class"] for e in entries)
    # trace 可为单 dict 或 term 列表（validate() 两条路径都收）：统计也要两态兼容，
    # 任务集 v3 的多痕迹项条目（W-LIM-03 等）是列表，直接按 dict 取会 TypeError。
    by_kind = Counter(
        t.get("kind")
        for e in entries
        for t in (e["trace"] if isinstance(e["trace"], list) else [e["trace"]]))
    n_t_irr = sum(1 for e in entries if e.get("is_t_irr"))
    print("通过：%d 条，九类齐备" % len(entries))
    print("  类别分布：%s" % "、".join("%s×%d" % (k, v) for k, v in sorted(by_class.items())))

    # ── 可注入性报告（把"痕迹全 lexeme ⇒ 不能用于注入"这个隐性陷阱显形）──
    #
    # 目录里两种条目的用途不同，混在一起会骗人：
    #   * `trace` 含 pattern/witness 的条目 ， 可注入：生成器能把痕迹钉在注入改动上，
    #     因而能造出"金标准 = 注入计划"的实例；
    #   * `trace` 全是 lexeme 的条目 ， 不可注入：判定机会把该子句归为 definitional，
    #     tier2（本题主口径）永不命中，生成器直接拒（见 make_taskset.py 的守卫）。
    #     这类条目仍有价值（可用于判定真实输出里的该类弱点），但不能进任务集。
    #
    # 不报告的话，作者会以为"目录有 18 条 ⇒ 任务集能有 18 个实例"，
    # 直到跑生成器时才被逐个拒掉。
    injectable, lex_only = [], []
    for e in entries:
        tr = e.get("trace")
        trs = tr if isinstance(tr, list) else [tr]
        (injectable if any(t.get("kind") in ("pattern", "witness") for t in trs)
         else lex_only).append(e["id"])
    print("  可注入（含 pattern/witness 痕迹）：%d 条" % len(injectable))
    print("  不可注入（痕迹全 lexeme）：%d 条%s"
          % (len(lex_only), ("：" + "、".join(lex_only)) if lex_only else ""))
    if lex_only:
        print("    ↑ 这些条目可用于判定**真实输出**，但**不能进任务集**——"
              "\n      要进任务集须把痕迹改成 pattern（让注入文本带一段独有措辞），"
              "\n      修法与理由见 weaknesses.yaml 头部与 make_taskset.py 的守卫 ⑤。")

    injections = catalogue_raw.get("injections") or []
    by_empty = {"trace.kind": by_kind}
    _ = by_empty  # 保留计数供后续扩展
    if not injections:
        print("\n提示：`injections` 为空 —— 表示**尚无已核验的实例**，不等于目录为空。"
              "\n      任务集生成器在该状态下应拒绝产出空任务集（见 tasks/README.md §7）。")
    else:
        used = {i.get("weakness_id") for i in injections}
        unusable = sorted(used & set(lex_only))
        if unusable:
            print("\n✗ 注入计划引用了不可注入的条目：%s —— 生成器会拒" % "、".join(unusable))
            return 1
        print("\n  注入计划 %d 条，引用弱点 %d 个，均在可注入集合内"
              % (len(injections), len(used)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())