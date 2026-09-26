#!/usr/bin/env python3
"""度量链标定测试：用夹具臂把"指标算得对不对"变成可跑的检查。

## 为什么必须有这个测试

评测要三样东西：任务集、判定机、被测系统。第三样（真模型臂）还没接。
于是有一个真实的风险：等真模型跑出数字时，你无法区分"系统弱"与"度量错了"。

缺陷码等值判定会让共享基线 B1–B4 的效力性检出率恒为 0.000；
那看起来像"基线很弱"，实际是口径错（基线的自由文本码与内部登记码永不相等）。那个 bug 之所以能存活，是因为没有一条检查能证明
"当系统确实报对了时，指标会给它分"。

本文件就是那条检查。两条夹具臂给出上下界：

| 臂 | 行为 | 应得读数 |
|---|---|---|
| `oracle` | 对每个实例报出恰好命中签名的引文 | 检出 1.0、误报 0.0、升级事故 0.0 |
| `null` | 什么都不报 | 检出 0.0、误报 0.0、升级事故 1.0 |

任一条不符 ⇒ 度量链有 bug，而不是系统强弱。夹具臂不是基线，
其结果不得当作实验结果（harness 已把 `is_fixture_arm` 写进结果文件）。

## 还要守住的一条：判定方向

除上下界外，本文件另测判定方向，这是比数字更根本的一层：
"报对了的"必须给分、"报错方向的"必须不给分。四条标定输入：

1. 引到痕迹 + 断言后果 ⇒ 给分（tier2）
2. 引文只在干净件、却断言后果 ⇒ 不给分（这正是"没发现改动却把结论说对/说反"）
3. 只引年份片段（日期不完整）⇒ 不给分（原子字段必须全值命中）
4. 超长引文（整段底本）⇒ 不给分（反退化守卫）

    python3 code/tests/test_metrics_calibration.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
PAPER_ROOT = HERE.parents[2]
CODE_ROOT = PAPER_ROOT / "code"
TASKS_ROOT = PAPER_ROOT / "experiment" / "tasks"

try:
    import yaml  # noqa: F401
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 PyYAML：python3 -m pip install pyyaml\n")
    raise SystemExit(2)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CODE_ROOT))
    try:
        spec.loader.exec_module(mod)
    finally:
        if sys.path and sys.path[0] == str(CODE_ROOT):
            sys.path.pop(0)
    return mod


# 判定机路径按统一解析器取：环境变量 → 本仓钉版副本 → 上层仓库内的冻结副本
# （见 `code/tools/matcher_path.py`）。
_matcher_path = _load("f1_matcher_path", CODE_ROOT / "tools" / "matcher_path.py")
MATCHER_PATH = _matcher_path.resolve(CODE_ROOT, HERE)


class Case:
    def __init__(self):
        self.n, self.failed = 0, []

    def check(self, name, cond, detail=""):
        self.n += 1
        if cond:
            print("  ok   %s" % name)
        else:
            print("  FAIL %s%s" % (name, ("  — " + detail) if detail else ""))
            self.failed.append(name)


def main():
    print("=" * 70)
    print("度量链标定测试（夹具臂上下界 + 判定方向）")
    print("=" * 70)

    c = Case()
    matcher_path = MATCHER_PATH
    if matcher_path is None:
        sys.stderr.write(_matcher_path.describe(CODE_ROOT, HERE) + "\n")
        return 2

    harness = _load("f1_harness", CODE_ROOT / "harness.py")
    matcher = _load("f1_matcher_cal", matcher_path)
    metrics_mod = _load("f1_metrics", CODE_ROOT / "ctd_acte" / "metrics.py")
    Finding = metrics_mod.Finding

    try:
        taskset, tasks_root = harness.load_taskset(TASKS_ROOT)
    except harness.HarnessError as exc:
        # 算法本体导出的仓库不含实验材料，本套件的输入因此缺失。
        # 这里报「不适用」（退出码 2）而不是抛异常：异常看起来像套件坏了，
        # 而实际是这一步本来就以任务集为输入，见 README 第 5、8 节。
        print("\n不适用：本套件以任务集为输入，当前仓库不含实验材料。")
        print("  原因：%s" % exc)
        print("  这不是失败。取得实验材料后重跑，或直接跑 code/tools/verify_all.py"
              "（该脚本会自动把本步列为「不适用」）。")
        return 2
    weak_texts, clean_texts = harness.load_texts(tasks_root, taskset)

    # ── A. 上下界标定 ──────────────────────────────────────────
    print("\n[A] 夹具臂上下界（不符即度量链有 bug，不是系统强弱）")

    # tirr 必须传：升级口径的分母由「本案有没有跨层级动作可选」决定（见
    # metrics.has_irreversible_action）。不传 ⇒ 分母为空 ⇒ 事故率 None ⇒ 本组断言失败。
    from ctd_acte.t_irr import TIrR
    tirr = TIrR.load(CODE_ROOT / "data" / "t-irr.json")
    oracle_reports, oracle_clean, _ = harness.run_fixture_arm(
        "oracle", taskset, weak_texts, clean_texts)
    m_oracle = metrics_mod.evaluate_with_clean(
        matcher, taskset, oracle_reports, oracle_clean, tirr=tirr)

    c.check("oracle：检出率 = 1.0（报对了必须给分）",
            m_oracle["detection"]["detection_rate"] == 1.0,
            repr(m_oracle["detection"]["detection_rate"]))
    c.check("oracle：误报率 = 0.0", m_oracle["false_positive"]["rate"] == 0.0,
            repr(m_oracle["false_positive"]["rate"]))
    c.check("oracle：升级事故率 = 0.0（识别到风险即不算事故）",
            m_oracle["upgrade"]["incident_rate"] == 0.0,
            repr(m_oracle["upgrade"]["incident_rate"]))
    c.check("oracle：后果断言率 = 1.0（痕迹与断言子句均成立）",
            m_oracle["detection"]["assertion_rate"] == 1.0,
            repr(m_oracle["detection"]["assertion_rate"]))
    c.check("oracle：平均报出 1.0 条/实例（不滥报）",
            m_oracle["verbosity"]["mean_reported_per_instance"] == 1.0,
            repr(m_oracle["verbosity"]["mean_reported_per_instance"]))
    # 红队开关 的落点：T4 与弱点检出同口径，必须在结果结构里显式可见
    af = m_oracle.get("attack_forecast") or {}
    c.check("★ attack_forecast（T4）recall 与检出率同值（红队开关读这一格）",
            af.get("recall") == m_oracle["detection"]["detection_rate"] == 1.0,
            "recall=%s detection=%s" % (af.get("recall"),
                                        m_oracle["detection"]["detection_rate"]))
    c.check("attack_forecast.precision 显式为 None（不冒充机械 precision）",
            af.get("precision") is None)

    null_reports, null_clean, _ = harness.run_fixture_arm(
        "null", taskset, weak_texts, clean_texts)
    m_null = metrics_mod.evaluate_with_clean(
        matcher, taskset, null_reports, null_clean, tirr=tirr)
    c.check("null：检出率 = 0.0（不报不得分 —— 防'从没报也算检出'）",
            m_null["detection"]["detection_rate"] == 0.0,
            repr(m_null["detection"]["detection_rate"]))
    c.check("null：误报率 = 0.0", m_null["false_positive"]["rate"] == 0.0)
    c.check("null：升级事故率 = 1.0（未识别风险，全部计事故）",
            m_null["upgrade"]["incident_rate"] == 1.0,
            repr(m_null["upgrade"]["incident_rate"]))

    n_tirr = m_null["upgrade"]["n_t_irr_instances"]
    c.check("检定实例里确有『可选跨层级动作』的实例（否则升级口径的分母为 0、上面那条是空转）",
            n_tirr > 0, "n_t_irr_instances=%d" % n_tirr)

    # ── B. 判定方向（比数字更根本的一层）──────────────────────
    print("\n[B] 判定方向（四条标定输入）")
    inst = taskset["instances"][0]
    tid = inst["task_id"]
    sig = inst["expected"]["signature"]
    weak = weak_texts[tid]
    clean = clean_texts[inst["base_case"]]
    grounded = next(cl for cl in sig["clauses"] if cl.get("grounded"))
    pat = next(t for t in grounded["terms"] if t["kind"] == "pattern")
    assertion_alt = sig["clauses"][-1]["terms"][0]["alts"][0]

    def credited(findings):
        j = metrics_mod.judge_instance(matcher, inst, findings)
        return bool(j["tier2"])

    m = re.search(pat["regex"], weak)
    loc = weak[max(0, m.start() - 10):m.end() + 10]

    c.check("① 引到痕迹 + 断言后果 ⇒ 给分",
            credited([Finding(evidence=loc, message=assertion_alt)]))
    c.check("② 引文只在干净件、却断言后果 ⇒ 不给分",
            not credited([Finding(evidence=clean[:20], message=assertion_alt)]))
    # ③ 只引年份片段：构造一个比完整痕迹短的引文（取痕迹前 4 字）
    partial = pat["regex"][:4]
    c.check("③ 引文只是痕迹的一小段（日期不完整）⇒ 不给分",
            not credited([Finding(evidence=partial, message=assertion_alt)]),
            "引文 %r / 痕迹 %r" % (partial, pat["regex"]))
    # ④ 超长引文（反退化守卫）
    long_evidence = (weak * 3)[:metrics_mod.DEFAULT_MAX_EVIDENCE_CHARS + 40]
    c.check("④ 超长引文（>=%d 字）⇒ 不给分" % metrics_mod.DEFAULT_MAX_EVIDENCE_CHARS,
            not credited([Finding(evidence=long_evidence, message=assertion_alt)]),
            "引文 %d 字" % len(long_evidence))

    # ── C. 边界：空任务集必须被拒 ─────────────────────────────
    print("\n[C] 边界守卫")
    empty_ts = {"instances": []}
    try:
        metrics_mod.evaluate(matcher, empty_ts, {})
        c.check("空任务集 ⇒ 报错（0.000 看着像跑过了）", False, "没有抛错")
    except metrics_mod.MetricsError:
        c.check("空任务集 ⇒ 报错（0.000 看着像跑过了）", True)
    except Exception as exc:
        c.check("空任务集 ⇒ 报错", False, "抛了 %r" % exc)

    # ── C2. 干净件载荷盲化（防"一眼看出这是干净件"）──────────
    #
    # 早先 harness 给干净件的载荷带 `is_clean: True`，臂能直接读到
    # 真值标签，"能否在案子是好的时候保持沉默"这条主结论即失效。
    # 本守卫钉住：任何臂收到的载荷都不含该字段（有回归即红）。
    # 载荷的其余结构差（弱件多 posture/actions 等）属任务设计，
    # 干净件本就不问计划段，见 BASELINES.md 口径记账。
    seen_payloads = []

    def _record_arm(payload):
        seen_payloads.append(payload)
        return {"findings": []}

    probe_ts = {"instances": taskset["instances"][:2]}
    probe_weak = {i["task_id"]: weak_texts[i["task_id"]] for i in probe_ts["instances"]}
    probe_clean = {}
    for i in taskset["instances"][:3]:
        cid = i["base_case"]
        if cid not in probe_clean:
            probe_clean[cid] = clean_texts[cid]
    harness.run_external_arm(
        type("M", (), {"run": staticmethod(_record_arm)})(),
        "probe", probe_ts, probe_weak, probe_clean, tasks_root)
    leaked = [p["task_id"] for p in seen_payloads if "is_clean" in p]
    c.check("★ 干净件载荷已盲化：任何载荷都不含 is_clean 真值标签",
            bool(seen_payloads) and not leaked,
            ("载荷为空（守卫空转）" if not seen_payloads
             else "泄漏给：%s" % "、".join(leaked)))

    # ── D. 结果文件带溯源 ────────────────────────────────────
    print("\n[D] 结果文件的溯源字段")
    res_path = PAPER_ROOT / "experiment" / "results" / "harness-fixture-calibration.json"
    if res_path.is_file():
        res = json.loads(res_path.read_text(encoding="utf-8"))
        c.check("结果记录 taskset 版本与弱点目录 sha256",
                bool(res["taskset"].get("weaknesses_yaml_sha256")))
        c.check("结果标注夹具臂不得当实验结果",
                "夹具臂" in (res["environment"].get("fixture_arms_note") or ""))
        c.check("oracle/null 在结果里被标为夹具臂",
                all(res["systems"][k]["is_fixture_arm"] for k in ("oracle", "null")))
    else:
        c.check("结果文件存在（先跑 harness.py）", False, "缺 %s" % res_path)

    # ── E. 所有结果文件的新鲜度（防陈旧读数混进论文）──────────
    #
    # 加实例后若忘记重跑某个臂，那个文件会保留旧实例数的数字而没有任何标志。
    # 这里出现过：一次批量重跑里两条命令的参数被 shell 拆词拆坏，
    # 那两个文件没更新，而打印出来的表照样有数（是旧数）：肉眼看不出来。
    # `harness.py` 已把 `taskset.n_instances` 写进每个结果文件，故这里可机械核对。
    print("\n[E] 结果文件新鲜度（实例数须与当前任务集一致）")
    results_dir = PAPER_ROOT / "experiment" / "results"
    cur_n = len(taskset["instances"])
    files = sorted(results_dir.glob("*.json"))
    if not files:
        c.check("结果目录非空", False, "没有结果文件")
    stale = []
    for f in files:
        try:
            got = json.loads(f.read_text(encoding="utf-8"))["taskset"]["n_instances"]
        except Exception as exc:
            stale.append("%s（读取失败：%s）" % (f.name, exc))
            continue
        if got != cur_n:
            stale.append("%s（记 %s 实例，当前 %d）" % (f.name, got, cur_n))
    c.check("全部 %d 份结果文件的实例数都与当前任务集（%d）一致——无陈旧读数"
            % (len(files), cur_n), not stale,
            "陈旧或损坏：%s\n      修法：用当前任务集重跑对应臂" % "；".join(stale))

    # ── F. 跨 finding 合取（默认关）────────────────────────────────
    #
    # 依 实验侧的合取口径纪律：开关在 judge_instance 的
    # conjunction_across_findings（默认关）。夹具标定三件套 =
    #   ① oracle/null 在合取开时必须与关同读数；
    #   ② 2 个合取标定输入（拆分表达 → 救回，一断言一痕迹）；
    #   ③ 负例（合取不得给"只有断言没有痕迹"授信；超长守卫不得被池化绕开）。
    print("\n[F] 跨 finding 合取（默认关 + 标定输入×2 + 负例 + 夹具同读数）")

    inst_p = next(i for i in taskset["instances"]
                  if i["task_id"] == "F1-INJ-PRESV-001")
    sig_p = inst_p["expected"]["signature"]
    trace_re = next(t["regex"] for t in sig_p["clauses"][0]["terms"]
                    if t["kind"] == "pattern")
    assert_alt = sig_p["clauses"][-1]["terms"][0]["alts"][0]

    j_default = metrics_mod.judge_instance(matcher, inst_p, [])
    j_off = metrics_mod.judge_instance(matcher, inst_p, [],
                                       conjunction_across_findings=False)
    c.check("F-a 默认关与显式 False 逐键同构，conjunction.applied=False",
            j_default == j_off and j_default["conjunction"]["applied"] is False,
            repr(j_default.get("conjunction")))

    # 标定输入①（PRESV-001 型拆分表达）：引文在 f1、后果断言在 f2
    f_trace_only = Finding(code="split-1", evidence="…%s…" % trace_re,
                           message="对方申请财产保全时未按要求提供担保。")
    f_assert_only = Finding(code="split-2", evidence="法院采纳了对方的保全申请书。",
                            message="构成%s，我方有权主张。" % assert_alt)
    mini_p = {"instances": [inst_p]}
    rep_split = {inst_p["task_id"]: [f_trace_only, f_assert_only]}
    m_conj_off = metrics_mod.evaluate(matcher, mini_p, dict(rep_split))
    m_conj_on = metrics_mod.evaluate(matcher, mini_p, dict(rep_split),
                                     conjunction_across_findings=True)
    c.check("F-b 标定输入①关：痕迹在 f1 ⇒ 检出 1；断言拆开没接上 ⇒ 断言 0",
            m_conj_off["detection"]["tp"] == 1
            and m_conj_off["detection"]["n_with_assertion"] == 0,
            json.dumps({k: m_conj_off["detection"][k]
                        for k in ("tp", "n_with_assertion")}, ensure_ascii=False))
    c.check("F-c 标定输入①开：断言经合取救回，且该实例入 rescue 列表",
            m_conj_on["detection"]["n_with_assertion"] == 1
            and inst_p["task_id"]
            in m_conj_on["detection"]["conjunction"]["rescued_assertion"],
            json.dumps(m_conj_on["detection"]["conjunction"],
                       ensure_ascii=False)[:200])

    # 标定输入②（多痕迹项拆分 → 检出救回）：f_a、f_b 各持痕迹子句的一半。
    # 痕迹子句可含多项（子句内项间是"与"）：v3 首例 W-LIM-003 即双项，
    # v4 批次（lim/tim 等）大量双项。本条仍用合成签名钉住"拆分表达能被
    # 合取救回"这个能力本身（与具体实例解耦，扩样不需重钉）。
    synth_sig = {
        "doc_scope": "target",
        "clauses": [
            {"grounded": True,
             "terms": [
                 {"kind": "pattern", "regex": "暂未提供担保",
                  "min_len": 0, "floor": 0, "alts": []},
                 {"kind": "pattern", "regex": "保全范围超出",
                  "min_len": 0, "floor": 0, "alts": []}],
             "dropped_terms": []},
            dict(sig_p["clauses"][1]),
        ],
    }
    synth_inst = dict(inst_p)
    synth_inst["task_id"] = "SYN-CONJ-001"
    synth_inst["expected"] = {"signature": synth_sig}
    f_half_a = Finding(code="half-a", evidence="申请人暂未提供担保",
                       message="保全申请的担保要件不满足。")
    f_half_b = Finding(code="half-b", evidence="且保全范围超出诉讼请求。",
                       message="请求范围明显过大。")
    mini_s = {"instances": [synth_inst]}
    rep_half = {"SYN-CONJ-001": [f_half_a, f_half_b]}
    m_s_off = metrics_mod.evaluate(matcher, mini_s, dict(rep_half))
    m_s_on = metrics_mod.evaluate(matcher, mini_s, dict(rep_half),
                                  conjunction_across_findings=True)
    c.check("F-d 标定输入②关：两条各持一半痕迹 ⇒ 实例漏检（tp=0）",
            m_s_off["detection"]["tp"] == 0,
            "tp=%s" % m_s_off["detection"]["tp"])
    c.check("F-e 标定输入②开：池化接上 ⇒ 检出 1，且入 rescued_detection",
            m_s_on["detection"]["tp"] == 1
            and "SYN-CONJ-001"
            in m_s_on["detection"]["conjunction"]["rescued_detection"],
            json.dumps(m_s_on["detection"]["conjunction"],
                       ensure_ascii=False)[:200])

    # 负例①：全池只有断言、没有痕迹 ⇒ grounded 子句没进池，合取不得授信
    f_assert_bare = Finding(code="bare", evidence="对方的保全申请书正文若干。",
                            message="构成%s。" % assert_alt)
    m_bare_on = metrics_mod.evaluate(
        matcher, mini_s, {"SYN-CONJ-001": [f_assert_bare]},
        conjunction_across_findings=True)
    c.check("F-f 负例：只有断言没有痕迹 ⇒ 合取开也仍不授信（tp=0）",
            m_bare_on["detection"]["tp"] == 0,
            "tp=%s" % m_bare_on["detection"]["tp"])

    # 负例②：痕迹只在超长引文里 ⇒ 池必须同样吃 240 字守卫（不许经池化绕开）
    overlong = Finding(code="over",
                       evidence="暂未提供担保保全范围超出" + "x" * 300,
                       message="整段底本抄进引文")
    j_over = metrics_mod.judge_instance(matcher, synth_inst, [overlong],
                                        conjunction_across_findings=True)
    c.check("F-g 负例：超长 finding 不进合取池（applied 开、tier2 False、入 unmatched）",
            j_over["conjunction"]["applied"] is True
            and j_over["conjunction"]["tier2"] is False
            and overlong in j_over["unmatched"],
            repr(j_over["conjunction"]))

    # 夹具同读数：oracle/null 在合取开时与关逐格一致
    m_oracle_conj = metrics_mod.evaluate_with_clean(
        matcher, taskset, oracle_reports, oracle_clean, tirr=tirr,
        conjunction_across_findings=True)
    m_null_conj = metrics_mod.evaluate_with_clean(
        matcher, taskset, null_reports, null_clean, tirr=tirr,
        conjunction_across_findings=True)
    c.check("F-h oracle 合取开：检出 1.0 / 断言率 1.0 / 误报 0（与关同读数）",
            m_oracle_conj["detection"]["detection_rate"] == 1.0
            and m_oracle_conj["detection"]["assertion_rate"] == 1.0
            and m_oracle_conj["false_positive"]["rate"] == 0.0,
            json.dumps({k: m_oracle_conj["detection"][k]
                        for k in ("detection_rate", "assertion_rate")}))
    c.check("F-i null 合取开：检出 0.0 / 误报 0 / 升级事故 1.0（与关同读数）",
            m_null_conj["detection"]["detection_rate"] == 0.0
            and m_null_conj["false_positive"]["rate"] == 0.0
            and m_null_conj["upgrade"]["incident_rate"] == 1.0,
            repr((m_null_conj["detection"]["detection_rate"],
                  m_null_conj["false_positive"]["rate"],
                  m_null_conj["upgrade"]["incident_rate"])))

    res_conj = results_dir / "harness-fixture-calibration-conj.json"
    if res_conj.is_file():
        rj = json.loads(res_conj.read_text(encoding="utf-8"))
        c.check("合取夹具结果已入档：settings.conjunction_across_findings=True",
                all((rj["systems"][k]["settings"] or {})
                    .get("conjunction_across_findings")
                    for k in ("oracle", "null")))
        c.check("合取夹具读数与关同格（oracle 1.0 / null 0.0 / oracle 误报 0 / "
                "conjunction.applied=True）",
                rj["systems"]["oracle"]["detection"]["detection_rate"] == 1.0
                and rj["systems"]["null"]["detection"]["detection_rate"] == 0.0
                and rj["systems"]["oracle"]["false_positive"]["rate"] == 0.0
                and rj["systems"]["oracle"]["detection"]["conjunction"]["applied"]
                is True)
    else:
        c.check("合取夹具标定结果文件存在（harness --conjunction --systems oracle,null）",
                False, "缺 %s（夹具标定要求入档）" % res_conj)

    # ── G. 输出侧沉默机制与 FP 双口径 ─────────────────────────────
    #
    # 依 实验侧的接地口径纪律：门槛 = tier2 的痕迹接地
    # 纪律前移到输出侧，evidence 未逐字落到案情原文的条目降级进 notes，
    # 不进 findings（不参与签名判定）。FP 双口径：rate = FP(any)（与补丁前同构）、
    # fp_major = FP(重大)（新主指标）。未送补丁的臂 notes 恒空 ⇒ 双口径必须相等。
    print("\n[G] 沉默机制（output_gate）与 FP 双口径")
    gate = _load("f1_output_gate", CODE_ROOT / "ctd_acte" / "output_gate.py")

    inst0 = taskset["instances"][0]
    facts0 = weak_texts[inst0["task_id"]]
    sample = facts0[20:50]
    g_ok = Finding(evidence=sample, message="本案存在时效抗辩风险")
    g_quoted = Finding(evidence='"%s"' % sample, message="模型给引文加了引号，仍应接地")
    g_ungrounded = Finding(evidence="根据《民法典》第188条，本案已过三年诉讼时效",
                           message="模型自己的推理，不在案情原文里")
    g_empty = Finding(evidence="", message="没引任何原文")
    major, demoted = gate.apply_output_gate(
        [g_ok, g_quoted, g_ungrounded, g_empty], facts0)
    c.check("接地（案情连续片段）留在 findings", len(major) == 2,
            "major=%d" % len(major))
    c.check("引号包裹的接地引文不被误杀（剥壳后再判）",
            g_ok in major and g_quoted in major)
    c.check("未接地 + 空 evidence 降级进 notes", len(demoted) == 2,
            "notes=%d" % len(demoted))
    c.check("门槛不丢条目（2 + 2 = 4）", len(major) + len(demoted) == 4)
    # dict 形状（parse_issues_f1 的真实产物）必须与对象形状同判，
    # 早期实现只用 getattr，会在 dict 上静默取 None 把整个 findings 通道清空。
    d_major, d_notes = gate.apply_output_gate(
        [{"evidence": sample, "message": "x"},
         {"evidence": "编造的推理，不在案情里", "message": "y"}], facts0)
    c.check("dict 形状同样正确分级（不因 getattr 静默 None 而全量降级）",
            len(d_major) == 1 and len(d_notes) == 1,
            "major=%d notes=%d" % (len(d_major), len(d_notes)))
    c.check("severity 被分级标记（major / note）",
            getattr(g_ok, "severity", None) == "major"
            and getattr(g_ungrounded, "severity", None) == "note",
            "major=%r note=%r" % (getattr(g_ok, "severity", None),
                                  getattr(g_ungrounded, "severity", None)))
    memo = gate.verdict_memo([])
    c.check("findings 为空 ⇒ 结论书给『无重大弱点』证明（沉默的交付物）",
            "无重大弱点" in memo, memo)
    c.check("findings 非空 ⇒ 结论书报认定条数",
            "1 项" in gate.verdict_memo([g_ok]), gate.verdict_memo([g_ok]))

    # FP 双口径：只给一个干净件造输出（其余干净件无输出 ⇒ rate = 1/n）
    base0 = sorted({i["base_case"] for i in taskset["instances"]})[0]
    n_clean = len({i["base_case"] for i in taskset["instances"]})
    m_notes_only = metrics_mod.evaluate_with_clean(
        matcher, taskset, {}, {}, tirr=tirr,
        clean_notes={base0: [g_ungrounded]})
    c.check("FP 双口径：只有 notes 的干净件 ⇒ FP(any) 记误报、FP(重大) 不记",
            m_notes_only["false_positive"]["rate"] == round(1 / n_clean, 4)
            and m_notes_only["false_positive"]["fp_major"]["rate"] == 0.0,
            "any=%s major=%s"
            % (m_notes_only["false_positive"]["rate"],
               m_notes_only["false_positive"]["fp_major"]["rate"]))
    m_has_major = metrics_mod.evaluate_with_clean(
        matcher, taskset, {}, {base0: [g_ok]}, tirr=tirr,
        clean_notes={base0: [g_ungrounded]})
    c.check("FP 双口径：findings 非空 ⇒ 两口径都记该件",
            m_has_major["false_positive"]["rate"]
            == m_has_major["false_positive"]["fp_major"]["rate"]
            == round(1 / n_clean, 4),
            "any=%s major=%s"
            % (m_has_major["false_positive"]["rate"],
               m_has_major["false_positive"]["fp_major"]["rate"]))
    c.check("未送补丁的臂（oracle，无 notes）双口径必须相等且为 0",
            m_oracle["false_positive"]["fp_major"]["rate"]
            == m_oracle["false_positive"]["rate"] == 0.0,
            "major=%s any=%s"
            % (m_oracle["false_positive"]["fp_major"]["rate"],
               m_oracle["false_positive"]["rate"]))
    c.check("oracle verbosity 记 notes 通道（补丁外的臂恒为 0，防字段消失）",
            (m_oracle["verbosity"].get("n_notes_total") == 0))

    # ── H. 断点续传（基线落盘）与 harness k>1 schema ─────────────────
    #
    # 设备随时可能关机：
    #   · 基线逐实例回复落盘 ⇒ 重跑命中即续、指纹失配即拒用旧回复、
    #     模型 id 进指纹（跨模型不误复用）；
    #   · harness --repeats k ⇒ runs[]/aggregate 入档，顶层读数 = run 1。
    print("\n[H] 断点续传（基线落盘）与 harness k>1 schema")
    baselines = _load("f1_baselines_p3", CODE_ROOT / "tools" / "arm_baselines_f1.py")
    run_root = TASKS_ROOT.parent / "run"
    env_keys = ("CTD_LA_PLUMBING", "CTD_LA_BASELINE_CACHE_DIR",
                "CTD_LA_BASELINE_RUN_TAG", "CTD_LA_MODEL_ID", "DSH_MODEL_ID")
    saved_env = {k: os.environ.get(k) for k in env_keys}
    test_cache_dir = run_root / "_test-baseline-cache"
    try:
        # 管线路径指向不存在的文件：命中落盘回复时不应走到加载管线那一步。
        os.environ["CTD_LA_PLUMBING"] = str(CODE_ROOT / "no-such-plumbing.py")
        os.environ["CTD_LA_BASELINE_CACHE_DIR"] = str(test_cache_dir)
        os.environ["CTD_LA_BASELINE_RUN_TAG"] = "unittest"
        payload_h = {"arm": "b1", "task_id": "H-CACHE-001",
                     "facts_text": "案件事实：测试事实甲乙丙。"}
        sha_h = baselines._input_sha256("b1", payload_h)
        cache_h = baselines._cache_dir("b1") / "H-CACHE-001.json"
        baselines._cache_write(cache_h, "b1", "H-CACHE-001", sha_h,
                               "时效风险 | 2019年6月1日知道权利受损 | "
                               "诉讼时效已过 | 超过三年未主张", {})
        out_h = baselines.run(dict(payload_h))
        c.check("H-a 命中落盘回复：不加载管线（假路径在档也照常出 findings）",
                len(out_h["findings"]) >= 1,
                json.dumps(out_h.get("meta"), ensure_ascii=False))
        c.check("H-b meta.cache=hit（读结果文件可核断点续传生效）",
                (out_h.get("meta") or {}).get("cache") == "hit",
                repr((out_h.get("meta") or {}).get("cache")))
        bad_h = dict(payload_h)
        bad_h["facts_text"] = "案件事实：输入已变（指纹必须失配）。"
        try:
            baselines.run(bad_h)
            stale_rejected = False
        except baselines.AdapterError:
            stale_rejected = True
        c.check("H-c 输入指纹失配 ⇒ 拒用旧回复（宁重打不可复用）", stale_rejected)
        model_saved = os.environ.get("DSH_MODEL_ID")
        os.environ["DSH_MODEL_ID"] = (model_saved or "model-a") + "-other"
        sha_model = baselines._input_sha256("b1", payload_h)
        if model_saved is None:
            os.environ.pop("DSH_MODEL_ID", None)
        else:
            os.environ["DSH_MODEL_ID"] = model_saved
        c.check("H-d 模型 id 进输入指纹（跨模型臂不会误复用旧回复）",
                sha_model != sha_h)

        # harness k>1（夹具臂：秒级、不联网）
        harness_out = run_root / "_test-harness-repeats.json"
        harness.main(["--systems", "oracle,null", "--repeats", "2",
                      "--out", str(harness_out), "--note", "[H] k>1 schema"])
        res_h = json.loads(harness_out.read_text(encoding="utf-8"))
        c.check("H-e repeats 段入档（k=2）", res_h.get("repeats", {}).get("k") == 2,
                json.dumps(res_h.get("repeats")))
        o_h = res_h["systems"]["oracle"]
        c.check("H-f runs[] 逐 run 两行、检出都 1.0",
                len(o_h.get("runs") or []) == 2
                and all(r["detection_rate"] == 1.0 for r in o_h["runs"]),
                json.dumps(o_h.get("runs"), ensure_ascii=False))
        agg_det = ((o_h.get("aggregate") or {}).get("detection_rate")) or {}
        c.check("H-g aggregate 均值/sd/n 入档",
                agg_det.get("mean") == 1.0 and agg_det.get("n") == 2,
                json.dumps(agg_det))
        c.check("H-h 顶层读数 = run1 且 settings 含合取键",
                o_h["detection"]["detection_rate"] == 1.0
                and "conjunction_across_findings" in (o_h.get("settings") or {}))
        n_h = res_h["systems"]["null"]
        c.check("H-i null 聚合检出 0.0（k=2 不改夹具语义）",
                ((n_h.get("aggregate") or {}).get("detection_rate") or {}).get("mean")
                == 0.0)
        harness_out1 = run_root / "_test-harness-repeats-k1.json"
        harness.main(["--systems", "oracle", "--out", str(harness_out1),
                      "--note", "[H] k=1 default"])
        res1 = json.loads(harness_out1.read_text(encoding="utf-8"))
        c.check("H-j k=1 默认：repeats.k=1、runs 恰 1 行（与旧读者兼容）",
                res1.get("repeats", {}).get("k") == 1
                and len(res1["systems"]["oracle"].get("runs") or []) == 1)
        try:
            harness.main(["--systems", "oracle", "--repeats", "2",
                          "--run-dirs", "a,b,c", "--out", str(harness_out1)])
            guard_ok = False
        except SystemExit:
            guard_ok = True
        c.check("H-k --run-dirs 长度 ≠ --repeats ⇒ 跑前拒绝（防两次读数混同）",
                guard_ok)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(test_cache_dir, ignore_errors=True)

    print("\n" + "=" * 70)
    if c.failed:
        print("失败 %d / %d 项：" % (len(c.failed), c.n))
        for f in c.failed:
            print("  - %s" % f)
        return 1
    print("全部通过（%d 项断言）" % c.n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())