#!/usr/bin/env python3
"""盲判材料包生成 + 计分（T4 precision / κ / 真实额外发现）。

## 与既有文书质检盲标协议的差别（协议不同，不能照抄）

既有协议的盲标问的是"这条 finding 有没有报出我们注入的那处缺陷"，标注者要看注入
diff（改动本身就是金标准来源）。本评测的协议（标注手册 §5）相反：

- 标注者看不到任何注入信息，只看到案情文本 + 系统逐条报出，回答
  "这条攻击/风险在本案中是否真实成立"（成立 / 不成立 / 说不清 + 一句理由）；
- κ 是标注者之间的（两人同题）；既有协议比的是"标注者 vs 规则"；
- 判"成立"而注入签名未命中的条目 = 真实额外发现：机械判定原理上产不出的
  信息（这是盲标相对机械判定在评审证据上的增量，见 §5.2）。

## 盲化三件事（破一件，precision 就会被质疑）

1. 系统身份隐去：两臂的报出混在同一案件下、顺序按盲标哈希打乱；臂名只进
   `_internal.json`（不交给标注者）。
2. 实例编号匿名：`task_id` 的后缀（如 `-SCOPE-001`）直接泄露弱点类别，
   `-INJ-` 泄露"这是注入件"，故标注者只看到 `K01…` 乱序码，映射只进 internal。
3. 姿态、注入计划、签名、检出结论一概不出现，出现任何一样，
   "判成立"就变成"复述我们知道的事"。

## 抽样（预注册，与读数无关）

全量两臂约 300+ 条，超出 §10.1 的 100–200 条标注预算。抽样以实例为单位、
按层（弱点类 / 干净）在盲标哈希序里取前 K，确定性、可复现、只依赖实例本身，
不依赖臂身份也不依赖读数高低（事后挑样本才是问题，事前定好不是）。
选中的实例两臂都进包（配对：红队开关 的 precision 对照必须同案可比）。

## 用法（在 papers/F1-adjudication/ 下）

    # 1) 生成盲判稿（需两臂会话产物已就位）
    python3 code/tools/make_blind_pack.py --packet \
        --arm 主臂=experiment/run/acte-sessions \
        --arm 无红队=experiment/run/acte-sessions-no-attack \
        --results 主臂=实验结果目录 实验结果目录 experiment/results/（随实验材料发布）（随实验材料发布）acte-model.json \
        --results 无红队=实验结果目录 实验结果目录 experiment/results/（随实验材料发布）（随实验材料发布）abl-no-attack-loop.json \
        --out-dir experiment/blind

    # 2) 标注者填完（一人或两人各一份 answers.csv）后计分
    python3 code/tools/make_blind_pack.py --score experiment/blind/answers_1.csv[,answers_2.csv]

计分产出（对应 标注手册 §5.1–§5.2）：κ（两人时；单人显式记"单标注者判定"）、
按臂的 T4 precision（命中注入条目机械入分子；其余按标注者"成立/不成立"对表）、
真实额外发现清单与不成立条目全文台账，§5.2 要求的"平均报出"从 --results
的全量读数取（不由抽样外推）。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
PAPER_ROOT = HERE.parents[2]
CODE_ROOT = PAPER_ROOT / "code"
DEFAULT_TASKSET = PAPER_ROOT / "experiment" / "tasks" / "taskset.json"
DEFAULT_ARM_MODULE = CODE_ROOT / "tools" / "arm_acte_model.py"
DEFAULT_OUT = PAPER_ROOT / "experiment" / "blind"

SEED = 20260923            # 固定种子：盲标序与抽样可复现，改一次就换一个批次号
LABELS = ("成立", "不成立", "说不清")
LABEL_ALIASES = {"成立": "成立", "是": "成立", "yes": "成立",
                 "不成立": "不成立", "否": "不成立", "no": "不成立",
                 "说不清": "说不清", "": ""}


class PackError(RuntimeError):
    pass


# ── 模块加载（判定机与 harness 同一入口，不另写一份） ──────────────
def _load_module(path, alias):
    path = Path(path)
    if not path.is_file():
        raise PackError("模块不存在：%s" % path)
    spec = importlib.util.spec_from_file_location(alias, str(path))
    mod = importlib.util.module_from_spec(spec)
    code_dir = str(CODE_ROOT)
    sys.path.insert(0, code_dir)
    try:
        spec.loader.exec_module(mod)
    finally:
        if sys.path and sys.path[0] == code_dir:
            sys.path.pop(0)
    return mod


def _load_harness():
    return _load_module(CODE_ROOT / "harness.py", "f1_harness_blind")


def _default_matcher():
    # 与 harness 同一条解析器取：环境变量 → 本仓钉版副本 → 上层仓库内的冻结副本。
    # 按文件路径加载解析器：本模块可能被测试套件以文件路径加载，
    # 那时脚本目录不在 sys.path 上，`import matcher_path` 会失败。
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "f1_matcher_path", str(HERE.with_name("matcher_path.py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = module.resolve(CODE_ROOT)
    if found is None:
        raise PackError("找不到判定机；用 --matcher 显式给出，"
                        "或在 code/vendor/ 放置钉版副本")
    return found


def _blind_hash(*parts):
    return hashlib.sha256(
        "|".join([str(SEED)] + [str(p) for p in parts]).encode("utf-8")).hexdigest()


# ── 从会话产物取各臂 findings（复用 harness 的外部臂路径） ─────────
def collect_arm(harness, arm_module_path, label, run_dir, taskset, weak, clean):
    """跑一遍外部臂取 findings，与计分时同一段代码，避免两处口径漂移。

    会话失败/缺产物时臂会抛错（arm 的缺测纪律），这里不兜底：盲判稿里
    少一个实例比"默默少一题"好，后者要在标注者交卷后才会被发现。
    """
    run_dir = Path(run_dir)
    if not (run_dir / "manifest.tsv").is_file():
        raise PackError("%s 缺 manifest.tsv（这不是一个会话产物目录）" % run_dir)
    os.environ["ACTE_RUN_DIR"] = str(run_dir)
    os.environ.pop("ACTE_ARM_LOG", None)          # 盲判生成不写臂日志
    try:
        mod = _load_module(arm_module_path, "f1_arm_blind_%s" % abs(hash(label)))
        # run_external_arm 自输出侧沉默机制落地起返回 5 值（多出 findings/notes 两条通道）；
        # 盲判稿按 v1 口径只收 findings（重大通道）：notes 是输出侧降级的
        # 提示通道，进包属口径变更，须先过口径记账（暂不做）。
        reports, clean_reports, _plans, _wn, _cn = harness.run_external_arm(
            mod, label, taskset, weak, clean, Path(DEFAULT_TASKSET).parent)
    finally:
        os.environ.pop("ACTE_RUN_DIR", None)
    return reports, clean_reports


# ── 抽样：以实例为单位、按层哈希序取 K（两臂同进同出，保配对） ─────
def select_instances(taskset, k_weak, k_clean):
    strata = {}
    for inst in taskset["instances"]:
        cls = inst.get("weakness_class") or "(未标注)"
        strata.setdefault(cls, []).append(inst["task_id"])
    bases = sorted({i["base_case"] for i in taskset["instances"]})
    strata["(干净)"] = list(bases)

    picked, report = [], []
    for name in sorted(strata):
        ids = sorted(strata[name], key=lambda t: _blind_hash(name, t))
        k = k_clean if name == "(干净)" else k_weak
        take = ids[:k]
        picked.extend(take)
        report.append((name, len(take), len(ids), take))
    return picked, report


def anon_codes(picked):
    """实例 → K01…（按盲标哈希序编号；编号不携带类别信息）。"""
    ordered = sorted(picked, key=lambda t: _blind_hash("anon", t))
    return {tid: "K%02d" % (i + 1) for i, tid in enumerate(ordered)}


# ── 生成盲判稿 ────────────────────────────────────────────────────
def build_packet(args):
    harness = _load_harness()
    arm_mod = Path(args.arm_module)
    matcher = _load_module(args.matcher or _default_matcher(), "f1_matcher_blind")
    metrics_mod = _load_module(CODE_ROOT / "ctd_acte" / "metrics.py", "f1_metrics_blind")

    tp = Path(args.taskset)
    taskset, tasks_root = harness.load_taskset(tp if tp.is_dir() else tp.parent)
    weak_texts, clean_texts = harness.load_texts(tasks_root, taskset)

    arms = []
    for spec in args.arm or []:
        if "=" not in spec:
            raise PackError("--arm 需要 标签=会话产物目录，得到：%s" % spec)
        label, rd = spec.split("=", 1)
        arms.append((label, rd))
    if not arms:
        raise PackError("至少给一个 --arm 标签=run_dir")
    if len(arms) > 4:
        raise PackError("臂太多（%d）——盲判包最多装 4 臂" % len(arms))

    # 全臂先收集（任一臂缺产物立刻炸，不产出残缺包）
    arm_findings = {}
    for label, rd in arms:
        reports, clean_reports = collect_arm(
            harness, arm_mod, label, rd, taskset, weak_texts, clean_texts)
        arm_findings[label] = (reports, clean_reports)

    # 可选：与结果文件对 n_reported（防解析口径漂移，两次解析必须一致）
    for spec in args.results or []:
        if "=" not in spec:
            raise PackError("--results 需要 标签=结果JSON")
        label, path = spec.split("=", 1)
        if label not in arm_findings:
            raise PackError("--results 的标签没有对应臂：%s" % label)
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        block = next(iter(doc.get("systems").values()))
        by_id = {r["task_id"]: r["n_reported"] for r in block.get("per_instance") or []}
        reports, clean_reports = arm_findings[label]
        bad = []
        for tid, got in by_id.items():
            want = len(reports.get(tid) or [])
            if got != want:
                bad.append("%s：结果 %s / 重解析 %s" % (tid, got, want))
        if bad:
            raise PackError(
                "%s 与会话产物解析结果不一致（口径漂移，先查 parse_issues_f1）：%s"
                % (path, "；".join(bad[:5])))

    picked, stratum_report = select_instances(
        taskset, args.sample_weak, args.sample_clean) if not args.all else (
        [i["task_id"] for i in taskset["instances"]]
        + sorted({i["base_case"] for i in taskset["instances"]}), None)
    codes = anon_codes(picked)
    inst_by_id = {i["task_id"]: i for i in taskset["instances"]}
    base_to_insts = {}
    for i in taskset["instances"]:
        base_to_insts.setdefault(i["base_case"], []).append(i)

    # 组装条目：一个实例下，两臂 findings 混排（哈希序），臂身份只进 internal
    sections, items = [], []
    for tid in sorted(picked, key=lambda t: _blind_hash("order", t)):
        is_clean = tid not in inst_by_id
        if is_clean:
            facts = clean_texts[tid]
        else:
            facts = weak_texts[tid]
        rows = []
        for label, _ in arms:
            reports, clean_reports = arm_findings[label]
            fnd = (clean_reports.get(tid) if is_clean
                   else reports.get(tid)) or []
            # 同臂内完全相同的报出去重（解析偶尔产重复行；跨臂不去重，
            # 两臂各报一条相似内容正是要分别判的对象）
            seen = set()
            uniq = []
            for f in fnd:
                key = (f.evidence or "", f.message or "")
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(f)
            for f in uniq:
                rows.append((label, f))
        rows.sort(key=lambda lf: _blind_hash("item", tid, lf[0],
                                             lf[1].evidence or "", lf[1].message or ""))
        if not rows:
            continue

        # 签名判定（弱点件才有注入可命中；干净件的"成立"= 夹具发现）
        rule_tier2 = {}
        if not is_clean:
            inst = inst_by_id[tid]
            judged = metrics_mod.judge_instance(matcher, inst,
                                                [f for _, f in rows])
            # judge_instance 按 findings 列表顺序回 membership；用对象身份对齐
            tier2_ids = {id(f) for f in judged["tier2"]}
            rule_tier2 = {i: (id(rows[i][1]) in tier2_ids)
                          for i in range(len(rows))}

        sec_items = []
        for idx, (label, f) in enumerate(rows, 1):
            item = {
                "item_id": "A%04d" % (len(items) + 1),
                "k_code": codes[tid],
                "_task_id": tid,
                "_arm": label,
                "_kind": "clean" if is_clean else "weak",
                "_rule_tier2": bool(rule_tier2.get(idx - 1, False)),
                "_stratum": "(干净)" if is_clean else
                            (inst_by_id[tid].get("weakness_class") or "(未标注)"),
                "evidence": f.evidence or "",
                "message": f.message or "",
            }
            items.append(item)
            sec_items.append(item)
        sections.append({"k_code": codes[tid], "facts": facts,
                         "items": sec_items,
                         "_task_id": tid, "_kind": "clean" if is_clean else "weak"})

    if not items:
        raise PackError("0 题——臂没产出任何 findings？先查会话产物")
    n_weak_items = sum(1 for it in items if it["_kind"] == "weak")
    n_clean_items = len(items) - n_weak_items
    budget_hint = ""
    if len(items) > 240:
        budget_hint = ("  ⚠ 已超 §10.1 的 100–200 条预算——调低 --sample-* 再生成。")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── packet.md（交标注者） ────────────────────────────────────
    md = [
        "# F1 盲判稿：这些风险/攻击在本案中是否真实成立？", "",
        "每一条报出请回答一个问题：**它在本案中是否真实成立？**", "",
        "- **成立**：案情支持这条风险/攻击确实存在（措辞是否漂亮不重要）；",
        "- **不成立**：与案情不符、无据夸大、或本案不存在该风险；",
        "- **说不清**：仅凭案情无法判断。**判不了就填「说不清」，不必勉强**",
        "  （会被单独统计，不计入 κ）。", "",
        "每条请给**一句理由**（理由进台账，可复核）。", "",
        "- 你看到的只有案情与系统报出；逐条就案情本身判断，不要去猜题目设计；",
        "- 系统身份已隐去，同案相似的报出可能来自不同系统，**分别判**；",
        "- 案件编号 K## 与系统身份无对应关系，不要试图反推。", "",
        "答卷：`answers.csv`（填 **label** 与 **理由** 两列，Excel/WPS 直接打开）"
        " 或 `answers.json`（填 `label`/`reason` 字段），二选一。", "",
        "共 %d 条（其中弱点件 %d 条 / 干净件 %d 条），来自 %d 个案件切片。%s"
        % (len(items), n_weak_items, n_clean_items, len(sections), budget_hint), "",
    ]
    if stratum_report:
        md += ["## 抽样口径（**生成之前定好的，不是看完读数挑的**）", "",
               "按**弱点类 / 干净**分层，每层按盲标哈希序取前 K"
               "（确定性、可复现；只依赖实例本身，不依赖臂身份与读数）。"
               "选中的实例**两臂报出都进包**（同案配对）。", "",
               "| 层 | 抽取 | 全量 |", "|---|---|---|"]
        for name, got, total, _ids in stratum_report:
            md.append("| %s | %d | %d |" % (name, got, total))
        md.append("")
    for sec in sections:
        md += ["---", "", "## 案件 %s" % sec["k_code"], "",
               "案情：", "", sec["facts"], "", "**系统报出（逐条判）**", ""]
        for i, it in enumerate(sec["items"], 1):
            md += ["**%d.** 引文：%s" % (i, _clip(it["evidence"])), "",
                   "　　说明：%s" % _clip(it["message"]), "",
                   "　判定：〔 成立 / 不成立 / 说不清 〕　理由：", ""]
        md.append("")
    (out_dir / "packet.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # ── 答卷（csv 给 Excel 标注者；json 给程序化回填） ─────────────
    answers = {"annotator": "",
               "instructions": "每条填 label（成立/不成立/说不清）与 reason（一句理由）",
               "batch_seed": SEED,
               "items": [{"item_id": it["item_id"], "label": "", "reason": ""}
                         for it in items]}
    (out_dir / "answers.json").write_text(
        json.dumps(answers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["item_id", "案件编号", "引文", "说明", "label", "理由"])
    for it in items:
        w.writerow([it["item_id"], it["k_code"], _clip(it["evidence"], 200),
                    _clip(it["message"], 300), "", ""])
    # utf-8-sig：用 Excel/WPS 打开中文不乱码（与既有导出同一取法）
    (out_dir / "answers.csv").write_text(buf.getvalue(), encoding="utf-8-sig")

    # ── internal（不交标注者）：臂身份 + 实例映射 + 规则判定 ──
    (out_dir / "_internal.json").write_text(json.dumps(
        {"seed": SEED,
         "arms": [label for label, _ in arms],
         "sampling": {"sample_weak": args.sample_weak,
                      "sample_clean": args.sample_clean, "all": bool(args.all)},
         "sections": [{"k_code": s["k_code"], "task_id": s["_task_id"],
                       "kind": s["_kind"]} for s in sections],
         "items": [{k: v for k, v in it.items()} for it in items]},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("盲判稿已写出（%d 题 / %d 案件切片）：" % (len(items), len(sections)))
    print("  %s" % (out_dir / "packet.md"))
    print("  %s   （交给标注者填：Excel 用 csv，或用 json）" % (out_dir / "answers.csv"))
    print("  %s   （交给标注者填）" % (out_dir / "answers.json"))
    print("  %s   （**不要**给标注者：臂身份/实例映射/规则判定）"
          % (out_dir / "_internal.json"))
    if budget_hint:
        print(budget_hint)
    return 0


def _clip(text, limit=160):
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[:limit] + "…"


# ── 计分：κ（标注者之间）+ 按臂 precision + 两个台账 ──────────────
def _load_answers(path):
    """csv/json 两种答卷都收；label 同义词归一（成立/是…）。"""
    p = Path(path)
    if p.suffix.lower() != ".csv":
        doc = json.loads(p.read_text(encoding="utf-8"))
        rows = [{"item_id": str(it.get("item_id") or "").strip(),
                 "label": (it.get("label") or "").strip(),
                 "reason": (it.get("reason") or "").strip()}
                for it in doc.get("items") or []]
        return rows
    rows = []
    with p.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            label = reason = ""
            for key in ("label", "判定", "判定结果"):
                if row.get(key):
                    label = str(row[key]).strip()
                    break
            for key in ("理由", "reason"):
                if row.get(key):
                    reason = str(row[key]).strip()
                    break
            rows.append({"item_id": (row.get("item_id") or "").strip(),
                         "label": label, "reason": reason})
    return rows


def _norm(label):
    return LABEL_ALIASES.get((label or "").strip().lower(),
                             LABEL_ALIASES.get((label or "").strip(), ""))


def _kappa(pairs):
    n = len(pairs)
    if n == 0:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    p = sum(1 for a, _ in pairs if a == "成立") / n
    q = sum(1 for _, b in pairs if b == "成立") / n
    pe = p * q + (1 - p) * (1 - q)
    k = (po - pe) / (1 - pe) if pe != 1 else float("nan")
    return po, pe, k, n


def _fmt_k(v):
    if v is None or v != v:
        return "—"
    return "%.3f" % v


def score(args):
    # 包目录三顺位：显式 --packet-dir ＞ 答卷所在目录 ＞ 默认包目录。
    # 答卷常被标注者拷走另存（不在包目录）：回落并显式打印用了哪一个
    # 。
    if args.packet_dir:
        packet_dir = Path(args.packet_dir)
    else:
        packet_dir = Path(args.answers[0]).parent
        if not (packet_dir / "_internal.json").is_file() and \
                (Path(DEFAULT_OUT) / "_internal.json").is_file():
            print("注：答卷目录 %s 无 _internal.json，改用默认包目录 %s"
                  % (packet_dir, DEFAULT_OUT))
            packet_dir = Path(DEFAULT_OUT)
    internal_path = packet_dir / "_internal.json"
    if not internal_path.is_file():
        raise PackError("缺 _internal.json（先 --packet 生成）；"
                        "答卷与包不在同一目录时用 --packet-dir 指定包目录")
    internal = json.loads(internal_path.read_text(encoding="utf-8"))
    by_item = {it["item_id"]: it for it in internal["items"]}

    annotators = []
    for path in args.answers:
        rows = _load_answers(path)
        lab = {}
        for r in rows:
            v = _norm(r["label"])
            if not v:
                continue
            if r["item_id"] in by_item:
                lab[r["item_id"]] = (v, r.get("reason") or "")
        annotators.append((Path(path).name, lab))
        missing = [i for i in by_item if i not in lab]
        if missing:
            print("⚠ %s 未填 %d 题（未计入）：%s"
                  % (Path(path).name, len(missing), "、".join(sorted(missing)[:8])))

    # ── κ：标注者之间（§5.1.4）────────────────────────────────────
    if len(annotators) == 2:
        (n1, a1), (n2, a2) = annotators
        ids = [i for i in by_item if i in a1 and i in a2
               and a1[i][0] in ("成立", "不成立") and a2[i][0] in ("成立", "不成立")]
        unclear = sum(1 for i in by_item
                      if (i in a1 and a1[i][0] == "说不清")
                      or (i in a2 and a2[i][0] == "说不清"))
        pairs = [(a1[i][0], a2[i][0]) for i in ids]
        got = _kappa(pairs)
        print()
        print("可计分 %d 题（任一方「说不清」%d 题不计入）" % (len(ids), unclear))
        if got:
            po, pe, k, _ = got
            print("一致率 po = %.3f ｜ 期望一致率 pe = %.3f ｜ **Cohen's κ = %s**"
                  % (po, pe, _fmt_k(k)))
        for arm in internal["arms"]:
            sub = [(a1[i][0], a2[i][0]) for i in ids
                   if by_item[i]["_arm"] == arm]
            g = _kappa(sub)
            if g:
                print("  分层 κ［%s］：n=%d  po=%.3f  κ=%s"
                      % (arm, g[3], g[0], _fmt_k(g[2])))
        disagree = [(i, a1[i][0], a2[i][0]) for i in ids if a1[i][0] != a2[i][0]]
        if disagree:
            print("分歧 %d 题（§6：分歧本身是要报告的结果，不得多数决掩盖）："
                  % len(disagree))
            for i, x, y in disagree[:10]:
                print("  %s：%s=%s / %s=%s" % (i, n1, x, n2, y))
        else:
            print("零分歧。")
    else:
        print()
        print("**单标注者判定**（§5.1.4）：只有一份答卷 ⇒ 无一致性数据，"
              "论文中须照此写明。")

    # ── 按臂 precision（弱点件；§5.1.3 对表规则）＋台账 ───────────
    judged_true_extras, judged_rejected, judged_clean_true = [], [], []
    print()
    print("T4 precision（弱点件；命中注入的条目机械入分子——对表由本工具做）")
    print("  臂        分子=命中注入+判成立(非注入)   分母(判了的)  说不清   precision")
    for arm in internal["arms"]:
        items_arm = [it for it in internal["items"]
                     if it["_arm"] == arm and it["_kind"] == "weak"]
        for who, lab in annotators:
            n_hit = n_true_extra = n_false = n_unclear = n_blank = 0
            for it in items_arm:
                j = lab.get(it["item_id"])
                label = j[0] if j else ""
                if it["_rule_tier2"]:
                    n_hit += 1
                    if label == "不成立":
                        judged_rejected.append(_row(it, who, j, note="与注入命中冲突"))
                    continue
                if label == "成立":
                    n_true_extra += 1
                    judged_true_extras.append(_row(it, who, j))
                elif label == "不成立":
                    n_false += 1
                    judged_rejected.append(_row(it, who, j))
                elif label == "说不清":
                    n_unclear += 1
                else:
                    n_blank += 1
            num = n_hit + n_true_extra
            den = num + n_false
            # 未填守卫：答卷全空时，非命中条目既不进
            #   分子也不进分母，precision 会退化成"机械命中/机械命中 = 1.000"，
            #   一个看似漂亮实则空转的数。此时显式记不可用，不打数字。
            if den == n_hit and (n_blank or n_unclear):
                print("  %-8s %-4s  **precision 不可用**——非命中条目 %d 题"
                      "全为空/说不清（未填 %d、说不清 %d）；机械命中 %d 条单列"
                      "（命中注入不依赖标注，进 recall 侧对表）"
                      % (arm, who, len(items_arm) - n_hit, n_blank, n_unclear, n_hit))
                continue
            prec = ("%.3f" % (num / den)) if den else "—"
            prec_u = ("%.3f" % (num / (den + n_unclear))) if (den + n_unclear) else "—"
            print("  %-8s %-4s  %4d = %3d + %3d            %4d        %3d   %s"
                  % (arm, who, num, n_hit, n_true_extra, den, n_unclear, prec))
            extra_bits = []
            if n_unclear:
                extra_bits.append("「说不清」计入分母 ⇒ %s" % prec_u)
            if n_blank:
                extra_bits.append("**未填 %d 题不计**（答卷不完整）" % n_blank)
            if extra_bits:
                print("    （%s）" % "；".join(extra_bits))

    # §5.2 必须同时报的两条：不成立全文台账 + 平均报出（全量，不外推）
    # 干净件上被标注者判"成立"的条目：机械口径一律记误报，但判成立意味着
    # 底本可能仍有真实风险 ⇒ 候选夹具发现（§6 第三行：入台账并在局限节列出）。
    for it in internal["items"]:
        if it["_kind"] != "clean":
            continue
        for who, lab in annotators:
            j = lab.get(it["item_id"])
            if j and j[0] == "成立":
                judged_clean_true.append(_row(it, who, j))
    (packet_dir / "_judged_true_extras.json").write_text(
        json.dumps({"note": "判成立且注入签名未命中 = **真实额外发现**（机械判定产不出）",
                    "rows": judged_true_extras,
                    "clean_flagged_note": "干净件上判成立 = 候选夹具发现（§6 局限节）",
                    "clean_flagged": judged_clean_true},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (packet_dir / "_judged_rejected.json").write_text(
        json.dumps({"note": "标注者判不成立的条目全文（§5.2 #2 防滥报抬 precision）",
                    "rows": judged_rejected}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print()
    print("台账已写出：真实额外发现 %d 条 → %s"
          % (len(judged_true_extras), packet_dir / "_judged_true_extras.json"))
    print("            判不成立     %d 条 → %s"
          % (len(judged_rejected), packet_dir / "_judged_rejected.json"))

    if args.results:
        print()
        print("平均报出（**全量**读数，§5.2 #1——不由抽样外推）：")
        for spec in args.results:
            label, path = spec.split("=", 1) if "=" in spec else ("?", spec)
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
            block = next(iter(doc.get("systems").values()))
            af = block.get("attack_forecast") or {}
            print("  %-8s %s 条/实例" % (label, af.get("mean_reported_per_instance")))
    else:
        print()
        print("⚠ 未给 --results：§5.2 #1 的「平均报出」没报——补 results 标签=JSON。")
    return 0


def _row(it, who, j, note=""):
    return {"item_id": it["item_id"], "annotator": who, "k_code": it["k_code"],
            "task_id": it["_task_id"], "arm": it["_arm"], "kind": it["_kind"],
            "rule_tier2": it["_rule_tier2"],
            "label": j[0] if j else "", "reason": (j[1] if j else ""), "note": note,
            "evidence": it["evidence"], "message": it["message"]}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--packet", action="store_true", help="生成盲判稿")
    ap.add_argument("--score", nargs="+", metavar="ANSWERS",
                    help="标注者答卷（1 或 2 份，csv/json）；同目录须有 _internal.json，"
                         "不在同目录时配 --packet-dir")
    ap.add_argument("--packet-dir", default=None,
                    help="盲判稿所在目录（答卷被拷走另存时指定；默认先看答卷所在目录、"
                         "再回落 experiment/blind/）")
    ap.add_argument("--arm", action="append", default=[],
                    metavar="标签=run_dir", help="要入包的臂（可重复；两臂配对）")
    ap.add_argument("--results", action="append", default=[],
                    metavar="标签=JSON", help="对 n_reported + 报平均报出（可重复）")
    ap.add_argument("--taskset", default=str(DEFAULT_TASKSET))
    ap.add_argument("--arm-module", default=str(DEFAULT_ARM_MODULE))
    ap.add_argument("--matcher", default=None, help="判定机路径（缺省按 matcher_path 解析）")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--sample-weak", type=int, default=1,
                    help="每弱点类抽几个实例（默认 1；两臂同进同出）")
    ap.add_argument("--sample-clean", type=int, default=2,
                    help="抽几个干净底本（默认 2）")
    ap.add_argument("--all", action="store_true", help="全量入包（不抽样）")
    args = ap.parse_args(argv)

    try:
        if args.score:
            args.answers = args.score
            return score(args)
        if args.packet:
            return build_packet(args)
        raise PackError("请给 --packet 或 --score（见 --help）")
    except PackError as exc:
        sys.stderr.write("错误：%s\n" % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
