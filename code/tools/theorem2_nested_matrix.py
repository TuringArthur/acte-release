#!/usr/bin/env python3
"""定理 2 实验级嵌套矩阵（P4）：fixed-posture 两配置的逐实例双层嵌套读数。

把 任务设计文档 §4.2-7 的判据升级为可复算的读数。
固定 `Obj`（utility）、只变 `ε`（strict=稳 / loose=狠），逐实例给两层读数：

| 层 | 比什么 | 判据 | 违例处置 |
|---|---|---|---|
| 盾允许层 | `Shield_loose` 的 `allowed` vs `Shield_strict`（配额 1 vs 0） | 稳 ⊆ 狠（定理 2(b)(c) 的直接对象；`posture.assert_nested` 同层） | `crossing` / `b_subset_a` / `disjoint` ⇒ 硬失败 |
| 选出层 | 策略层在盾内选中的动作集 | 不设失败判据，盾拿掉冒险动作后策略层改选替代动作，选出集出现交叉是替代效应，恰是定理 2(a) 预言的退化形态 | 只报读数 |
| 值单调 | `E[U] 稳 ≤ E[U] 狠`（定理 2(a)） | 可行集收缩 ⇒ 最优值不增 | 违例 ⇒ 硬失败 |

## 为什么选出层的交叉不是错误

首版把"反向交叉即实现有误"用在选出集上，结果 4 条 M3 实例全报违例，
逐条打开后是替代效应：稳的盾拿掉冒险动作 `adm-a7`（配额 0 不接受不可逆动作），
策略层改选 `adm-a2`；狠的盾放行 `adm-a7`，策略层选它。于是 `{a2} × {a7}` 看似
"交叉"，但盾的允许集是严格嵌套的（稳排除 3 个、狠排除 2 个），且值单调成立
（10 ≤ 15，正是"稳在需冒险实例上必然更差"的预言）。

若按选出集判"交叉即错误"，会把理论预言的退化当实现错误抓。故判据的作用面
只能是盾允许层，与 `posture.assert_nested`（单元级）保持同一层。该口径修正
按纪律记入 `口径变更记账`。

读数释义（写论文时照此口径）：

* 盾允许层 `equal` 是正常退化，不是缺陷，`diagnose_nesting` 早有说明：无
  `T_irr` 触发时两姿态的盾相同（配额根本没被用到），差异只在有触发的实例上显现。
  矩阵单列退化计数，"效应面窄"如实报告，不得拿"均值变化不大"论证"姿态不重要"。
* 选出层的交叉条目要与值单调一起读：交叉 + 值下降 = 替代效应（预言形态）。
* 本矩阵只对机械臂成立（策略层精确求解、盾是纯函数）。真实系统臂的计划由
  模型生成，不保证最优，不得套用本矩阵的失败判据。

用法（在 papers/F1-adjudication/ 下）：

    python3 code/tools/theorem2_nested_matrix.py
    python3 code/tools/theorem2_nested_matrix.py \\
        --strict 实验结果目录 实验结果目录 experiment/results/（随实验材料发布）（随实验材料发布）abl-fixed-posture-strict.json \\
        --loose  实验结果目录 实验结果目录 experiment/results/（随实验材料发布）（随实验材料发布）abl-fixed-posture-loose.json \\
        --out    实验结果目录 实验结果目录 experiment/results/（随实验材料发布）（随实验材料发布）p4/theorem2-nested-matrix.json

（产物放 `results/p4/` 子目录：`[E]` 新鲜度检查只扫 results 顶层、且要求每份都是
带 `taskset.n_instances` 的臂结果文件，本文件是派生分析产物，不属该检查域。）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
CODE_ROOT = HERE.parents[1]              # papers/F1-adjudication/code/
PAPER_ROOT = HERE.parents[2]
sys.path.insert(0, str(CODE_ROOT))

from ctd_acte import posture as posture_mod  # noqa: E402
from ctd_acte import strategy as strategy_mod  # noqa: E402
from ctd_acte.t_irr import TIrR  # noqa: E402

# 与 harness.py 保持一致：可逆风险登记表是已知事实，不是消融参数；
# 触发 id 既不在 T_irr 也不在登记表 ⇒ 报错而非静默忽略（漏防即事故）。
REVERSIBLE_RISK_IDS = ("W-ESC-03",)


class MatrixError(RuntimeError):
    pass


def _load_plan_rows(path):
    """读一份 fixed-posture 消融结果 → ({task_id: 计划行}, settings)。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    systems = data.get("systems") or {}
    if "acte" not in systems:
        raise MatrixError("%s 里没有机械臂 'acte' 的读数——本矩阵只对机械臂成立" % path)
    m = systems["acte"]
    rows = {}
    for row in (m.get("plan") or {}).get("per_instance") or []:
        rows[row["task_id"]] = row
    return rows, (m.get("settings") or {})


