#!/usr/bin/env python3
"""决策套件评测运行器：T1 立案正确率 / T2 主张栈完整率。

## 为什么不直接用 harness

harness 的主评测链走注入签名（`expected.signature`），决策级实例无注入，
T4 检出指标对它们不适用（`tasks-decision/taskset.json` 的 provenance 已声明）。
本工具只取计划评 T1/T2：机械臂进程内现算（`harness.arm_acte`），
真实系统臂读会话产物（`arm_acte_model.serve`，计划口径与 harness 同一：
提议 ∩ 盾放行）。基线无计划 ⇒ T1/T2 不适用（metrics 侧显式记，不是 0）。

## 用法（在 papers/F1-adjudication/ 下）

    # 机械臂（现算，无需会话）：
    python3 code/tools/eval_decision.py --mechanical \\
        --out 实验结果目录 实验结果目录 experiment/results/（随实验材料发布）（随实验材料发布）decision/decision-mechanical.json
    # 真实臂（先跑会话：--prepare → run-acte-sessions.sh）：
    python3 code/tools/eval_decision.py --real \\
        --run-dirs experiment/run/acte-sessions-decision \\
        --out 实验结果目录 实验结果目录 experiment/results/（随实验材料发布）（随实验材料发布）decision/acte-model-decision.json

    # 备料（转交给会话驱动）：
    python3 code/tools/arm_acte_model.py --prepare --taskset experiment/tasks-decision \\
        --run-dir experiment/run/acte-sessions-decision
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve()
CODE_ROOT = HERE.parents[1]              # papers/F1-adjudication/code/
PAPER_ROOT = HERE.parents[2]
sys.path.insert(0, str(CODE_ROOT))

from ctd_acte import metrics as metrics_mod  # noqa: E402
from ctd_acte.t_irr import TIrR  # noqa: E402

DEFAULT_TASKSET = PAPER_ROOT / "experiment" / "tasks-decision" / "taskset.json"


class DecisionEvalError(RuntimeError):
    pass


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_taskset(path):
    ts = json.loads(Path(path).read_text(encoding="utf-8"))
    if not ts.get("instances"):
        raise DecisionEvalError("决策任务集为 0 实例：%s" % path)
    return ts


def taskset_payload(inst, tasks_root):
    """备料同构的载荷（serve 只需要 facts_text/actions/elements/…）。"""
    weak = json.loads((tasks_root / inst["weak_file"]).read_text(encoding="utf-8"))
    return {
        "arm": "acte-model",
        "task_id": inst["task_id"],
        "facts_text": weak["facts_text"],
        "posture": inst.get("posture"),
        "actions": inst.get("actions") or [],
        "elements": inst.get("elements") or [],
        "remaining_days": inst.get("remaining_days"),
    }


def mechanical_plans(taskset):
    harness = _load_module(CODE_ROOT / "harness.py", "f1_harness_for_decision")
    tirr = TIrR.load(CODE_ROOT / "data" / "t-irr.json")
    bundle = harness.arm_acte(taskset, tirr)
    return {tid: v.get("plan") for tid, v in bundle.items()}


def real_plans(taskset, run_dirs):
    arm = _load_module(CODE_ROOT / "tools" / "arm_acte_model.py", "f1_arm_for_decision")
    tasks_root = Path(DEFAULT_TASKSET).parent
    plans, per_run = {}, []
    for run_idx, rd in enumerate(run_dirs):
        run_dir = Path(rd)
        if not run_dir.is_dir():
            raise DecisionEvalError("会话产物目录不存在：%s" % run_dir)
        for inst in taskset["instances"]:
            payload = taskset_payload(inst, tasks_root)
            out = arm.serve(payload, run_dir)
            plan = out.get("plan")
            if plan is not None:
                plan["source_run"] = str(rd)
            plans.setdefault(inst["task_id"], plan)
            per_run.append({"run": run_idx + 1, "task_id": inst["task_id"],
                            "has_plan": plan is not None})
    return plans, per_run


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--taskset", default=str(DEFAULT_TASKSET))
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mechanical", action="store_true")
    mode.add_argument("--real", action="store_true")
    ap.add_argument("--run-dirs", default=None,
                    help="真实臂会话产物目录，逗号分隔（k>1 时逐 run 取，先到先得）")
    ap.add_argument("--note", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    taskset = load_taskset(args.taskset)
    if args.mechanical:
        plans = mechanical_plans(taskset)
        arm_note = "mechanical (arm_acte, 效用贪心——决策实例的错误选项可带更高表面效用)"
    else:
        if not args.run_dirs:
            raise DecisionEvalError("--real 需要 --run-dirs")
        plans, per_run = real_plans(taskset, [d.strip() for d in args.run_dirs.split(",") if d.strip()])
        arm_note = "acte-model (serve：提议 ∩ 盾放行，与 harness 口径 A 同一)"

    block = metrics_mod.evaluate_decision(taskset, plans)
    out = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "taskset": {"path": str(args.taskset), "n_instances": len(taskset["instances"])},
        "arm": arm_note,
        "operator_note": args.note,
        "plans": plans,
        "decision": block,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    t1, t2 = block["T1"], block["T2"]
    print("T1 立案正确率：实例 %s（受检 %d / 不可评 %d）；维度 %s"
          % (t1["instance_accuracy"], t1["n_evaluable"], t1["n_not_evaluable"],
             json.dumps(t1["dimension_accuracy"], ensure_ascii=False)))
    for d, s in sorted(t1["per_dimension"].items()):
        print("   维度 %-12s %s（%d/%d）" % (d, s["rate"], s["correct"], s["n"]))
    print("T2 主张栈完整率：主张级 %s（%d/%d）；实例级 %s"
          % (t2["claim_completeness"], t2["n_complete_claims"], t2["n_claims"],
             t2["instance_completeness"]))
    for r in block["per_instance_t1"]:
        if r.get("not_evaluable"):
            print("   T1 %s：不可评（%s）" % (r["task_id"], r["not_evaluable"]))
        else:
            print("   T1 %s：%s" % (r["task_id"],
                                    json.dumps(r["dimensions"], ensure_ascii=False)))
    print("已写出：%s" % out_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DecisionEvalError as exc:
        sys.stderr.write("\n%s\n" % exc)
        raise SystemExit(2)
