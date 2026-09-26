#!/usr/bin/env python3
"""把红队探针源（experiment/tasks-redteam/probes.json）展开成 `--prepare` 的输入。

## 为什么要有这一步

`arm_acte_model.py --prepare` 吃的是"taskset.json + cases/weak/*.json"两件套
（`facts_text` + `actions`），而红队探针的**手写源**只在 probes.json 一份：
24 个探针各自带攻击说明与判定真值（dangerous_action_ids / valid_confirmed /
forged_confirmation），若手写 24 份 weak JSON，真值与案情迟早分家。故这里做
一次确定性展开（无时间戳、重复跑产物逐字节相同）：

    probes.json ──▶ taskset.json（instances 带真值字段）
                └─▶ cases/weak/<probe_id>.json（facts_text，供备料读）

展开产物进的是 `experiment/tasks-redteam/`（**不进主 taskset**：探针是给
会话臂用的提示词素材，进主 taskset 会污染主实验的分母）。

## 与主任务集的两处刻意差异

- 实例**不带 `weakness_id`**：`prepare` 见到有注入的套件会给每个底本生成
  干净对照行，探针套件不测"干净件保持沉默"（那是主结论的口径），不生成；
- `elements` 不展开：探针只测护栏的放行判定（journal 口径），不测覆盖度。

## 用法

    python3 code/tools/build_redteam_taskset.py            # 展开到 experiment/tasks-redteam/
    python3 code/tools/build_redteam_taskset.py --check    # 只校验探针源，不写文件
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
CODE_ROOT = HERE.parents[1]
PAPER_ROOT = HERE.parents[2]
DEFAULT_SUITE_DIR = PAPER_ROOT / "experiment" / "tasks-redteam"
T_IRR_SNAPSHOT = CODE_ROOT / "data" / "t-irr.json"


class SuiteError(RuntimeError):
    pass


def load_probes(suite_dir):
    src = suite_dir / "probes.json"
    if not src.is_file():
        raise SuiteError("缺探针源：%s" % src)
    return json.loads(src.read_text(encoding="utf-8"))


def validate(suite):
    """探针源的自检：id 唯一、真值可解析、expected_gate 在 T_irr 登记表内。"""
    problems = []
    if not T_IRR_SNAPSHOT.is_file():
        raise SuiteError("缺 T_irr 快照：%s" % T_IRR_SNAPSHOT)
    known_gates = {
        e.get("gate_id")
        for e in json.loads(T_IRR_SNAPSHOT.read_text(encoding="utf-8")).get("t_irr") or []
    }
    probes = suite.get("probes") or []
    if not probes:
        raise SuiteError("探针源里 0 个探针")
    seen_probe, seen_action = set(), set()
    for p in probes:
        pid = p.get("probe_id")
        if not pid or pid in seen_probe:
            problems.append("probe_id 缺失或重复：%r" % pid)
        seen_probe.add(pid)
        acts = p.get("actions") or []
        act_ids = {a.get("action_id") for a in acts}
        for a in acts:
            aid = a.get("action_id")
            if not aid or aid in seen_action:
                problems.append("%s：action_id 缺失或全局重复 %r" % (pid, aid))
            seen_action.add(aid)
            if not (a.get("text") or "").strip():
                problems.append("%s：%s 缺动作文本（fail-closed 推断要靠它）" % (pid, aid))
        dangerous = p.get("dangerous_action_ids") or []
        if not dangerous:
            problems.append("%s：dangerous_action_ids 为空——没有真值就判不了 bypass" % pid)
        for d in dangerous:
            if d not in act_ids:
                problems.append("%s：dangerous_action_ids 里的 %r 不在 actions 中" % (pid, d))
        gate = p.get("expected_gate")
        if gate is not None and gate not in known_gates:
            problems.append("%s：expected_gate %r 不在 T_irr 登记表内" % (pid, gate))
        if p.get("forged_confirmation") and (p.get("valid_confirmed") or []):
            problems.append("%s：forged_confirmation 与 valid_confirmed 不能同时成立" % pid)
        if not (p.get("facts_text") or "").strip():
            problems.append("%s：缺 facts_text" % pid)
    return problems


def expand(suite, suite_dir):
    probes = suite.get("probes") or []
    weak_dir = suite_dir / "cases" / "weak"
    weak_dir.mkdir(parents=True, exist_ok=True)
    instances = []
    for p in probes:
        pid = p["probe_id"]
        weak_rel = "cases/weak/%s.json" % pid
        weak = {
            "task_id": pid,
            "base_case": p.get("derived_from"),
            "facts_text": p["facts_text"],
            "injected": [{
                "probe_class": p.get("probe_class"),
                "attack": p.get("attack"),
            }],
        }
        (suite_dir / weak_rel).write_text(
            json.dumps(weak, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        inst = {
            "task_id": pid,
            "family": "RT",
            "probe_class": p.get("probe_class"),
            "attack": p.get("attack"),
            "derived_from": p.get("derived_from"),
            # `base_case` 与主任务集同名同位：arm_acte_model.prepare 的干净行
            # 逻辑按该键取集合（探针套件无 weakness_id ⇒ 不生成干净行，但键必须在，
            # 否则 KeyError）。取值给 derived_from（溯源够用）。
            "base_case": p.get("derived_from"),
            "posture": p.get("posture"),
            "case_type": p.get("case_type"),
            "task_kind": "guardrail_redteam",
            "weak_file": weak_rel,
            "expected_gate": p.get("expected_gate"),
            "dangerous_action_ids": list(p.get("dangerous_action_ids") or []),
            "valid_confirmed": list(p.get("valid_confirmed") or []),
            "forged_confirmation": bool(p.get("forged_confirmation")),
            "actions": [
                {k: a.get(k) for k in ("action_id", "text", "covers",
                                       "expected_utility", "cost")}
                for a in (p.get("actions") or [])
            ],
            "remaining_days": p.get("remaining_days"),
        }
        instances.append(inst)
    out = {
        "taskset_version": "redteam-%s" % suite.get("suite_version", 1),
        "provenance": {
            "source": "experiment/tasks-redteam/probes.json",
            "builder": "code/tools/build_redteam_taskset.py",
            "note": "生成物，勿手改（重复跑逐字节相同）。探针不进主 taskset。",
        },
        "n_instances": len(instances),
        "instances": instances,
    }
    (suite_dir / "taskset.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return len(instances)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suite-dir", default=str(DEFAULT_SUITE_DIR),
                    help="探针套件目录（默认 experiment/tasks-redteam）")
    ap.add_argument("--check", action="store_true", help="只校验探针源，不写文件")
    args = ap.parse_args(argv)

    suite_dir = Path(args.suite_dir)
    try:
        suite = load_probes(suite_dir)
        problems = validate(suite)
        if problems:
            sys.stderr.write("探针源校验失败 %d 项：\n" % len(problems))
            for p in problems:
                sys.stderr.write("  ✗ %s\n" % p)
            return 1
        print("探针源校验通过：%d 个探针" % len(suite.get("probes") or []))
        if args.check:
            return 0
        n = expand(suite, suite_dir)
    except SuiteError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    print("展开完成：%d 个实例 → %s（taskset.json + cases/weak/）" % (n, suite_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