def shield_matrix(taskset, tirr):
    """盾允许层：逐实例算配额 0（稳）与配额 1（狠）的盾并比较 `allowed`。

    与 `harness.arm_acte` 的盾调用逐参数同形（含 t_irr / 可逆风险的触发拆分），
    保证矩阵读数与消融结果同一口径。
    """
    q_strict = posture_mod.get("稳").quota
    q_loose = posture_mod.get("狠").quota
    known_t_irr = {i.gate_id for i in tirr.items}
    rev_ids = set(REVERSIBLE_RISK_IDS)

    out = []
    for inst in taskset["instances"]:
        tid = inst["task_id"]
        actions = strategy_mod.actions_from(inst.get("actions"))
        all_ids = [a.action_id for a in actions]
        trigger_map = {a.action_id: list(a.triggers) for a in actions}

        t_irr_triggered = []
        for g in strategy_mod.triggered_from(actions):
            if g in known_t_irr:
                t_irr_triggered.append(g)
            elif g in rev_ids:
                continue
            else:
                raise MatrixError(
                    "实例 %s 触发了未登记的风险 id：%r——报错而非静默忽略（与盾同纪律）"
                    % (tid, g))

        kw = dict(actions=all_ids, triggered=t_irr_triggered,
                  remaining=inst.get("remaining_days"),
                  trigger_map=trigger_map if t_irr_triggered else None,
                  confirmed=inst.get("confirmed") or ())
        s_allowed = sorted(tirr.shield(quota=q_strict, **kw)["allowed"])
        l_allowed = sorted(tirr.shield(quota=q_loose, **kw)["allowed"])
        cmp = posture_mod.compare_shields({"allowed": s_allowed}, {"allowed": l_allowed})
        out.append({
            "task_id": tid, "family": inst.get("family"),
            "is_t_irr": bool(inst.get("is_t_irr")),
            "t_irr_triggered": sorted(t_irr_triggered),
            "allowed_strict": s_allowed, "allowed_loose": l_allowed,
            "relation": cmp["relation"],
            "only_strict": cmp["only_a"], "only_loose": cmp["only_b"],
        })
    return out


def build_matrix(strict_path, loose_path):
    """双层嵌套读数 + 汇总。硬判据违例记入 `violations`。"""
    strict_rows, strict_cfg = _load_plan_rows(strict_path)
    loose_rows, loose_cfg = _load_plan_rows(loose_path)

    objs = {r.get("objective") for r in list(strict_rows.values()) + list(loose_rows.values())}
    objs.discard(None)
    if len(objs) != 1:
        raise MatrixError("两份消融的目标不统一（%s）——定理 2 要求固定 Obj" % sorted(objs))
    if strict_cfg.get("posture_override") != "稳" or loose_cfg.get("posture_override") != "狠":
        raise MatrixError(
            "消融设置不符预期：strict 侧 posture_override=%r、loose 侧 %r（应为 稳 / 狠）"
            % (strict_cfg.get("posture_override"), loose_cfg.get("posture_override")))
    if set(strict_rows) != set(loose_rows):
        raise MatrixError("两份消融的实例集合不一致，无法逐实例对照")

    taskset = json.loads(
        (PAPER_ROOT / "experiment/tasks/taskset.json").read_text(encoding="utf-8"))
    tirr = TIrR.load(CODE_ROOT / "data" / "t-irr.json")
    shield_rows = {r["task_id"]: r for r in shield_matrix(taskset, tirr)}

    rows, violations, substitutions = [], [], []
    for task_id in sorted(strict_rows):
        s, l = strict_rows[task_id], loose_rows[task_id]
        sh = shield_rows.get(task_id)
        if sh is None:
            raise MatrixError("消融结果里的 %s 不在任务集中" % task_id)

        s_ids, l_ids = sorted(s.get("selected_ids") or []), sorted(l.get("selected_ids") or [])
        sel_cmp = posture_mod.compare_shields({"allowed": s_ids}, {"allowed": l_ids})

        eu_s, eu_l = s.get("expected_utility"), l.get("expected_utility")
        value_ok = None
        if isinstance(eu_s, (int, float)) and isinstance(eu_l, (int, float)):
            value_ok = eu_s <= eu_l + 1e-9

        row = {
            "task_id": task_id, "family": s.get("family"),
            "t_irr_triggered": sh["t_irr_triggered"],
            "shield_relation": sh["relation"],
            "allowed_strict": sh["allowed_strict"], "allowed_loose": sh["allowed_loose"],
            "selected_relation": sel_cmp["relation"],
            "selected_strict": s_ids, "selected_loose": l_ids,
            "substituted": sel_cmp["only_a"] if sel_cmp["relation"] in
            ("crossing", "b_subset_a", "equal") else [],
            "eu_strict": eu_s, "eu_loose": eu_l, "value_monotone": value_ok,
            "shield_degenerate": sh["relation"] == "equal",
        }
        rows.append(row)

        # 硬判据一：盾允许层嵌套（定理 2(b)(c) 的作用面）
        if sh["relation"] not in ("equal", "a_subset_b"):
            violations.append("2(b)(c) 盾允许层嵌套违例：%s 呈 %s（仅稳放行：%s；仅狠放行：%s）"
                              % (task_id, sh["relation"],
                                 "、".join(sh["only_a"]) or "无",
                                 "、".join(sh["only_b"]) or "无"))
        # 硬判据二：值单调（定理 2(a)）
        if value_ok is False:
            violations.append("2(a) 值单调违例：%s 的 E[U] 稳=%s > 狠=%s"
                              % (task_id, eu_s, eu_l))
        # 读数：选出层交叉 = 替代效应（预言形态，不作违例）
        if sel_cmp["relation"] in ("crossing", "b_subset_a"):
            substitutions.append(task_id)

    summary = {
        "n_instances": len(rows),
        "objective": sorted(objs)[0],
        "shield_nesting": {
            "degenerate_equal": sum(1 for r in rows if r["shield_relation"] == "equal"),
            "strict_subset": sum(1 for r in rows if r["shield_relation"] == "a_subset_b"),
            "violations": sum(1 for r in rows if r["shield_relation"]
                              not in ("equal", "a_subset_b")),
        },
        "selection_substitution_instances": substitutions,
        "value_monotone_checked": sum(1 for r in rows if r["value_monotone"] is not None),
        "value_monotone_violations": sum(1 for r in rows if r["value_monotone"] is False),
    }
    return {"rows": rows, "summary": summary, "violations": violations}


