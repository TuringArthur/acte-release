#!/usr/bin/env python3
"""任务集生成器：注入计划 → 弱点底本 + 核验过的金标准签名 + `taskset.json`。

## 纪律（与共享任务集管线同一套，理由相同）

金标准 = 注入计划实际产出的东西，不是任何系统的输出。故本工具：

1. 逐条执行注入计划，任一锚点未命中即报错退出（`--strict`，默认）：
   防"计划说改了、实际没改"；
2. 每个注入实例必须带一份通过核验的签名，签名的痕迹项必须满足
   "出现在该有的那一份、不出现在不该有的那一份"，核验由判定机自己做
   （`signature.materialize`），本工具不重实现；
3. 签名核验不过的实例不产出，且不是静默跳过，`--strict` 下整批失败。
   静默跳过等于让任务集悄悄缩水，而论文里的分母会跟着变，没人发现。

## 为什么判定机是外部加载的

判定机是共享冻结件（`code/vendor/signature.py`，见其 PROVENANCE）。
本工具用 `--matcher-module` 按操作者给出的路径加载它，不重实现判定：

- 重实现会让"同一套判定"这一可比性声称失效；
- 也让 `check_reuse.py` 的验证失去意义（验证的是那一份，跑的是另一份）。

## 底本与注入计划的口径

- 底本（`cases/base/<case_id>.json`）：`facts_text` 是案件事实的文本序列化，
  判定走字串比对，故必须存在文本形态。另带 `fields`（结构化字段）供溯源，
  以及 `sources`（真实出处：案号 + 链接 + 抽了哪些字段）。
- 注入：按字面文本值替换（`from` → `to`）。刻意不用结构化字段做替换，
  因为判定比的是字串：若注入改的是 `fields` 里的 ISO 日期而文本里是"年月日"格式，
  两者会静默脱钩，签名核验却可能仍通过（痕迹串从 `fields` 取、恰好也在文本里）。
  用字面值则"计划改了什么"与"文本上改了什么"必然一致。
- 痕迹：由弱点条目的 `trace` 推出。`atomic: true` 的条目取注入后的全值为
  pattern（见 弱点目录 头部的原子字段规则）；其余按条目标好的 kind 取。

## 用法

    python3 code/tools/make_taskset.py --tasks-root experiment/tasks \\
        --matcher-module <path>/signature.py
    python3 code/tools/make_taskset.py --tasks-root experiment/tasks --check   # 只校验不写盘
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 PyYAML：python3 -m pip install pyyaml\n")
    raise SystemExit(2)

HERE = Path(__file__).resolve()
PAPER_ROOT = HERE.parents[2]
# 判定机路径不写死：按 `matcher_path` 的解析顺序取（环境变量 CTD_LA_MATCHER →
# 本仓钉版副本 `code/vendor/signature.py` → 上层仓库里的共享冻结副本）。
# 写死路径会让独立导出的仓库拿到一个不存在的路径，而"文件不存在"要在运行到
# 那一步才暴露，故这里只备一个解析器，实际取值在 main() 里进行。
try:                                    # 脚本方式运行（脚本目录自动在 sys.path）
    from matcher_path import resolve as _resolve_matcher
except ImportError:                     # 以文件路径加载时脚本目录不在 sys.path
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "f1_matcher_path", str(HERE.with_name("matcher_path.py")))
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _resolve_matcher = _mod.resolve

# 任务集版本 v4：87 件弱点实例 + 64 件干净对照。读数跨版本不可比，
# v2 时代的结果文件已归档到 results/history-v2/（扩样纪律：扩样即重钉+全臂重跑）。
# v4 = v3（24 弱件 + 17 底本）+ 扩容轮批量编写（2026-09-27）：作为型不可逆
# 大幅加厚 + 可逆弱点加厚 + 纯负对照底本（无注入底本也进干净集，见 harness
# load_texts 的补丁），目标弱件 ≥80、干净件 ≥50。读数与 v3 不可比，v3 结果
# 归档 history-v3/（扩样纪律同样适用：扩样即重钉+全臂重跑）。
TASKSET_VERSION = 4


class InjectError(RuntimeError):
    pass


def load_matcher(path):
    if not path.is_file():
        raise InjectError(
            "判定机不存在：%s\n  用 --matcher-module 指定（F1 独立导出时须带钉版副本，"
            "见 code/vendor/PROVENANCE.md）" % path)
    spec = importlib.util.spec_from_file_location("ctd_la_signature_matcher", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_file(path):
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def apply_injection(facts_text, injection):
    """执行一次注入，返回 (弱点文本, 命中次数)。命中 0 次即锚点未中，调用方须报错。"""
    src, dst = injection["from"], injection["to"]
    n = facts_text.count(src)
    return facts_text.replace(src, dst), n


def _trace_terms(trace):
    """把 `trace` 归一成 term 列表（子句内各项之间是"与"）。

    支持两种写法：
    - 单个 dict：一个 term（最常见）；
    - list：多个 term，必须同时命中。

    为什么要支持 list：有些弱点的痕迹是"两个词共现"而非单个词。实例：
    `W-CONC-01`（请求权竞合）的痕迹是"违约责任"且"侵权责任"同时出现，
    只写一个 term 会让"只提到违约责任"也算命中，而那恰恰是正确做法（择一），
    把正确做法误判为命中弱点，方向正好反了。
    """
    if isinstance(trace, list):
        return trace
    return [trace]


def build_signature_spec(weakness, injection):
    """由弱点条目 + 注入算出判定用的签名声明。

    形状与既有缺陷登记表一致（`clauses` 内项间与、子句间或），
    故判定机原样吃下，这是"复用"落到实处的地方。
    """
    to_value = injection["to"]
    # `field_value`：注入改的是一段文字，而痕迹要钉的是其中一个字段的取值。
    #   两者未必相等。实测的必需场景是管辖类（W-JUR-01）：要让"选定法院"真的错，
    #   必须同时把住所地与履行地都改掉（只改一个的话，另一个连接点仍支持原法院，
    #   弱点不成立），故 `to` 是一整句、而痕迹只能是其中的住所地值。
    #   不给 `field_value` 时退回旧行为（痕迹 = 整个 `to`），既有 6 条实例不受影响。
    field_value = injection.get("field_value")
    terms = []
    for trace in _trace_terms(weakness["trace"]):
        kind = trace["kind"]
        if kind == "pattern":
            if trace.get("atomic"):
                # 原子字段：必须写全值。全值里的正则元字符要转义，
                # 日期里无碍，但案号里的括号会变成分组（实测踩点），故一律转义。
                if field_value:
                    if field_value not in to_value:
                        raise InjectError(
                            "%s：`field_value`=%r 不在注入文本 `to` 里——"
                            "痕迹与注入脱钩（改的是一处、判的是另一处）"
                            % (weakness["id"], field_value))
                    regex = re.escape(field_value)
                else:
                    regex = re.escape(to_value)
            else:
                regex = trace.get("regex")
                if regex:
                    # 非原子 pattern 写了显式 regex 时，必须验证它确实落在注入的
                    #   那段文字上（`to` 里含该模式）。不验会出两类静默失效：
                    #   ① 计划改了 A 段、痕迹却写的是 B 段，签名仍能物化成功
                    #      （只要 B 段恰好不在干净件里），于是金标准判的不是本次注入；
                    #   ② 注入文字后来被改写，痕迹 regex 忘改，任务集照旧生成。
                    if not re.search(regex, to_value):
                        raise InjectError(
                            "%s：非原子 pattern 的 regex %r 在注入文本 `to` 里没有命中——"
                            "痕迹与本次注入不对应（改的是一处、判的是另一处）"
                            % (weakness["id"], regex))
                else:
                    regex = re.escape(field_value or to_value)
            terms.append({"kind": "pattern", "regex": regex})
        elif kind == "lexeme":
            terms.append({"kind": "lexeme", "alts": list(trace["alts"])})
        else:
            raise InjectError("弱点 %s 的 trace.kind=%r 不可用于注入式签名"
                              % (weakness["id"], kind))

    # 全 lexeme 的痕迹子句会被判定机归为 definitional（不算"有痕迹根据"），
    #   于是 tier2，F1 的主口径，永远命中不了它，该实例的所有读数恒为 0。
    #   这类弱点的痕迹必须至少含一个 pattern/witness 项才能进 tier2。
    #   不报错的话症状极隐蔽：任务集照常生成、跑得通，只是那个弱点永远检不出。
    if all(t["kind"] == "lexeme" for t in terms):
        raise InjectError(
            "%s：痕迹子句全是 lexeme 项 ⇒ 判定机归其为 definitional，"
            "tier2（F1 主口径）永不命中，该弱点恒判漏检。\n"
            "  修法：让注入文本里带一段**可作 pattern 的独有措辞**，"
            "并把该措辞写成 trace 的 pattern（可参照 W-CONC-01 的写法）。"
            % weakness["id"])

    assertion = weakness["assertion"]
    assertion_term = {
        "kind": "lexeme",
        "alts": list(assertion["alts"]),
        "require_unquotable": bool(assertion.get("require_unquotable")),
    }
    return {
        "doc_scope": "target",
        "clauses": [{"terms": terms}, {"terms": [assertion_term]}],
    }


def build(matcher, tasks_root, strict=True):
    cat_path = tasks_root / "weaknesses.yaml"
    catalogue_raw = yaml.safe_load(cat_path.read_text(encoding="utf-8"))
    weaknesses = {w["id"]: w for w in (catalogue_raw.get("catalogue") or [])}
    injections = catalogue_raw.get("injections") or []

    # ── 批次片段合并（v4 扩容轮）────────────────────────────────
    # 并行作者各写各的 `batches/<批名>.yaml`（只含 `catalogue:`/`injections:`
    # 两段，schema 与本文件同构），在此按文件名序合并——多作者直接改
    # weaknesses.yaml 会互相覆盖，片段化是并行编写的前提。重复 id 一律
    # 报错：静默覆盖会让后写的吃掉先写的，任务集悄悄少实例而没人发现。
    batches_dir = tasks_root / "batches"
    batch_shas = {}
    if batches_dir.is_dir():
        for bp in sorted(batches_dir.glob("*.yaml")):
            raw = yaml.safe_load(bp.read_text(encoding="utf-8")) or {}
            for w in (raw.get("catalogue") or []):
                if w["id"] in weaknesses:
                    raise InjectError(
                        "批次 %s 的目录条目 id=%r 与已有条目重复"
                        % (bp.name, w["id"]))
                weaknesses[w["id"]] = w
            for inj in (raw.get("injections") or []):
                if any(inj.get("injection_id") == e.get("injection_id")
                       for e in injections):
                    raise InjectError(
                        "批次 %s 的 injection_id=%r 与已有注入重复"
                        % (bp.name, inj.get("injection_id")))
                injections.append(inj)
            batch_shas[bp.name] = sha256_file(bp)

    if not injections:
        raise InjectError(
            "注入计划为空（`weaknesses.yaml` 的 `injections: []`）。\n"
            "  这表示**尚无已核验的实例**。本工具拒绝产出空任务集——\n"
            "  一个 0 实例的任务集会被下游当成'跑通了'，而论文的分母是 0。\n"
            "  若确实要检查空计划，用 --allow-empty。")

    cases_dir = tasks_root / "cases"
    weak_dir = cases_dir / "weak"
    weak_dir.mkdir(parents=True, exist_ok=True)

    instances, problems = [], []
    for inj in injections:
        inj_id = inj.get("injection_id") or "(无 injection_id)"
        wid = inj.get("weakness_id")
        weakness = weaknesses.get(wid)
        if not weakness:
            problems.append("%s：weakness_id=%r 不在目录里" % (inj_id, wid))
            continue

        base_path = cases_dir / "base" / ("%s.json" % inj["base_case"])
        if not base_path.is_file():
            problems.append("%s：底本不存在 %s" % (inj_id, base_path))
            continue
        base = json.loads(base_path.read_text(encoding="utf-8"))
        clean_text = base["facts_text"]

        weak_text, hits = apply_injection(clean_text, inj)
        if hits == 0:
            problems.append(
                "%s：锚点未命中——`from`=%r 在底本 %s 的事实文本里出现 0 次。"
                "（计划改的与文本里的对不上，通常是格式不一致或多字少字）"
                % (inj_id, inj["from"], inj["base_case"]))
            continue
        if hits > 1:
            problems.append(
                "%s：锚点不唯一——`from`=%r 出现 %d 次，锚点不唯一会导致"
                "「改了哪一处」不可判定，且读起来像'改了全部'。请把 `from` 写到唯一。"
                % (inj_id, inj["from"], hits))
            continue

        spec = build_signature_spec(weakness, inj)
        try:
            signature = matcher.materialize(
                spec, {}, weak_text, clean_text, where=inj_id, extra_vars=None)
        except matcher.SignatureError as exc:
            problems.append("%s：签名核验不过——%s" % (inj_id, exc))
            continue

        weak_file = weak_dir / ("%s.json" % inj_id)
        instances.append({
            "task_id": inj_id,
            "family": inj.get("family") or base.get("family"),
            "posture": inj.get("posture"),
            "case_type": base.get("case_type"),
            "task_kind": inj.get("task_kind"),
            "base_case": inj["base_case"],
            "weak_file": str(weak_file.relative_to(tasks_root)),
            "weakness_id": wid,
            "weakness_class": weakness["class"],
            "is_t_irr": bool(weakness.get("is_t_irr")),
            "t_irr_ref": weakness.get("t_irr_ref"),
            # ── 策略层的输入（来自案件事实，不是金标准）──────────────
            # 要件事实（覆盖度的分母）与候选动作（含 covers/utility/triggers）。
            # 这三项从底本透传：它们是案件属性，像日期一样属事实层。
            # `actions[].triggers` 让盾据此判定触发了哪些 T_irr ， 这是
            #   夹具简化（真实系统应由模型从自然语言判断），
            #   已在 code/README.md 的"夹具简化"一节写明。
            "elements": (base.get("elements") or {}).get("items") or [],
            "actions": (base.get("actions") or {}).get("items") or [],
            "budget": base.get("budget") or {},
            # 时间增广分量（不作为型 T_irr 必需，见 formalization.md §1.1.1）。
            # 缺省为 None；盾在判定不作为型条目时会要求它，缺了报错而不静默放行。
            "remaining_days": (base.get("deadline") or {}).get("remaining_days"),
            # 已被当事人书面确认的动作（案件事实，不是金标准）。
            # `稳` 与 `狠` 的分歧前提：风险已知且当事人接受时，两姿态才分得开
            # （见该底本的 `confirmed._why_it_matters`）。
            "confirmed": (base.get("confirmed") or {}).get("items") or [],
            "injected": [{"weakness_id": wid, "field": inj.get("field"),
                          "from": inj["from"], "to": inj["to"],
                          "n_replacements": hits,
                          "t_irr_ref": weakness.get("t_irr_ref")}],
            "expected": {
                "signature": signature,
                "signature_desc": matcher.describe(signature),
            },
            "_weak_text": weak_text,   # 写盘时用；不进 taskset
        })

    if problems:
        raise InjectError("注入校验失败（%d 项）：\n\n" % len(problems)
                          + "\n\n".join("  ✗ " + p for p in problems))

    # 写弱点底本
    for inst in instances:
        wf = tasks_root / inst["weak_file"]
        wf.parent.mkdir(parents=True, exist_ok=True)
        wf.write_text(json.dumps({
            "task_id": inst["task_id"],
            "base_case": inst["base_case"],
            "facts_text": inst.pop("_weak_text"),
            "injected": inst["injected"],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for inst in instances:
        inst.pop("_weak_text", None)

    return {
        "taskset_version": TASKSET_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provenance": {
            "weaknesses_yaml_sha256": sha256_file(cat_path),
            "batch_fragments_sha256": batch_shas,
            "matcher_basename": Path(matcher.__file__).name if hasattr(matcher, "__file__") else None,
            "judgement_rule": (
                "弱点注入签名匹配（输出的事实引用命中该注入的可观察痕迹 + 后果断言）。"
                "**主口径取 tier2**（只算有痕迹根据的子句），"
                "理由见 weaknesses.yaml 头部：F1 的弱点类别可从实例形态猜出，"
                "含定义性子句的 tier1 会误授信未定位到痕迹的输出。"),
        },
        "n_instances": len(instances),
        "instances": instances,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks-root", default=str(PAPER_ROOT / "experiment" / "tasks"))
    ap.add_argument("--matcher-module",
                    default=os.environ.get("CTD_LA_MATCHER"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--check", action="store_true", help="只校验，不写盘")
    ap.add_argument("--allow-empty", action="store_true",
                    help="允许空注入计划（仅用于检查工具本身；默认拒绝）")
    args = ap.parse_args(argv)

    tasks_root = Path(args.tasks_root)
    if not tasks_root.is_dir():
        sys.stderr.write("任务根目录不存在：%s\n" % tasks_root)
        return 1

    try:
        matcher_path = Path(args.matcher_module) if args.matcher_module else None
        if matcher_path is None:
            matcher_path = _resolve_matcher(HERE.parent)
        if matcher_path is None:
            raise InjectError(
                "找不到注入签名判定机。用 --matcher-module 指定路径，"
                "或在 code/vendor/ 放置钉版副本（见 code/vendor/PROVENANCE.md）。")
        matcher = load_matcher(matcher_path)
    except InjectError as exc:
        sys.stderr.write("\n%s\n" % exc)
        return 2

    try:
        taskset = build(matcher, tasks_root)
    except InjectError as exc:
        if args.allow_empty and "注入计划为空" in str(exc):
            print("注入计划为空（--allow-empty 下放过）：尚无已核验实例。")
            return 0
        sys.stderr.write("\n%s\n" % exc)
        return 1

    print("=" * 70)
    print("任务集生成：%s" % tasks_root)
    print("=" * 70)
    print(" 实例 %d 条" % taskset["n_instances"])
    for inst in taskset["instances"]:
        print("  %-22s %s/%s 弱点 %s（%s）"
              % (inst["task_id"], inst["family"], inst["task_kind"],
                 inst["weakness_id"], inst["weakness_class"]))

    if args.check:
        print("\n--check：未写盘。")
        return 0

    out = Path(args.out) if args.out else tasks_root / "taskset.json"
    out.write_text(json.dumps(taskset, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print("\n已写出 %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())