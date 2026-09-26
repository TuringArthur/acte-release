"""输出侧沉默机制：把 tier2 的痕迹接地纪律前移到输出口。

## 作用

接地纪律的由来：全部基线与本引擎在 9/9 干净件上误报
（FP(any)=1.0），价值主张因此迁移到"能否在案子是好的时候保持沉默"一侧，
但系统此前没有履行这一主张的构件：findings 是单通道的，"建议/提醒"与"认定的弱点"
混在一起流进判定，谁都没有给出无问题结论的出口。

本模块给出三件：

1. 痕迹接地门槛：`evidence` 不能逐字落到案情原文（`facts_text`）的条目，
   降级进 `notes`（提示通道），不进 findings，与 tier2"只计有痕迹根据的子句"
   同一条纪律，只是从判定侧前移到输出侧；
2. 分级：findings = 重大弱点（major），notes = 提示事项（note），
   `severity` 字段从透传状态升级为被度量的标记；
3. 结论书：`verdict_memo()`，findings 为空时给出
   《立案与诉讼准备评估结论书：未见重大弱点》。"给干净案开证明"由此成为
   系统可产出的交付物，而不是论文里的一句比喻。

## 公平性边界

补丁只进我方臂（`arm_acte_model.py`），不送 B1–B4：基线保持原样。
于是双口径差（FP(any) vs FP(重大)）恰是方法贡献量本身；旧读数 FP(any)=1.0
全员对照作为历史行保留。度量侧的双口径在 `metrics.evaluate_with_clean()` 落地。

## 与判定机的关系

本模块不改判定机、不改签名。降级后的 notes 不进签名判定（harness 只把
findings 交给 `judge_instance`）。这是方法侧输出重构，不是判定口径变更；
读数差异按口径纪律 记入 `口径变更记账`。

## 接地判据（三条）

1. `evidence` 为空 ⇒ 不接地（没引任何原文）；
2. 去掉首尾引号/书名号/空白后非空且连续出现在 `facts_text` ⇒ 接地
   ，实测模型常给引文加中文引号（`"…"`），字面包含判断前须剥掉（B1 的
   evidence 就常带引号，见 BASELINES.md §2）；
3. 其余 ⇒ 降级（模型把自己的推理写进了引文栏，B1 的典型形态，
   BASELINES.md §2 有诚实记录）。
"""

from __future__ import annotations

# 首尾可剥的包裹字符：模型可能加的各种引号与空白
_WRAP_CHARS = " \t\r\n\"'“”‘’「」『』《》（）()"

SEVERITY_MAJOR = "major"
SEVERITY_NOTE = "note"

CLEAN_VERDICT = (
    "立案与诉讼准备评估结论书：无重大弱点。"
    "（全部条目均未逐字引到案情原文，已按痕迹接地纪律降级为提示事项，"
    "不构成弱点认定。）"
)


def strip_wrap(text):
    """剥掉引文首尾的引号与空白，保留内部原文。"""
    return text.strip(_WRAP_CHARS)


def _field(obj, name):
    """读字段：兼容 dict（`parse_issues_f1` 的产物）与 `Finding` 对象（测试/夹具）。

     只用 `getattr` 会在 dict 上静默取到 None ， 全部条目会被判"未接地"
    而降级，门槛看起来在工作、实际把整个 findings 通道清空。故 dict 走 `.get`。
    """
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _set_severity(obj, value):
    if isinstance(obj, dict):
        obj["severity"] = value
    else:
        obj.severity = value


def is_grounded(finding, facts_text):
    """`evidence` 是否逐字落到案情原文（接地）。"""
    evidence = _field(finding, "evidence") or ""
    candidate = strip_wrap(str(evidence))
    if not candidate:
        return False
    if not facts_text:
        return False
    return candidate in str(facts_text)


def apply_output_gate(findings, facts_text):
    """输出门槛：接地的留在 findings（major），不接地的降级进 notes（note）。

    :param findings: 解析器产出的条目列表（dict 或 `Finding` 形状均可）。
    :param facts_text: 本案案情原文（接地参照；弱点件/干净件各用各的）。
    :returns: `(major_findings, notes)`
    """
    major, notes = [], []
    for f in findings or []:
        if is_grounded(f, facts_text):
            if _field(f, "severity") is None:
                _set_severity(f, SEVERITY_MAJOR)
            major.append(f)
        else:
            _set_severity(f, SEVERITY_NOTE)
            notes.append(f)
    return major, notes


def verdict_memo(major_findings):
    """结论书文案：findings 为空 ⇒ 无重大弱点证明；否则报认定条数。"""
    if not major_findings:
        return CLEAN_VERDICT
    return "立案与诉讼准备评估结论：认定重大弱点 %d 项。" % len(major_findings)
