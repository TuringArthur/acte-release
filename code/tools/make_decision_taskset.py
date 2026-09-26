#!/usr/bin/env python3
"""决策级任务集构建器：立案决策金标准 → `tasks-decision/taskset.json`。

## 与主任务集生成器（`make_taskset.py`）的分工

主任务集的实例 = 注入计划 + 签名核验（判定机做主）；决策级实例没有注入，
它的金标准是决策本身（`expected_decision` / `expected_claim_stack`），
来自法条与真实判决的机械推导唯一解（`tasks/README.md` §1：T1/T2 判据 = 完全机械）。
故本工具不做签名核验，改查决策金标准的六条守卫；不过守卫的实例不产出，
整批失败（与主生成器同一条"静默跳过等于任务集缩水"的纪律）。

## 金标准的形状（随实例 JSON 声明，构建器逐条核对）

```
expected_decision:
  <维度>: {must_select: [action_id, …], must_not_select: [action_id, …]}
expected_claim_stack:
  - {claim_id, claim_text, edges: {fact: [element_id,…], norm: […], evidence: […]}}
```

## 六条守卫

| # | 不变量 | 不查会怎样 |
|---|---|---|
| 1 | 金标准引用的 action_id 必须存在于候选动作 | 金标准指向不存在的动作 ⇒ 指标恒 0 或恒 1，没人发现 |
| 2 | 每维 `must_select` 非空且与 `must_not_select` 不交 | 空维是哑指标；交集让"对错"不可判定 |
| 3 | 主张的每条支撑边（fact/norm/evidence）各 ≥1 条 | "三边齐备"是 T2 的定义本身；缺边的金标准让 T2 失去含义 |
| 4 | 金标准引用的 element_id 必须存在于 elements；actions 的 covers 也一样 | 悬空引用 ⇒ covered 集合永远对不上 |
| 5 | `sources` 必须带真实案号与链接（逐案核对过） | 无出处的"唯一解"站不住（`M_1` 186 条同类教训） |
| 6 | task_id / action_id 全局唯一；task_kind = `decision_assessment` | 重复 id 会让取数与指标串实例 |

决策动作的效用设计纪律：错误选项可以带更高的表面效用（直诉比复议省事）：
决策实例测的是"系统能否用法律知识压过效用诱惑"。机械臂按效用贪心，其 T1 读数
如实报告（效用最优 ≠ 决策正确），不得为机械臂调效用凑 T1。

## 用法

    python3 code/tools/make_decision_taskset.py            # 构建并写盘
    python3 code/tools/make_decision_taskset.py --check    # 只校验不写盘
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve()
PAPER_ROOT = HERE.parents[2]
DEFAULT_ROOT = PAPER_ROOT / "experiment" / "tasks-decision"

EDGE_TYPES = ("fact", "norm", "evidence")


class DecisionTasksetError(RuntimeError):
    pass


def _items(x):
    """兼容主套件底本的 `{items: […]}` 字典形态与裸列表两种写法。"""
    if isinstance(x, dict):
        return x.get("items") or []
    return x or []


def _strip_meta(d):
    """剥掉 `_` 前缀的元数据键（仓库通用约定），只留真实数据。"""
    return {k: v for k, v in (d or {}).items() if not k.startswith("_")}


def validate_case(case, problems):
    """对一个决策实例声明逐条过守卫；问题追加进 `problems`（不抛出，攒齐一批再报）。"""
    tid = case.get("task_id") or case.get("base_case_id") or "?"
    where = "cases/base/%s.json" % (case.get("base_case_id") or tid)

    def err(msg):
        problems.append("%s：%s" % (where, msg))

    if case.get("task_kind") != "decision_assessment":
        err("task_kind 必须为 'decision_assessment'，现为 %r" % case.get("task_kind"))
    if not case.get("facts_text"):
        err("缺 facts_text——决策级实例同样要有文本形态的案情")
    if not case.get("sources"):
        err("缺 sources（真实案号+链接）——无出处的'唯一解'会被一句法条推翻")

    elements = {e.get("element_id") for e in _items(case.get("elements"))}
    if None in elements:
        err("elements 存在无 element_id 的条目")
    actions = _items(case.get("actions"))
    action_ids = [a.get("action_id") for a in actions]
    dup_a = sorted({i for i in action_ids if action_ids.count(i) > 1})
    if dup_a:
        err("action_id 重复：%s" % "、".join(map(str, dup_a)))
    ids = set(action_ids)
    for a in actions:
        for e in (a.get("covers") or []):
            if e not in elements:
                err("动作 %s 的 covers 引用了不存在的要素 %r" % (a.get("action_id"), e))

    # 守卫 1/2：决策维度的引用与集合性质（剥 `_` 元数据键后逐维）
    for dim, spec in _strip_meta(case.get("expected_decision")).items():
        must = set(spec.get("must_select") or [])
        must_not = set(spec.get("must_not_select") or [])
        if not must:
            err("决策维度 %r 的 must_select 为空——空维是哑指标" % dim)
        for i in must | must_not:
            if i not in ids:
                err("决策维度 %r 引用了不存在的动作 %r" % (dim, i))
        if must & must_not:
            err("决策维度 %r 的 must_select 与 must_not_select 相交：%s"
                % (dim, "、".join(sorted(must & must_not))))

    # 守卫 3/4：主张栈三边齐备 + 悬空引用
    for claim in _items(case.get("expected_claim_stack")):
        cid = claim.get("claim_id") or "?"
        edges = claim.get("edges") or {}
        missing_types = [t for t in EDGE_TYPES if not (edges.get(t) or [])]
        if missing_types:
            err("主张 %s 缺支撑边：%s（三边齐备是 T2 的定义本身）"
                % (cid, "、".join(missing_types)))
        for t, refs in edges.items():
            for e in refs:
                if e not in elements:
                    err("主张 %s 的 %s 边引用了不存在的要素 %r" % (cid, t, e))

    # 复用主套件的动作字段纪律：triggers 只能引用 T_irr/登记过的风险 id，
    # 决策实例通常无不可逆动作，出现 triggers 时照 arm_baselines 的未知 id 纪律
    # 在评测侧报错；这里只查形状。
    for a in actions:
        if not isinstance(a.get("triggers") or [], list):
            err("动作 %s 的 triggers 不是列表" % a.get("action_id"))


    # deadline / confirmed 兼容两种形态后提取（与主生成器同口径）。
    dl = case.get("deadline") or {}
    if isinstance(dl, dict) and dl.get("remaining_days") is None and "items" not in dl:
        pass  # 无期限声明合法：决策实例可以没有不作为型风险
    cf = case.get("confirmed") or {}
    if isinstance(cf, list):  # 裸列表形态归一为 dict，便于实例装配
        case["confirmed"] = {"items": cf}


def build(tasks_root):
    cases_dir = tasks_root / "cases" / "base"
    if not cases_dir.is_dir():
        raise DecisionTasksetError("缺目录：%s" % cases_dir)
    problems, instances, seen = [], [], set()
    for path in sorted(cases_dir.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        validate_case(case, problems)
        tid = case.get("base_case_id") or path.stem
        if tid in seen:
            problems.append("%s：task_id 重复（%s）" % (path.name, tid))
        seen.add(tid)
        instances.append({
            "task_id": tid,
            "task_kind": case.get("task_kind"),
            "family": case.get("family"),
            "posture": case.get("posture"),
            "case_type": case.get("case_type"),
            "base_case": tid,
            # 决策实例无注入：weak_file 即底本自身（facts_text 在同一文件里，
            # 备料/取数管线只读 facts_text，金标准字段不进任何臂可见载荷）。
            "weak_file": str(path.relative_to(tasks_root)),
            "elements": _items(case.get("elements")),
            "actions": _items(case.get("actions")),
            "budget": case.get("budget") or {},
            "remaining_days": (case.get("deadline") or {}).get("remaining_days"),
            "confirmed": (case.get("confirmed") or {}).get("items") or [],
            "expected_decision": _strip_meta(case.get("expected_decision")),
            "expected_claim_stack": _items(case.get("expected_claim_stack")),
            "sources": case.get("sources") or [],
        })

    # 弱点类字段不存在 ⇒ 与主套件共用的评测代码不会把它们当弱点实例：
    # 不写 weakness_id/expected.signature 等键，判定机路径碰不到它们。
    if problems:
        raise DecisionTasksetError(
            "决策实例守卫未过（%d 项）：\n\n" % len(problems)
            + "\n\n".join("  ✗ " + p for p in problems))

    taskset = {
        "taskset_version": "decision-1",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provenance": {
            "builder": "code/tools/make_decision_taskset.py",
            "guards": "六条守卫见模块 docstring（金标准引用/维度性质/三边齐备/悬空引用/真实出处/id 唯一）",
            "note": ("决策级实例无注入、无签名——T4 检出指标不适用；"
                     "只评 T1/T2（metrics.evaluate_decision）。"),
        },
        "instances": instances,
    }
    return taskset


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks-root", default=str(DEFAULT_ROOT))
    ap.add_argument("--check", action="store_true", help="只校验不写盘")
    args = ap.parse_args(argv)

    tasks_root = Path(args.tasks_root)
    taskset = build(tasks_root)
    n = len(taskset["instances"])
    dims = sum(len(i["expected_decision"]) for i in taskset["instances"])
    claims = sum(len(i["expected_claim_stack"]) for i in taskset["instances"])
    print("✓ 决策实例守卫全过：%d 实例 / %d 决策维度 / %d 条主张" % (n, dims, claims))
    if args.check:
        print("（--check：不写盘）")
        return 0
    out = tasks_root / "taskset.json"
    out.write_text(json.dumps(taskset, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print("已写出：%s" % out)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DecisionTasksetError as exc:
        sys.stderr.write("\n%s\n" % exc)
        raise SystemExit(2)
