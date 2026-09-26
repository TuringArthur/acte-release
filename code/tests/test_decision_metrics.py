#!/usr/bin/env python3
"""决策级指标（T1/T2）的回归测试。

钉住 `ctd_acte.metrics.evaluate_decision` 的口径：

1. T1 维度计分：must_select 全选中且 must_not_select 全未选才得 1，
   单独选中 must、或同时选中 must 与 must_not，都不得分；
2. T2 三边齐备：主张的三条支撑边要求的全部要件都被覆盖才得 1；
3. no_plan 不进分母（"没跑"≠"做错"），显式记 `not_evaluable`；
4. 无决策声明的实例不进指标（主任务集混入本函数时安全返回空块）。

    python3 code/tests/test_decision_metrics.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))  # code/

from ctd_acte import metrics as metrics_mod  # noqa: E402

FAILS = []


def ok(cond, msg):
    if cond:
        print("  ✓ %s" % msg)
    else:
        print("  ✗ %s" % msg)
        FAILS.append(msg)


TS = {"instances": [
    {"task_id": "D1",
     "expected_decision": {"route": {"must_select": ["a2"],
                                     "must_not_select": ["a1", "a3"]}}},
    {"task_id": "D2",
     "expected_claim_stack": [
         {"claim_id": "c1", "edges": {"fact": ["e1"], "norm": ["e2"],
                                      "evidence": ["e3"]}},
         {"claim_id": "c2", "edges": {"fact": ["e4"], "norm": ["e2"],
                                      "evidence": ["e5"]}},
     ]},
    {"task_id": "PLAIN"},  # 无决策声明：不得进指标
]}


def main():
    print("[A] T1 维度计分")
    r = metrics_mod.evaluate_decision(TS, {"D1": {"selected_ids": ["a2"],
                                                  "covered_elements": []}})
    ok(r["T1"]["per_dimension"]["route"]["correct"] == 1, "全选 must 且不碰 must_not ⇒ 维度得分")
    r = metrics_mod.evaluate_decision(TS, {"D1": {"selected_ids": ["a2", "a1"],
                                                  "covered_elements": []}})
    ok(r["T1"]["per_dimension"]["route"]["correct"] == 0,
       "同时选中 must 与 must_not ⇒ 不得分（对错不可并存）")
    r = metrics_mod.evaluate_decision(TS, {"D1": {"selected_ids": [],
                                                  "covered_elements": []}})
    ok(r["T1"]["per_dimension"]["route"]["correct"] == 0, "漏选 must ⇒ 不得分")

    print("[B] T2 三边齐备")
    r = metrics_mod.evaluate_decision(TS, {"D2": {"selected_ids": [],
                                                  "covered_elements": ["e1", "e2", "e3", "e4", "e5"]}})
    ok(r["T2"]["claim_completeness"] == 1.0 and r["T2"]["n_claims"] == 2,
       "两条主张三边全覆盖 ⇒ 1.0")
    r = metrics_mod.evaluate_decision(TS, {"D2": {"selected_ids": [],
                                                  "covered_elements": ["e1", "e2", "e3"]}})
    ok(abs(r["T2"]["claim_completeness"] - 0.5) < 1e-9, "只齐一条主张 ⇒ 0.5")

    print("[C] no_plan 不进分母")
    r = metrics_mod.evaluate_decision(TS, {})
    ok(r["T1"]["n_not_evaluable"] == 1 and r["T1"]["n_evaluable"] == 0,
       "缺计划记 not_evaluable，不进分母")
    ok(r["T2"]["n_not_evaluable"] == 1, "T2 同口径")
    ok(r["T1"]["instance_accuracy"] is None, "无可评实例 ⇒ None（不是 0）")

    print("[D] 无决策声明的实例不进指标")
    r = metrics_mod.evaluate_decision({"instances": [{"task_id": "PLAIN"}]}, {})
    ok(r["n_instances"] == 0 and r["T1"]["n_instances_declared"] == 0,
       "主任务集实例混入 ⇒ 空块，不报错")

    if FAILS:
        print("\n✗ %d 条断言失败" % len(FAILS))
        return 1
    print("\n✓ 全部断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
