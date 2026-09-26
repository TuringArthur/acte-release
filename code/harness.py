#!/usr/bin/env python3
"""评测入口：跑系统 → 判弱点注入签名 → 出指标 → 落盘带溯源的结果 JSON。

## "夹具臂"

评测要三样东西：任务集、判定机、被测系统。前两样已就位，第三样（真模型臂）
要么调 API（需凭据、有采样波动）、要么等 DSH 插件包做好。

故本 harness 先用一个可确定复现的方式把整条度量链跑通：内置两个夹具臂

| 臂 | 行为 | 用途 |
|---|---|---|
| `oracle` | 读任务集，对每个实例报出恰好命中签名的引文 | 上界标定：指标应达 100% 检出、0 误报。达不到说明度量链有 bug |
| `null` | 什么都不报 | 下界标定：检出 0%、误报 0%、升级事故 100%。非零说明判定机在"没报"时也授信了 |

两者都不是基线，不得当作实验结果使用，它们的作用是把"度量链本身是否
正确"变成一个可跑的检查。没有这一步，等真模型跑出数字时，
你无法区分"系统弱"与"度量错了"，缺陷码等值判定正是这个亏的来源
（基线用自由文本码、内部用登记码，两者永不相等，基线检出率恒为 0.000，
看着像基线弱，其实是口径错）。

真模型臂按 `--arm-module <路径>` 接入（与共享基线管线同一机制）：模块导出 `run(payload) -> {"findings": [...]}`，形状见
`ctd_acte/metrics.py` 的 `Finding`。

## 用法

    # 夹具标定（确定性、不联网）
    python3 code/harness.py --taskset experiment/tasks/taskset.json --systems oracle,null

    # 接入外部臂（路径由操作者给出，本目录不写死相对引用）
    python3 code/harness.py --systems oracle,b1 --arm-module <path>/arms.py

    # 判定机路径同样由操作者给出（独立导出时带自己的钉版副本）
    python3 code/harness.py --matcher-module <path>/signature.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve()
# 本文件在 `code/` 下（比 `code/tools/x.py` 少一级），故 parents 下标与工具类
#   文件不同。已有一次踩点：照抄工具类的 `parents[2]/parents[4]` 使结果被写到
#   实验材料目录的上一级（少了一级篇目录），且不报错。
#   故这里用语义明确的 `.parent` 链，并断言目录布局，布局一变即失败，
#   而不是继续往错的地方写结果。
CODE_ROOT = HERE.parent
PAPER_ROOT = HERE.parent.parent
if CODE_ROOT.name != "code" or (PAPER_ROOT / "code").resolve() != CODE_ROOT:
    raise RuntimeError(
        "目录布局与预期不符：本文件应位于 <篇目录>/code/ 下，实际 %s" % HERE)
sys.path.insert(0, str(CODE_ROOT))


# 判定机路径的三档解析在 `code/tools/matcher_path.py`（环境变量 → 本仓钉版副本 →
# 上层仓库内的冻结副本）。解析器单独成模块，因为三个测试套件也要用它；四处各写一份
# 迟早会漂移。默认值在下方 `load_module_by_path` 定义之后计算。

from ctd_acte import metrics as metrics_mod  # noqa: E402
from ctd_acte import posture as posture_mod  # noqa: E402
from ctd_acte import strategy as strategy_mod  # noqa: E402
from ctd_acte.metrics import Finding  # noqa: E402
from ctd_acte.t_irr import TIrR  # noqa: E402

T_IRR_SNAPSHOT = CODE_ROOT / "data" / "t-irr.json"

# 可逆风险 id 集合（供 no-shield-reversible 反向消融；权威在插件侧
# code/plugins/acte-shield.js 的 REVERSIBLE_RISKS，这里取其风险 id 的最小镜像）。
# 两处必须一致：`code/tools/check_plugin_data.py` 的静态核查会检查插件侧
#   含 REVERSIBLE_RISKS 且其中条目不在 T_IRR 里；本常量的 id 由
#   `test_strategy.py` 与插件侧对照（避免两处漂移）。
REVERSIBLE_RISK_IDS = ("W-ESC-03",)

FIXTURE_ARMS = ("oracle", "null")

# 机械臂：盾 + 策略层。不是基线，是本篇方法（ACTE）的机械部分。
# 它不含生成式能力（不写文书、不做自然语言判断），故它产出的
# "弱点检出"读数无意义（它不检弱点）；它产出的是计划质量读数
# （要件覆盖度 / 期望回收 / 升级事故）：那正是策略层与盾所影响的东西。
MECHANICAL_ARM = "acte"


class HarnessError(RuntimeError):
    pass


def load_module_by_path(path, alias):
    if not Path(path).is_file():
        raise HarnessError(
            "模块不存在：%s\n"
            "  判定机与外部臂均按操作者给出的路径加载（本目录不写死相对引用）。"
            % path)
    spec = importlib.util.spec_from_file_location(alias, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 判定机默认路径（三档，见 `code/tools/matcher_path.py`）
matcher_path_mod = load_module_by_path(CODE_ROOT / "tools" / "matcher_path.py",
                                       "f1_matcher_path")
DEFAULT_MATCHER = matcher_path_mod.resolve(CODE_ROOT, HERE)


# ── 夹具臂 ────────────────────────────────────────────────────
def _quoted_window(text, regex):
    """围绕痕迹取一段引文，模拟"定位正确"的系统会怎么引。"""
    m = re.search(regex, text)
    if not m:
        return None
    return text[max(0, m.start() - 10):m.end() + 10]


def arm_oracle(taskset, weak_texts, clean_texts):
    """上界标定：对每个实例报出一条恰好命中 tier2 的 finding。

    引文围绕痕迹取窗口（含完整痕迹），说明里带上断言词表的第一项，
    使 `with_assertion` 也为真，这样也能顺带验证"后果断言率"的分子口径。
    """
    reports = {}
    for inst in taskset["instances"]:
        tid = inst["task_id"]
        sig = inst["expected"]["signature"]
        grounded = next((c for c in sig["clauses"] if c.get("grounded")), None)
        if not grounded:
            reports[tid] = []
            continue
        pats = [t for t in grounded["terms"] if t["kind"] == "pattern"]
        if not pats:
            reports[tid] = []
            continue
        text = weak_texts[tid]
        # 任务集 v3 起痕迹子句可含多项（子句内项间是"与"，如 W-LIM-003 的
        # 原子日期 + 改写措辞）：oracle 必须逐项各取一个窗口拼进引文；
        # 只引第一项会在多痕迹项实例上假漏检（v3 首轮实测：检出 18/22 = 0.8182，
        # 全部红在这一处）。单项时 join 不加连接符，行为与旧版逐字节相同。
        windows = []
        for pat in pats:
            w = _quoted_window(text, pat["regex"])
            if w is None:
                windows = None
                break
            windows.append(w)
        if windows is None:
            reports[tid] = []
            continue
        loc = "……".join(windows)
        assertion_alt = sig["clauses"][-1]["terms"][0]["alts"][0]
        reports[tid] = [Finding(code="oracle_issue",
                                evidence=loc,
                                locus=inst.get("weakness_class"),
                                message=assertion_alt,
                                defect_layer="validity")]
    return reports


def arm_null(taskset, weak_texts, clean_texts):
    """下界标定：什么也不报。"""
    return {i["task_id"]: [] for i in taskset["instances"]}


def run_fixture_arm(name, taskset, weak_texts, clean_texts):
    """夹具臂。返回 `(weak_reports, clean_reports, plans)` ，与外部臂同形，
    使调用点不必区分是夹具还是外部臂（`plans` 对夹具臂为空）。"""
    if name == "oracle":
        return arm_oracle(taskset, weak_texts, clean_texts), {}, {}
    if name == "null":
        return arm_null(taskset, weak_texts, clean_texts), {}, {}
    raise HarnessError("未知夹具臂：%r（现有 %s）" % (name, "、".join(FIXTURE_ARMS)))


# ── 机械臂：盾 + 策略层（本篇方法）──────────────────────────────
def arm_acte(taskset, tirr, *, posture_override=None, shield_enabled=True,
             include_reversible=False, reversible_ids=None):
    """跑 ACTE 的机械部分：先算盾、再在盾内按姿态选动作。

    ## 代码结构与定理 1(a) 的对应

    本函数刻意分两次调用、且顺序固定：

    1. `tirr.shield(...)` ， 不传姿态。盾只依赖 `⟨A, T_irr, triggered⟩`，
       故同一状态下三个姿态拿到的 `allowed` 完全相同（定理 1(a)）。
    2. `strategy.plan(posture_name, ...)` ， 只传姿态，且只在 `allowed` 内选。

    若把两者合成一个函数（`engine.shielded_plan` 就是那样的便捷 API，
    它还额外带"strict 姿态下不得进入规划阶段"的流水线检查），
    结构上就说不清"哪一部分依赖姿态"，而那正是本篇要主张的东西。

    ## 消融臂的落点

    | 臂 | 开关 | 机制 |
    |---|---|---|
    | 主臂 | 默认 | 盾筛 → 按实例自带姿态选 |
    | `no-shield` | `shield_enabled=False` | 不分两步，策略层在全量候选上选 ⇒ 可能选中跨层级动作 |
    | `fixed-posture` | `posture_override='狠'` | 盾不变（定理 1(a)），选择规则变 ⇒ M1 覆盖度下降、M3 事故上升 |
    | `no-shield-reversible` | `include_reversible=True` | 把可逆风险也拦掉 ⇒ M2 期望回收下降（定理 1′ 的失效方向） |

    :param tirr: `TIrR` 实例（盾的权威来源）。
    :param posture_override: 强制所有实例用同一姿态。
    :param include_reversible: 把 `reversible_ids` 里的可逆风险也纳入拦截。
    :param reversible_ids: 可逆风险的 id 集合。缺省取 `REVERSIBLE_RISK_IDS`
        ，登记表是已知事实，不是消融参数；消融参数是 `include_reversible`
        （要不要拦它）。若这里也默认空集，主臂会把 `W-ESC-03` 当成未知 id 报错。
    :returns: `{task_id: {"findings": [...], "plan": {...}}}`
    """
    out = {}
    for inst in taskset["instances"]:
        tid = inst["task_id"]
        posture_name = posture_override or inst.get("posture")
        actions = strategy_mod.actions_from(inst.get("actions"))
        all_ids = [a.action_id for a in actions]
        max_actions = (inst.get("budget") or {}).get("max_actions")

        # 把触发的风险分成两类，这是本篇的核心区分，不能混：
        #   * `T_irr` 条目（不可逆）→ 交给盾；
        #   * 可逆风险 id → 不进盾（定理 1′：对可逆风险，独立闸门会次优），
        #     只在 `no-shield-reversible` 反向消融下才拦。
        # 未知 id 报错而非忽略：静默忽略等于漏防（与盾的同一条纪律）。
        raw_triggered = strategy_mod.triggered_from(actions)
        rev_ids = set(REVERSIBLE_RISK_IDS if reversible_ids is None else reversible_ids)
        known_t_irr = {i.gate_id for i in tirr.items}
        t_irr_triggered, reversible_triggered = [], []
        for g in raw_triggered:
            if g in known_t_irr:
                t_irr_triggered.append(g)
            elif g in rev_ids:
                reversible_triggered.append(g)
            else:
                raise HarnessError(
                    "实例 %s 的动作声明触发了未登记的风险 id：%r\n"
                    "  既不在 T_irr（%s），也不在可逆风险集合（%s）。"
                    "静默忽略等于漏防，故报错。"
                    % (tid, g, "、".join(sorted(known_t_irr)),
                       "、".join(sorted(rev_ids))))

        notes = []
        if shield_enabled:
            # 第 1 步：算盾（不传姿态 ， 定理 1(a)）
            # trigger_map：动作在事实层面声明触发了哪些条目（盾据此拿掉它们）
            trigger_map = {a.action_id: list(a.triggers) for a in actions}
            # quota 来自姿态（定理 1′(i) 的配额自动机）；confirmed 是案件事实。
            # 二者都是状态而非策略，故盾仍是 (状态, 配额) 的纯函数（定理 1(a)）。
            shielded = tirr.shield(
                actions=all_ids, triggered=t_irr_triggered,
                remaining=inst.get("remaining_days"),
                trigger_map=trigger_map if t_irr_triggered else None,
                quota=posture_mod.get(posture_name).quota,
                confirmed=inst.get("confirmed") or ())
            allowed = list(shielded["allowed"])
            notes = list(shielded["notes"])
            if include_reversible:
                # 反向消融：把可逆风险对应的动作也拿掉（盾默认不拦它们）
                extra_blocked = [a.action_id for a in actions
                                 if any(t in rev_ids for t in a.triggers)]
                allowed = [i for i in allowed if i not in extra_blocked]
                if extra_blocked:
                    notes.append("反向消融：把可逆风险动作也拦掉 %s（定理 1′ 的失效方向）"
                                 % "、".join(extra_blocked))
        else:
            # 去掉盾：不设 allowed 限制，策略层在全量候选上选
            shielded = None
            allowed = all_ids
            notes = ["盾已禁用（no-shield 消融）：策略层在全量候选上选"]

        # 第 2 步：在盾内按姿态选（只传姿态）
        plan = strategy_mod.plan(posture_name, actions, allowed, max_actions)
        plan["shield_enabled"] = shield_enabled
        plan["include_reversible"] = include_reversible
        plan["t_irr_triggered"] = t_irr_triggered
        plan["reversible_triggered"] = reversible_triggered

        out[tid] = {
            "findings": [Finding(code="acte_shield_note", evidence=n[:120], message=n,
                                 defect_layer="validity") for n in notes],
            "plan": plan,
        }
    return out


# ── 外部臂接入 ────────────────────────────────────────────────
def run_external_arm(module, system_name, taskset, weak_texts, clean_texts, tasks_root):
    """外部臂的 `run(payload)` 由操作者提供的模块实现（形状见模块 docstring）。

    外部臂也可以返回 `plan`（若它做了动作选择），从而参与计划质量指标；
    不返回则计划指标记 `None`（不是 0，0 会被误读成"计划很差"）。

    外部臂还可以返回 `notes`（输出侧沉默机制的提示通道，本引擎的臂产出；
    基线不送该补丁故恒缺省）：不参与签名判定，只进误报的 FP(any) 口径
    与 notes 计数，双口径语义见 `metrics.evaluate_with_clean`。

    :returns: `(reports, clean_reports, plans, weak_notes, clean_notes)`
    """
    if not hasattr(module, "run"):
        raise HarnessError("外部臂模块没有 run(payload)：%s" % module)
    reports, clean_reports, plans = {}, {}, {}
    weak_notes, clean_notes = {}, {}
    for inst in taskset["instances"]:
        payload = {
            "arm": system_name,
            "task_id": inst["task_id"],
            "case_type": inst.get("case_type"),
            "task_kind": inst.get("task_kind"),
            "posture": inst.get("posture"),
            "elements": inst.get("elements") or [],
            "actions": inst.get("actions") or [],
            "budget": inst.get("budget") or {},
            "facts_text": weak_texts[inst["task_id"]],
        }
        out = module.run(payload) or {}
        reports[inst["task_id"]] = [Finding.from_dict(f) for f in (out.get("findings") or [])]
        weak_notes[inst["task_id"]] = [Finding.from_dict(f) for f in (out.get("notes") or [])]
        if out.get("plan"):
            plans[inst["task_id"]] = out["plan"]
    for cid, text in clean_texts.items():
        # 盲化：载荷与弱点件同形，不带 is_clean，否则臂（或未来接入的臂）
        # 能直接看出"这是干净件"，"能否在案子是好的时候保持沉默"这条主结论即失效。
        # 干净件身份只存在于 harness 自己的映射里（clean_reports 的键）。
        payload = {"arm": system_name, "task_id": cid, "facts_text": text}
        out = module.run(payload) or {}
        clean_reports[cid] = [Finding.from_dict(f) for f in (out.get("findings") or [])]
        clean_notes[cid] = [Finding.from_dict(f) for f in (out.get("notes") or [])]
    return reports, clean_reports, plans, weak_notes, clean_notes


# ── 主流程 ────────────────────────────────────────────────────
def load_taskset(tasks_root):
    p = Path(tasks_root) / "taskset.json"
    if not p.is_file():
        raise HarnessError("任务集不存在：%s（先跑 tools/make_taskset.py）" % p)
    ts = json.loads(p.read_text(encoding="utf-8"))
    if not ts.get("instances"):
        raise HarnessError("任务集里 0 实例——分母为 0，指标无意义。拒绝跑。")
    return ts, Path(tasks_root)


def load_texts(tasks_root, taskset):
    weak, clean = {}, {}
    for inst in taskset["instances"]:
        weak[inst["task_id"]] = json.loads(
            (Path(tasks_root) / inst["weak_file"]).read_text(encoding="utf-8"))["facts_text"]
        cid = inst["base_case"]
        if cid not in clean:
            clean[cid] = json.loads(
                (Path(tasks_root) / "cases" / "base" / ("%s.json" % cid))
                .read_text(encoding="utf-8"))["facts_text"]
    # v4 起：纯负对照底本（不带任何注入的干净案件）也进干净集——
    # 干净件报出率的分母不能只有"被注入案件的修好版"，还要有
    # "从未被注入过的案件"，否则'干净'与'注入设计'不独立。
    base_dir = Path(tasks_root) / "cases" / "base"
    if base_dir.is_dir():
        for bp in sorted(base_dir.glob("*.json")):
            cid = bp.stem
            if cid not in clean:
                clean[cid] = json.loads(bp.read_text(encoding="utf-8"))["facts_text"]
    return weak, clean


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--taskset", default=str(PAPER_ROOT / "experiment" / "tasks"))
    ap.add_argument("--systems", default="oracle,null",
                    help="逗号分隔的臂名：夹具臂 %s、机械臂 %s，"
                         "其余须配 --arm-module"
                         % ("/".join(FIXTURE_ARMS), MECHANICAL_ARM))
    ap.add_argument("--matcher-module",
                    default=os.environ.get("CTD_LA_MATCHER") or DEFAULT_MATCHER,
                    help="判定机模块路径（独立导出时须自带钉版副本，"
                         "见 code/vendor/PROVENANCE.md）")
    ap.add_argument("--arm-module", default=os.environ.get("CTD_LA_ARM_MODULE"),
                    help="外部臂模块路径（操作者给出）")
    # ── 消融开关（对应 消融配置）──────────
    ap.add_argument("--posture-override", default=os.environ.get("ACTE_POSTURE_OVERRIDE"),
                    choices=["细", "狠", "稳"],
                    help="强制所有实例用同一姿态（fixed-posture 消融）")
    ap.add_argument("--no-shield", action="store_true",
                    help="去掉盾（no-shield 消融）：策略层在全量候选上选")
    ap.add_argument("--include-reversible", action="store_true",
                    help="把可逆风险也纳入拦截（no-shield-reversible 反向消融）")
    ap.add_argument("--conjunction", action="store_true",
                    help="判定侧跨 finding 合取（metrics.judge_instance 的 "
                         "conjunction_across_findings，默认关）：痕迹项与断言项"
                         "允许分处不同 finding。")
    ap.add_argument("--repeats", type=int, default=1,
                    help="k>1 管道：每臂重复 k 次。结果 gains `runs[]`（逐 run 行）"
                         "与 `aggregate`（均值/样本 sd）；**顶层读数 = 第 1 run**"
                         "（k=1 时与旧 schema 同形，老读者不受影响）")
    ap.add_argument("--run-dirs", default=None,
                    help="逗号分隔的 k 个会话产物目录（外部臂专用，长度须等于 "
                         "--repeats）。k>1 的会话臂**必须**分目录——复用同一目录"
                         "会让两次读数混成一次（溯源缺口）")
    ap.add_argument("--out", default=None, help="结果 JSON 路径")
    ap.add_argument("--note", default=None,
                    help="操作者备注（记进 environment.operator_note，"
                         "如'no-attack-loop 消融臂'——哪一轮跑的必须可溯源）")
    args = ap.parse_args(argv)

    # ── k>1 管道的入参校验（跑前定死，跑后不改）───────────────────────
    if args.repeats < 1:
        raise SystemExit("--repeats 至少为 1")
    run_dirs = [d.strip() for d in (args.run_dirs or "").split(",") if d.strip()]
    if run_dirs and len(run_dirs) != args.repeats:
        raise SystemExit(
            "--run-dirs 给了 %d 个目录，但 --repeats=%d——必须相等："
            "一个 run 对应一个独立产物目录（防两次读数混同）"
            % (len(run_dirs), args.repeats))

    matcher = load_module_by_path(args.matcher_module, "ctd_la_signature_matcher")
    taskset, tasks_root = load_taskset(args.taskset)
    weak_texts, clean_texts = load_texts(tasks_root, taskset)

    tirr = None
    if MECHANICAL_ARM in [s.strip() for s in args.systems.split(",")] or True:
        # 计划质量与真实升级事故都要 T_irr。缺快照即报错，静默跳过会让
        # "真实升级事故"这一项悄悄消失，而它正是 M_3 的主指标。
        tirr = TIrR.load(T_IRR_SNAPSHOT)

    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    external = None
    need_external = [s for s in systems if s not in FIXTURE_ARMS and s != MECHANICAL_ARM]
    if need_external:
        if not args.arm_module:
            raise SystemExit(
                "系统 %s 不是夹具臂也不是机械臂，需要 --arm-module 给出臂模块路径\n"
                "  （形状：模块导出 run(payload) -> {\"findings\": [...], \"plan\": {...}}）"
                % "、".join(need_external))
        external = load_module_by_path(args.arm_module, "ctd_la_arm")
    if run_dirs and not need_external:
        raise SystemExit(
            "--run-dirs 只对**外部臂**（真实会话产物）有意义，但 --systems 里没有外部臂。"
            "夹具/机械臂的 k 次重复用裸 --repeats 即可（它们不读会话目录）。")

    results = {
        "harness_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "taskset": {
            "path": str(tasks_root / "taskset.json"),
            "taskset_version": taskset.get("taskset_version"),
            "n_instances": len(taskset["instances"]),
            "weaknesses_yaml_sha256": (taskset.get("provenance") or {})
            .get("weaknesses_yaml_sha256"),
        },
        "environment": {
            "matcher_module": str(args.matcher_module),
            "arm_module": str(args.arm_module) if args.arm_module else None,
            "systems": systems,
            # 溯源三字段（由 harness 记录，此前是会话后手工补的
            # ，手工补的字段无法复现，是复现缺口）。run_dir 决定取哪一轮会话
            # 产物（消融臂必须分开存）；attack_loop 是操作者对提示词层开关的声明；
            # operator_note 记"这是哪一条消融/哪一轮"。
            "run_dir": os.environ.get("ACTE_RUN_DIR"),
            "attack_loop": (
                None if not (os.environ.get("ACTE_ATTACK_LOOP") or "").strip()
                else (os.environ.get("ACTE_ATTACK_LOOP") or "").strip()
                     .lower() not in ("0", "false", "no", "off")),
            "operator_note": args.note,
            "fixture_arms_note": (
                "oracle / null 是**夹具臂**，不是基线，不得当作实验结果。"
                "它们的作用是标定度量链：oracle 应给出检出 100%、误报 0；"
                "null 应给出检出 0%、误报 0、升级事故 100%。"
                "任一条不符即说明度量链有 bug，而非系统强弱。"),
        },
        # k>1 管道：k 与逐 run 目录显式入档（顶层读数 = run 1，见 --repeats 帮助）
        "repeats": {"k": args.repeats, "run_dirs": run_dirs or None},
        "systems": {},
    }

    # ── k>1：单 run 运行 + 紧凑行 + 聚合 ──────────────────────────────
    def _eval_system(name, run_idx):
        """跑一个臂的一个 run 并判定。k 槽位语义：
        · `--run-dirs`：逐 run 换会话产物目录（会话臂的 k 必须分目录）；
        · `--repeats k>1`：API 基线按 `k<N>` 分槽落盘（同槽可断点续传，
          跨槽才是独立重复采样，共用一个槽会让 k 次全变同一条回复）。
        """
        if run_dirs:
            os.environ["ACTE_RUN_DIR"] = run_dirs[run_idx]
        if args.repeats > 1:
            os.environ["CTD_LA_BASELINE_RUN_TAG"] = "k%d" % (run_idx + 1)
        weak_notes, clean_notes = {}, {}
        if name in FIXTURE_ARMS:
            reports, clean_reports, plans = run_fixture_arm(
                name, taskset, weak_texts, clean_texts)
        elif name == MECHANICAL_ARM:
            bundle = arm_acte(taskset, tirr,
                              posture_override=args.posture_override,
                              shield_enabled=not args.no_shield,
                              include_reversible=args.include_reversible,
                              reversible_ids=REVERSIBLE_RISK_IDS)
            reports = {k: v["findings"] for k, v in bundle.items()}
            plans = {k: v["plan"] for k, v in bundle.items()}
            clean_reports = {}
        else:
            reports, clean_reports, plans, weak_notes, clean_notes = run_external_arm(
                external, name, taskset, weak_texts, clean_texts, tasks_root)
        return metrics_mod.evaluate_with_clean(
            matcher, taskset, reports, clean_reports, plans=plans, tirr=tirr,
            weak_notes=weak_notes, clean_notes=clean_notes,
            conjunction_across_findings=args.conjunction)

    def _compact(m, run_idx):
        d, fp, up, v = (m["detection"], m["false_positive"],
                        m["upgrade"], m["verbosity"])
        return {"run": run_idx + 1,
                "run_dir": run_dirs[run_idx] if run_dirs else None,
                "detection_rate": d["detection_rate"],
                "detection_rate_tier1": d["detection_rate_tier1"],
                "n_with_assertion": d["n_with_assertion"],
                "assertion_rate": d["assertion_rate"],
                "fp_rate": fp["rate"],
                "fp_major_rate": fp["fp_major"]["rate"],
                "incident_rate": up["incident_rate"],
                "mean_reported_per_instance": v["mean_reported_per_instance"]}

    def _aggregate(rows):
        """逐 run 行 → 均值/样本 sd。None（如空分母的事故率）整键记 None。"""
        keys = ("detection_rate", "detection_rate_tier1", "assertion_rate",
                "fp_rate", "fp_major_rate", "incident_rate",
                "mean_reported_per_instance")
        agg = {"n_runs": len(rows)}
        for key in keys:
            vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
            if not vals:
                agg[key] = None
                continue
            mean = round(sum(vals) / len(vals), 6)
            sd = None
            if len(vals) > 1:
                var = sum((x - mean) ** 2 for x in vals) / (len(vals) - 1)
                sd = round(var ** 0.5, 6)
            agg[key] = {"mean": mean, "sd": sd, "n": len(vals)}
        return agg

    for name in systems:
        run_rows = []
        first_m = None
        for r in range(args.repeats):
            m = _eval_system(name, r)
            if first_m is None:
                first_m = m
            run_rows.append(_compact(m, r))
        first_m["is_fixture_arm"] = name in FIXTURE_ARMS
        first_m["settings"] = {
            "posture_override": args.posture_override,
            "shield_enabled": not args.no_shield,
            "include_reversible": args.include_reversible,
            "conjunction_across_findings": bool(args.conjunction),
        }
        # 顶层读数 = run 1（k=1 与旧 schema 同形）；k>1 的完整读数看 runs[]/aggregate
        first_m["runs"] = run_rows
        first_m["aggregate"] = _aggregate(run_rows)
        results["systems"][name] = first_m
        d = first_m["detection"]
        fp = first_m["false_positive"]
        plan = first_m.get("plan") or {}
        real = plan.get("real_incident") or {}
        print("%-10s 检出 %s（tier2） / 误报 %s / 计划 %s 条 / 真实事故率 %s%s"
              % (name, d["detection_rate"], fp["rate"],
                 plan.get("n_with_plan", 0), real.get("incident_rate"),
                 "  [合取]" if args.conjunction else ""))
        if args.repeats > 1:
            agg = first_m["aggregate"]
            print("           k=%d 聚合：检出 mean %s（sd %s）/ 断言 mean %s / "
                  "误报 mean %s"
                  % (args.repeats,
                     (agg["detection_rate"] or {}).get("mean"),
                     (agg["detection_rate"] or {}).get("sd"),
                     (agg["assertion_rate"] or {}).get("mean"),
                     (agg["fp_rate"] or {}).get("mean")))
        if plan.get("n_with_plan"):
            for fam, s in sorted(plan.get("by_family", {}).items()):
                print("             %s：覆盖 %s / 效用 %s / 事故 %s（%d/%d）%s"
                      % (fam, s["mean_coverage"], s["mean_expected_utility"],
                         s["incident_rate"], s["n_incidents"], s["n_with_active_t_irr"],
                         "  ⚠ 退化 %d 条" % s["n_degenerate"] if s["n_degenerate"] else ""))

    out = Path(args.out) if args.out else (
        PAPER_ROOT / "experiment" / "results" / "harness-fixture-calibration.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print("\n已写出 %s" % out)
    print("提示：夹具臂的结果只用于标定度量链，不得当作实验结果。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())