def to_markdown(built):
    rel_zh = {"equal": "=（退化）", "a_subset_b": "⊊（预期）",
              "b_subset_a": "⊋（反向）", "crossing": "交叉", "disjoint": "不交"}
    lines = [
        "### 盾允许层（硬判据：稳 ⊆ 狠）",
        "",
        "| 实例 | 族 | 触发的 T_irr | 盾允许集关系 | E[U] 稳 | E[U] 狠 | 值单调 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in built["rows"]:
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            r["task_id"], r["family"] or "-",
            "、".join(r["t_irr_triggered"]) or "—",
            rel_zh.get(r["shield_relation"], r["shield_relation"]),
            r["eu_strict"], r["eu_loose"],
            {True: "✓", False: "✗", None: "-"}[r["value_monotone"]]))
    s = built["summary"]["shield_nesting"]
    lines += [
        "",
        "汇总：%d 实例——盾允许层退化 %d / 预期嵌套 %d / 违例 %d；"
        "值单调受检 %d、违例 %d。"
        % (built["summary"]["n_instances"], s["degenerate_equal"], s["strict_subset"],
           s["violations"], built["summary"]["value_monotone_checked"],
           built["summary"]["value_monotone_violations"]),
        "",
        "### 选出层（替代效应读数，不设失败判据）",
        "",
    ]
    if built["summary"]["selection_substitution_instances"]:
        lines.append("下列实例在盾收缩后策略层改选替代动作，选出集出现交叉——"
                     "与值单调下降同现时即定理 2(a) 预言的退化形态：")
        lines.append("")
        lines.append("| 实例 | 稳选出 | 狠选出 | 稳独有（替代） | 狠独有（被拦的冒险动作） |")
        lines.append("|---|---|---|---|---|")
        for r in built["rows"]:
            if r["task_id"] in built["summary"]["selection_substitution_instances"]:
                only_s = sorted(set(r["selected_strict"]) - set(r["selected_loose"]))
                only_l = sorted(set(r["selected_loose"]) - set(r["selected_strict"]))
                lines.append("| %s | %s | %s | %s | %s |" % (
                    r["task_id"], "、".join(r["selected_strict"]),
                    "、".join(r["selected_loose"]),
                    "、".join(only_s) or "—", "、".join(only_l) or "—"))
    else:
        lines.append("（无——选出集全部嵌套）")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strict", default=str(PAPER_ROOT / "experiment/results/abl-fixed-posture-strict.json"))
    ap.add_argument("--loose", default=str(PAPER_ROOT / "experiment/results/abl-fixed-posture-loose.json"))
    ap.add_argument("--out", default=str(PAPER_ROOT / "experiment/results/p4/theorem2-nested-matrix.json"))
    args = ap.parse_args(argv)

    built = build_matrix(args.strict, args.loose)
    out = {
        "generated_by": "code/tools/theorem2_nested_matrix.py",
        "criterion": ("experiment/tasks/README.md §4.2-7；判据作用面 = 盾允许层"
                      "（口径修正说明见 note 字段）"),
        "inputs": {"strict": args.strict, "loose": args.loose},
        **built,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(to_markdown(built))
    if built["violations"]:
        print("\n✗ 定理 2 实验级读数出现硬判据违例：", file=sys.stderr)
        for v in built["violations"]:
            print("  " + v, file=sys.stderr)
        return 1
    print("\n✓ 定理 2 实验级嵌套矩阵无违例（盾允许层退化 %d 条如实计；"
          "选出层替代效应 %d 条见上表）"
          % (built["summary"]["shield_nesting"]["degenerate_equal"],
             len(built["summary"]["selection_substitution_instances"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
