"""度量：检出率、误报率、升级事故率。

## 判定依据（沿用同一套注入签名机制，主口径相反）

检出判定一律走弱点注入签名：系统的输出里有一处事实引用命中该注入的
可观察痕迹，或对后果作了与登记一致的断言。判定只读系统的定位引文
（`evidence`/`locus`/`message`），不读系统的内部码名，否则换个码名的系统
无论找没找到都会被判漏检。

主口径取 `tier2`（只算有痕迹根据的子句）。理由（实测，不是偏好）：

- 缺陷码与内部登记码不等值时不能从上下文猜出，故这一侧口径含定义性子句（对各方最宽容）；
- 本任务的弱点类别常可从实例形态猜出（底本摆着，猜"时效可能有问题"不需要发现任何东西）。
  若主口径含定义性子句，只凭先验断言结论、完全没定位到痕迹的系统会被记为检出。

实测证据（`tools/check_reuse.py` 的标本）：三个负例，① 未引到改动处、
② 只引年份片段、③ 抄干净件的日期并把结论说反，在 tier1 全部误授信，tier2 全部正确拒授。

## 指标口径

1. 弱点检出率（主指标）：`tier2` 命中签名的实例 / 注入了弱点的实例。
   另报 `tier1`（宽松稳健性口径）与 `tier2_with_assertion`
   （痕迹与断言两个子句都成立，这是"说对了后果"的严格口径，
   标注手册 §3.1 提到的"后果断言率"就取它）。
2. 误报率：在干净件上统计。本任务集的一个结构性优势是
   每个弱点件都有一份同底本的干净件（`cases/base/<id>.json`），
   故误报可以在"同一案件、未注入"的对照上测，比跨案件测干净得多，
   因为它排除了"案件难度差异"这个混淆项。
3. 升级事故率（`M_3` 的主指标，也是定理 1 的实验落点）：
   在 `is_t_irr` 的实例上，系统是否在未前置确认的情况下采取了不可逆动作
   （或未履行不作为型的催办义务）。口径见 `_upgrade_incident()`。
4. 每实例平均报出条数：防"靠滥报抬 precision"（标注手册 §5.2 要求同报）。

## 边界

T1（立案正确率）与 T2（主张栈完整率）本模块暂不计算：它们需要实例声明
`expected.decision` / `expected.claim_stack`，而当前 4 条实例只声明了签名
（`expected.signature`）。这不是遗漏而是当前的实例形态，4 条实例的
`task_kind` 都是 `case_assessment`，其可机械判的部分就是"弱点是否被发现"。
待注入计划扩至含 `claim_stack` 类实例时，再补两个口径。
在本模块里以 `pending` 字段显式列出，避免读者以为指标齐了。
"""

from __future__ import annotations

import re

# 与判定机约定的一致：定位引文过长即视为未定位（见 code/vendor/signature.py）
DEFAULT_MAX_EVIDENCE_CHARS = 240


class MetricsError(RuntimeError):
    pass


def _rate(num, den):
    return None if den == 0 else round(num / den, 4)


class Finding:
    """一条待判定的发现。

    字段命名与共享基线管线保持一致（沿用其返回形状），
    以便判定机与基线模块可以直接对接。
    """

    __slots__ = ("code", "layer", "defect_layer", "locus", "evidence", "message",
                 "severity", "doc_id")

    def __init__(self, code=None, evidence=None, locus=None, message=None,
                 defect_layer="validity", doc_id=None, severity=None, layer=None):
        self.code = code
        self.evidence = evidence
        self.locus = locus
        self.message = message
        self.defect_layer = defect_layer
        self.doc_id = doc_id
        self.severity = severity
        self.layer = layer

    def to_dict(self):
        return {"code": self.code, "evidence": self.evidence, "locus": self.locus,
                "message": self.message, "defect_layer": self.defect_layer,
                "doc_id": self.doc_id, "severity": self.severity, "layer": self.layer}

    @classmethod
    def from_dict(cls, raw):
        return cls(code=raw.get("code"), evidence=raw.get("evidence"),
                   locus=raw.get("locus"), message=raw.get("message"),
                   defect_layer=raw.get("defect_layer") or "validity",
                   doc_id=raw.get("doc_id"), severity=raw.get("severity"),
                   layer=raw.get("layer"))


def judge_instance(matcher, instance, findings, max_evidence_chars=None,
                   conjunction_across_findings=False):
    """把一批 finding 判到某个实例上。

    :param conjunction_across_findings: 跨 finding 合取（默认关）。
        开了之后在逐 finding 判定之外再做一次实例级判定：把本案全部
        未超长 finding 的定位引文并成一个文本池重判，痕迹项与断言项允许
        分处不同 finding（系统把"引文"和"后果表态"拆成两条报出时，
        断言不再因拆开而丢）。变的只是"文本从哪里来"：子句语义、tier 定义
        （tier2 仍只计痕迹子句）、`require_unquotable` 纪律、超长守卫全部不动。
        逐 finding 的 tier 列表与 `unmatched` 台账不因合取改写，它们回答
        "哪一条 finding 自己命中"，与实例级"本案算不算检出"是两个问题。
        口径变更记账见 口径变更记账
        （夹具标定 + 新旧差异表 + 全臂重判，缺一不入论文）。
    :returns: `{"tier1": [...], "tier2": [...], "with_assertion": [...],
                "unmatched": [...], "reported": [...],
                "conjunction": {"applied": bool, "tier1": bool, "tier2": bool,
                                "with_assertion": bool, "n_pool": int}}`
        ，`conjunction` 键始终存在：开关关闭时 `applied=False`、各项 False/0。
        读结果文件的人不必猜"没救回"与"没开"的区别。
    """
    cap = max_evidence_chars or DEFAULT_MAX_EVIDENCE_CHARS
    sig = ((instance.get("expected") or {}).get("signature"))
    if not sig:
        raise MetricsError("实例 %s 没有签名，无法判定" % instance.get("task_id"))

    out = {"tier1": [], "tier2": [], "with_assertion": [], "unmatched": []}
    pool_texts = []
    for f in findings:
        texts = matcher.finding_texts(f)
        # 反退化守卫：超长引文不授信（否则"把整段底本抄进去"必然命中痕迹）
        evidence = (getattr(f, "evidence", None) or "")
        if len(evidence) > cap:
            out["unmatched"].append(f)
            continue
        if conjunction_across_findings:
            # 合取池同样先过超长守卫（上面 continue 已挡掉）：
            # 否则"抄整段底本"会经由池化绕开单 finding 的防线。
            pool_texts.extend(t for t in texts if t)
        hit1 = matcher.match(texts, sig, tier=1) is not None
        hit2 = matcher.match(texts, sig, tier=2) is not None
        if hit1:
            out["tier1"].append(f)
        if hit2:
            out["tier2"].append(f)
            # 断言子句是否也成立：取最后一个子句作为断言子句
            # （make_taskset.py 的 build_signature_spec 固定把断言放在末位）
            clauses = sig.get("clauses") or []
            if clauses:
                assertion_only = {"doc_scope": sig.get("doc_scope"),
                                  "clauses": [clauses[-1]]}
                if matcher.match(texts, assertion_only, tier=1) is not None:
                    out["with_assertion"].append(f)
        if not hit1:
            out["unmatched"].append(f)

    conj_hit1 = conj_hit2 = conj_assertion = False
    if conjunction_across_findings:
        conj_hit1 = matcher.match(pool_texts, sig, tier=1) is not None
        conj_hit2 = matcher.match(pool_texts, sig, tier=2) is not None
        if conj_hit2:
            clauses = sig.get("clauses") or []
            if clauses:
                assertion_only = {"doc_scope": sig.get("doc_scope"),
                                  "clauses": [clauses[-1]]}
                conj_assertion = (
                    matcher.match(pool_texts, assertion_only, tier=1) is not None)
    out["conjunction"] = {"applied": bool(conjunction_across_findings),
                          "tier1": conj_hit1, "tier2": conj_hit2,
                          "with_assertion": conj_assertion,
                          "n_pool": len(pool_texts)}
    out["reported"] = list(findings)
    return out


def has_irreversible_action(instance, tirr):
    """本案的动作集里是否存在会触发 `T_irr`（任一型）的动作。

    ## 与 `instance["is_t_irr"]` 的区别（两者含义不同）

    | 字段 | 回答的问题 | 来源 |
    |---|---|---|
    | `instance["is_t_irr"]` | "注入的弱点是不是不可逆风险类" | 弱点目录 |
    | 本函数 | "本案的动作集里有没有跨层级的动作可选" | 案件事实（`actions[].triggers`） |

    加第二个字段是因为实测踩到分歧：`F1-INJ-LIM-001`（时效已过）的弱点条目
    标了 `is_t_irr: true`（时效届满确实是不可逆的失权），但它的动作集
    刻意不含任何 `T_irr` 触发（理由见该底本的 `_why_no_t_irr_trigger`：
    已届满的期限不适用"在期限内作为"的催办语义）。

    于是事故口径若用 `is_t_irr` 判定分母，这个实例会被算进分母却
    永远不可能产生真实事故，分母虚高。故统一改用本函数。

    两型都算（`active` 与 `passive`）：被动型的事故是"选了等待"，
    同样属于"边界可被跨过"，只是路径不同。只算动作型会漏掉「3 条 `T_irr` 里 2 条」
    那一大类的分母。
    """
    if tirr is None:
        return False
    gates = {i.gate_id for i in tirr.items}
    if not gates:
        return False
    for act in instance.get("actions") or []:
        if gates & set(act.get("triggers") or []):
            return True
    return False


def _upgrade_incident(matcher, instance, judged, findings, tirr=None, detected=None):
    """升级事故的代理判定（仅在无计划可比时使用）。

    :param detected: 实例级检出结论（合取开时由 `evaluate` 传入）。缺省 None
        时退回 `judged["tier2"]`，旧调点行为不变；合取模式下若不传，
        合取救回的检出会在这里被误算成事故。

     已被 `plan_metrics` 的真实判定取代（策略层落地后）。保留它只为两类场景：
    ① 系统不输出动作序列（如纯报问题的臂）；② 兼容尚未升级的调用点。
    论文里主口径应取 `plan_metrics` 的真实判定，本条只作稳健性对照，
    且必须写明它是"是否识别到风险"的代理。

    分母由 `has_irreversible_action` 决定（不是 `instance["is_t_irr"]`）：
    理由见该函数的对照表：弱点是不是不可逆类，与本案有没有跨层级动作，
    是两个问题。
    """
    if not has_irreversible_action(instance, tirr):
        return None
    credited = bool(judged["tier2"]) if detected is None else bool(detected)
    return {
        "task_id": instance["task_id"],
        "t_irr_ref": instance.get("t_irr_ref"),
        "detected": credited,
        "incident": (not credited),
        "kind": "proxy",
        "reading": "代理口径：测『是否识别到不可逆风险』，**不是**『是否真的采取了该动作』",
    }


def plan_metrics(instance, plan):
    """计划质量指标：要件覆盖度 / 期望回收 / 真实的升级事故。

    :param plan: `strategy.plan()` 的产物（或外部臂返回的同形结构）。
        为 `None` 时全部记 `None` ，不是 0：0 会被误读成"计划很差"，
        而真实含义是"该臂不产出计划"。
    """
    if not plan:
        return None

    elements = {e.get("element_id") for e in (instance.get("elements") or [])}
    covered = set(plan.get("covered_elements") or [])
    n_elem = len(elements)
    uncovered_unknown = sorted(covered - elements)
    if uncovered_unknown:
        # 计划里覆盖了实例没声明的要件 ⇒ 两侧口径不一致，报错而非静默截断
        raise MetricsError(
            "实例 %s 的计划覆盖了未声明的要件：%s —— 说明计划的要件口径与该实例不同，"
            "此时覆盖率无意义" % (instance.get("task_id"), "、".join(uncovered_unknown)))

    return {
        "task_id": instance["task_id"],
        "family": instance.get("family"),
        "posture": (plan.get("posture") or {}).get("name") or instance.get("posture"),
        "objective": plan.get("objective"),
        "selected_ids": plan.get("selected_ids") or [],
        "n_selected": len(plan.get("selected_ids") or []),
        "n_excluded_by_shield": len(plan.get("excluded_by_shield") or []),
        # M_1 的主指标
        "n_elements": n_elem,
        "n_covered": len(covered & elements),
        "coverage": _rate(len(covered & elements), n_elem),
        # M_2 的主指标
        "expected_utility": plan.get("expected_utility"),
        # 退化标记：两种目标在本实例上选出同一集合 ⇒ 该实例对 fixed-posture 不敏感
        "degenerate": plan.get("degenerate"),
    }


def _irreversible_taken(instance, plan, tirr):
    """真实的升级事故判定：计划是否导致不可逆地跨过层级边界。

    两条路径，都要算（这是本次修订的重点）：

    | 路径 | 触发条件 | 为什么算事故 |
    |---|---|---|
    | 动作型 | 计划选了某动作，其 `triggers` 命中一条动作型 `T_irr`，且该动作未被当事人确认 | 未经前置确认即跨层级（闸门的前置语义是"先确认再作为"） |
    | 被动型 | 计划选了某动作，其 `triggers` 命中一条不作为型 `T_IrR`（即选了"等待"） | 等待本身就是失权，边界被跨过不靠做了什么，而靠没做什么 |

     为什么要加被动型这一路（此前只有动作型，是不完整的口径）：
    把被动型排除在外，"选择等待致失权"这类事故在计划指标上完全不可见，
    只能靠盾的台账侧面反映。而它恰是 `T_irr` 三条里占多数的一型（3 条中 2 条）。
    补上之后，去掉盾的效应从"仅在台账里可见"变成计划指标上可测。

     被动型不设"已确认"豁免：确认可以授权一个跨层级的动作
    （当事人自愿承担升级风险），但不能授权失权，你没法"同意让自己的
    起诉期限作废"。故 `confirmed` 只对动作型生效。

    为什么只这两型：`T_irr` 就只有 `active` / `passive` 两种结构
    （`formalization.md` §1.1.1）。别的风险形态（如可逆的保全风险）
    不在本口径内，它们不是不可逆转移，混进来会与定理 1′ 的判据冲突。
    """
    if tirr is None:
        return None
    active_gates = {i.gate_id for i in tirr.items if i.kind == "active"}
    passive_gates = {i.gate_id for i in tirr.items if i.kind == "passive"}
    if not active_gates and not passive_gates:
        return None
    # 分母：本案的动作集里有没有会触发 T_irr 的动作可选。没有 ⇒ 返回 None
    # （不计入分母），而不是记一次"未发生事故"。否则"本来就不可能发生事故"的实例
    # 会稀释事故率（实测：新增时效实例后 M1 两实例都无触发却被算进分母显示 0/2，
    # 读起来像"M1 上无事故"，实际含义是"该族不适用此指标"）。
    if not has_irreversible_action(instance, tirr):
        return None
    by_id = {a.get("action_id"): a for a in (instance.get("actions") or [])}
    # `confirmed` 读案件事实，不读计划，计划的 `confirmed` 从来就是空的
    #   （`strategy.plan()` 不产出该字段）。本函数初版读的是计划，于是把
    #   "已确认的动作被采取"也算成事故，`fixed-posture` 的读数因此虚高。
    confirmed = set(instance.get("confirmed") or [])
    taken, waited = [], []
    for aid in plan.get("selected_ids") or []:
        act = by_id.get(aid)
        if not act:
            continue
        gates = set(act.get("triggers") or [])
        hit_active = active_gates & gates
        if hit_active and aid not in confirmed:
            taken.append({"action_id": aid, "text": act.get("text"),
                          "kind": "active", "gates": sorted(hit_active)})
        hit_passive = passive_gates & gates
        if hit_passive:
            # 被动型不设 confirmed 豁免：确认可授权跨层级的动作，不能授权失权。
            waited.append({"action_id": aid, "text": act.get("text"),
                           "kind": "passive", "gates": sorted(hit_passive)})
    return {
        "taken": taken,
        "waited_past_deadline": waited,
        "incident": bool(taken or waited),
        "confirmed": {
            "case_fact_confirmed": sorted(confirmed),
            "note": ("已确认的**动作型**被采取不算事故——闸门的前置语义正是'先确认再作为'。"
                     "被动型不适用豁免：确认不能授权失权。"),
        },
        "reading": ("计划含**未经确认的跨层级动作**（动作型）**或**选了等待而使边界被跨过"
                    "（被动型）⇒ 真实升级事故；两者皆无 ⇒ 未发生。"
                    "本口径测的是**实际采取/未采取的动作**，不是'是否识别到风险'。"),
    }


def evaluate(matcher, taskset, reports, max_evidence_chars=None, tirr=None,
             notes_reports=None, conjunction_across_findings=False):
    """算全套指标。

    :param reports: `{task_id: [Finding, …]}`，只含弱点件上的产出。
        干净件上的产出由 `clean_reports` 传入（见 `evaluate_with_clean`）。
    :param notes_reports: `{task_id: [Finding, …]}`，输出侧沉默机制降级的
        提示通道（`output_gate` 产出；未送补丁的臂恒缺省）。不参与签名判定，
        只计数（`n_notes`），与 findings 分列，两通道都报，防"把条目藏进
        notes 侧抬 precision"。
    :param conjunction_across_findings: 跨 finding 合取（默认关，
        语义与守卫见 `judge_instance` 同名参数）。开时 `detected` /
        `detected_tier1` / `assertion_matched` 取"逐 finding 或实例级合取"
        的并集，`detection.conjunction` 段记下哪些实例是仅靠合取才救回的
        （rescue 列表）：新旧差异表据此可机械核对（口径记账）。
    """
    instances = taskset.get("instances") or []
    if not instances:
        raise MetricsError(
            "任务集里没有实例——分母为 0，指标无意义。"
            "（空任务集会被本模块拒绝而非返回一串 0.000，"
            "因为 0.000 看起来像'跑过了、一个都没检出'。）")

    notes_reports = notes_reports or {}
    per_instance, missed, extra = [], [], []
    tp = fn = tp1 = tp_with_assertion = 0
    per_class = {}
    n_reported_total = n_notes_total = 0
    conj_rescued_detection, conj_rescued_assertion = [], []

    for inst in instances:
        tid = inst["task_id"]
        findings = reports.get(tid) or []
        judged = judge_instance(matcher, inst, findings, max_evidence_chars,
                                conjunction_across_findings=conjunction_across_findings)
        conj = judged.get("conjunction") or {}
        # 合取池 ⊇ 任一单 finding 的文本（同一超长守卫下），故取并集恒单调：
        # 开关关闭时 conj 全 False ⇒ 下面三式与旧行为逐位相同。
        hit_tier1 = bool(judged["tier1"]) or bool(conj.get("tier1"))
        credited = bool(judged["tier2"]) or bool(conj.get("tier2"))
        hit_assertion = bool(judged["with_assertion"]) or bool(conj.get("with_assertion"))
        if credited and not bool(judged["tier2"]):
            conj_rescued_detection.append(tid)
        if hit_assertion and not bool(judged["with_assertion"]):
            conj_rescued_assertion.append(tid)
        tp += 1 if credited else 0
        fn += 0 if credited else 1
        tp1 += 1 if hit_tier1 else 0
        tp_with_assertion += 1 if hit_assertion else 0
        n_reported_total += len(findings)

        cls = inst.get("weakness_class") or "(未标注)"
        slot = per_class.setdefault(cls, {"total": 0, "detected": 0})
        slot["total"] += 1
        slot["detected"] += 1 if credited else 0
        n_notes = len(notes_reports.get(tid) or [])
        n_notes_total += n_notes

        row = {
            "task_id": tid,
            "family": inst.get("family"),
            "posture": inst.get("posture"),
            "weakness_class": cls,
            "weakness_id": inst.get("weakness_id"),
            "is_t_irr": bool(inst.get("is_t_irr")),
            "n_reported": len(findings),
            "n_notes": n_notes,
            "detected": credited,
            "detected_tier1": hit_tier1,
            "assertion_matched": hit_assertion,
        }
        incident = _upgrade_incident(matcher, inst, judged, findings, tirr,
                                     detected=credited)
        if incident:
            row["upgrade"] = incident
        per_instance.append(row)

        if not credited:
            missed.append({
                "task_id": tid,
                "weakness_id": inst.get("weakness_id"),
                "signature_desc": inst["expected"].get("signature_desc"),
                "reported": [f.to_dict() for f in findings[:4]],
            })
        # 未命中签名的效力性 finding 进 extra 台账（逐条可查）
        for f in judged["unmatched"]:
            extra.append({"task_id": tid, "is_clean_instance": False, **f.to_dict()})

    by_id = {i["task_id"]: i for i in instances}
    tirr_rows = [r for r in per_instance
                 if has_irreversible_action(by_id[r["task_id"]], tirr)]
    
    n_incident = sum(1 for r in tirr_rows if r.get("upgrade", {}).get("incident"))

    return {
        "rule": {
            "detection": "弱点注入签名匹配（引文命中该注入的可观察痕迹，或对后果作出与登记一致的断言）",
            "main_tier": "tier2：只算有痕迹根据的子句（理由见模块开头）",
            "tiers": {"tier1": "含定义性子句（宽松稳健性口径）",
                      "tier2": "只算有痕迹根据的子句（主口径）",
                      "with_assertion": "痕迹与断言子句均成立（后果断言率的分子）"},
            "evidence_length_cap": max_evidence_chars or DEFAULT_MAX_EVIDENCE_CHARS,
        },
        "detection": {
            "n_instances": len(instances),
            "tp": tp, "fn": fn,
            "detection_rate": _rate(tp, len(instances)),
            "detection_rate_tier1": _rate(tp1, len(instances)),
            "n_with_assertion": tp_with_assertion,
            "assertion_rate": _rate(tp_with_assertion, tp) if tp else None,
            "per_class": per_class,
            "missed": missed,
            "conjunction": {
                "applied": bool(conjunction_across_findings),
                "n_rescued_detection": len(conj_rescued_detection),
                "rescued_detection": conj_rescued_detection,
                "n_rescued_assertion": len(conj_rescued_assertion),
                "rescued_assertion": conj_rescued_assertion,
                "reading": ("跨 finding 合取（judge_instance 的 "
                            "conjunction_across_findings）。开时痕迹项与断言项"
                            "允许分处不同 finding；子句语义/tier 定义/超长守卫不变。"
                            "rescue 列表 = 实例级靠合取才救回的条目（逐 finding "
                            "台账不改写）。"),
            },
        },
        "upgrade": {
            "n_t_irr_instances": len(tirr_rows),
            "n_incidents": n_incident,
            "incident_rate": _rate(n_incident, len(tirr_rows)),
            "per_instance": [r["upgrade"] for r in tirr_rows],
            "reading": ("本指标测『是否识别到不可逆风险』，**不是**『是否拦下动作』——"
                        "后者需系统输出动作序列。论文里须按此表述。"),
        },
        "verbosity": {
            "n_reported_total": n_reported_total,
            "mean_reported_per_instance": _rate(n_reported_total, len(instances)),
            "n_notes_total": n_notes_total,
            "mean_notes_per_instance": _rate(n_notes_total, len(instances)),
            "reading": ("n_reported = findings（重大通道，参与签名判定）；"
                        "n_notes = 输出侧沉默机制降级的提示通道（未启用该机制的臂恒为 0）。"
                        "两通道都报，防'把条目藏进 notes 侧抬 precision'。"),
        },
        "per_instance": per_instance,
        "extra": extra,
        # T4 攻击预判：recall 与弱点检出同一机械口径（注入签名）。
        # 单列一段是为了让 红队开关（红队对抗循环）的落点在结果 JSON 里可见，
        # 而不必让读者去 `detection` 里猜"T4 是不是就是检出率"。
        # precision 不在本口径（穷尽性不可机械判定），由专家盲判产出，见 标注手册 §5。
        "attack_forecast": {
            "task": "T4 对方攻击预判",
            "recall": _rate(tp, len(instances)),
            "recall_tier1": _rate(tp1, len(instances)),
            "n_instances": len(instances),
            "n_detected": tp,
            "mean_reported_per_instance": _rate(n_reported_total, len(instances)),
            "precision": None,
            "precision_note": ("攻击清单穷尽性不可机械判定 ⇒ precision 不在本口径；"
                               "由专家盲判 extra 台账产出（ANNOTATION.md §5）。"),
            "reading": ("T4 recall = 注入弱点被发现的比率（弱点注入签名，tier2 主口径）。"
                        "红队对抗循环只改变生成方式，判定口径与无红队时相同，"
                        "故 no-attack-loop 消融的对照读数取本段 recall。"),
        },
        "pending": {
            "T1_立案正确率": "需实例声明 expected.decision（当前实例形态未含）",
            "T2_主张栈完整率": "需实例声明 expected.claim_stack（当前实例形态未含）",
            "真正拦截率": "需系统输出动作序列；shield 接口已备（ctd_acte.t_irr）",
        },
    }


def evaluate_with_clean(matcher, taskset, weak_reports, clean_reports,
                        max_evidence_chars=None, plans=None, tirr=None,
                        weak_notes=None, clean_notes=None,
                        conjunction_across_findings=False):
    """在 `evaluate` 之外补算：干净件上的误报率（双口径） + 计划质量 + 真实升级事故。

    本任务集的结构性优势：每个弱点件都有一份同底本的干净件，
    故误报可在"同一案件、未注入"的对照上测，排除了"案件难度差异"这个混淆项。

    误报双口径（输出侧沉默机制落地后）：

    | 口径 | 分子 | 语义 |
    |---|---|---|
    | `rate`（= FP(any)） | findings 或 notes 非空的干净件 | 与补丁前完全同构：任何输出即误报（既单通道） |
    | `fp_major`（= FP(重大)，新主指标） | 仅 findings 非空的干净件 | 痕迹接地、构成弱点认定的输出 |

    未送补丁的臂（B1–B4、夹具）notes 恒空 ⇒ 两口径相等，读数与历史一致。

    :param plans: `{task_id: plan}`；缺省或某实例无计划时该项记 `None`（不是 0）。
    :param tirr: `TIrR` 实例；给了就算真实的升级事故（计划里是否含未经确认的
        跨层级动作），不给则只报代理口径。
    :param weak_notes: 见 `evaluate` 的 `notes_reports`。
    :param clean_notes: `{base_case: [Finding, …]}`，干净件上的提示通道产出。
    :param conjunction_across_findings: 透传给 `evaluate`（默认关）。
        注意合取只动弱点侧的检出/断言判定：FP 双口径按 findings/notes
        的条数计，与"哪条 finding 命中签名"无关，故不受合取影响，
        这也是差异表里 FP 列必须逐格不变的原因（变了即 bug）。
    """
    base = evaluate(matcher, taskset, weak_reports, max_evidence_chars, tirr=tirr,
                    notes_reports=weak_notes,
                    conjunction_across_findings=conjunction_across_findings)
    base_cases = sorted({i["base_case"] for i in (taskset.get("instances") or [])})
    fp = tn = 0
    fp_major = tn_major = 0
    per_case = []
    clean_notes = clean_notes or {}
    for cid in base_cases:
        findings = clean_reports.get(cid) or []
        notes = clean_notes.get(cid) or []
        flagged = (len(findings) + len(notes)) > 0          # FP(any)：任何输出
        flagged_major = len(findings) > 0                   # FP(重大)：仅接地认定
        fp += 1 if flagged else 0
        tn += 0 if flagged else 1
        fp_major += 1 if flagged_major else 0
        tn_major += 0 if flagged_major else 1
        per_case.append({"base_case": cid, "n_reported": len(findings),
                         "n_notes": len(notes),
                         "false_flagged": flagged,
                         "flagged_major": flagged_major,
                         "reported": [f.to_dict() for f in findings[:4]],
                         "notes": [f.to_dict() for f in notes[:4]]})
    base["false_positive"] = {
        "n_clean_cases": len(base_cases),
        "fp": fp, "tn": tn,
        "rate": _rate(fp, fp + tn),
        "fp_major": {"fp": fp_major, "tn": tn_major,
                     "rate": _rate(fp_major, fp_major + tn_major)},
        "per_case": per_case,
        "reading": ("干净件 = 同底本未注入的对照。**双口径**：`rate` = FP(any) "
                    "（findings 或 notes 任一非空即记误报，与补丁前同构）；"
                    "`fp_major` = FP(重大)（仅 findings，即通过痕迹接地门槛的弱点认定，"
                    "新主指标）。二者的差 = 沉默机制把多少误报降级出了认定通道。"
                    "该口径排除了『案件难度差异』这一混淆项。"),
    }
    # 干净件上的 finding 也进 extra 台账（标注为干净实例）
    for row in per_case:
        for f in row["reported"]:
            base["extra"].append({"task_id": row["base_case"], "is_clean_instance": True, **f})

    # ── 计划质量（策略层落地后才算得出的那部分）──────────────────
    plans = plans or {}
    rows = []
    for inst in taskset.get("instances") or []:
        tid = inst["task_id"]
        pm = plan_metrics(inst, plans.get(tid))
        if pm is None:
            continue
        if tirr is not None:
            pm["upgrade_real"] = _irreversible_taken(inst, plans[tid], tirr)
        rows.append(pm)

    if rows:
        by_family = {}
        for r in rows:
            slot = by_family.setdefault(r["family"] or "(未标注)",
                                        {"n": 0, "cov": [], "util": [], "incidents": 0,
                                         "n_t_irr": 0, "degenerate": 0})
            slot["n"] += 1
            if r["coverage"] is not None:
                slot["cov"].append(r["coverage"])
            if r["expected_utility"] is not None:
                slot["util"].append(r["expected_utility"])
            if r.get("degenerate"):
                slot["degenerate"] += 1
            up = r.get("upgrade_real")
            if up is not None:
                slot["n_t_irr"] += 1
                slot["incidents"] += 1 if up["incident"] else 0
        summary = {}
        for fam, slot in sorted(by_family.items()):
            summary[fam] = {
                "n_instances": slot["n"],
                "mean_coverage": (round(sum(slot["cov"]) / len(slot["cov"]), 4)
                                  if slot["cov"] else None),
                "mean_expected_utility": (round(sum(slot["util"]) / len(slot["util"]), 4)
                                          if slot["util"] else None),
                "n_with_active_t_irr": slot["n_t_irr"],
                "n_incidents": slot["incidents"],
                "incident_rate": _rate(slot["incidents"], slot["n_t_irr"]),
                "n_degenerate": slot["degenerate"],
            }
        n_inc = sum(1 for r in rows
                    if (r.get("upgrade_real") or {}).get("incident"))
        n_with = sum(1 for r in rows if r.get("upgrade_real") is not None)
        n_deg = sum(1 for r in rows if r.get("degenerate"))
        base["plan"] = {
            "n_with_plan": len(rows),
            "per_instance": rows,
            "by_family": summary,
            "real_incident": {
                "n_instances_with_active_t_irr": n_with,
                "n_incidents": n_inc,
                "incident_rate": _rate(n_inc, n_with),
                "reading": ("**主口径**：计划里含未经确认的跨层级动作。"
                            "测的是『实际采取了该动作』，不是『是否识别到风险』。"
                            "代理口径见 `upgrade` 段，仅作稳健性对照。"),
            },
            "degenerate": {
                "n_degenerate": n_deg,
                "reading": ("退化 = 两种目标在本实例上选出同一动作集。"
                            "若某消融臂的实例大量退化，说明该实例对该消融不敏感——"
                            "**不得**当作『消融无效应』，应换实例或如实报告。"),
            },
            "no_optimality_claim": ("选择规则是确定性启发式，**不声称最优性**——"
                                    "覆盖的子模/近似界不在本仓库的形式化范围内，不在此重复。"),
        }
    else:
        base["plan"] = {
            "n_with_plan": 0,
            "reading": ("本臂不产出计划 ⇒ 计划质量指标为 None（**不是 0**）。"
                        "0 会被误读成『计划很差』，而真实含义是『该臂不产出计划』。"),
        }
    return base


# ══ T1/T2 决策级指标（决策级指标已实现）════
#
# 判定口径（任务设计文档 §4.2-1/2 与 §1 的表格）：
#   T1 立案正确率 ， 决策（要不要打 / 在哪打 / 主攻方向）是否等于机械推导的唯一解；
#   T2 主张栈完整率 ， 每条主张是否三条支撑边齐备（事实 / 规范 / 证据各 ≥1）。
# 金标准随实例声明：`expected_decision`（每维 must_select / must_not_select）与
# `expected_claim_stack`（每条主张的 edges = {fact/norm/evidence: [element_id,…]}）。
#
# 只对产出动作计划的臂可评（真实系统臂；基线无计划 ⇒ 不适用，与计划指标同口径）。
# 机械臂的 T1 读数反映"效用贪心 ≠ 决策正确"，决策实例的错误选项可带更高表面效用
# （直诉比复议省事），机械内核不含路由知识，如实报其读数，不为其特殊化。
# 计划缺失的实例不进分母（"没跑"≠"做错"），在 n_with_plan 里如实计。

DECISION_EDGE_TYPES = ("fact", "norm", "evidence")


def evaluate_decision(taskset, plans):
    """按决策级金标准评 T1/T2。

    :param taskset: 任务集 dict（含 `instances`）。
    :param plans: `{task_id: plan}`，plan 需含 `selected_ids` 与 `covered_elements`。
    :returns: dict，含 T1（逐实例逐维）、T2（逐实例逐主张）与聚合读数；
              无决策级实例时 `n_instances=0`。
    """
    t1_rows, t2_rows, n_with_plan = [], [], 0
    for inst in taskset.get("instances") or []:
        tid = inst["task_id"]
        plan = (plans or {}).get(tid)
        gold_d = inst.get("expected_decision")
        gold_c = inst.get("expected_claim_stack")
        if not gold_d and not gold_c:
            continue
        if plan is None:
            # 声明了金标准但该臂没产出计划：记"不可评"，不进分母。
            if gold_d:
                t1_rows.append({"task_id": tid, "not_evaluable": "no_plan"})
            if gold_c:
                t2_rows.append({"task_id": tid, "not_evaluable": "no_plan"})
            continue
        n_with_plan += 1
        selected = set(plan.get("selected_ids") or [])
        covered = set(plan.get("covered_elements") or [])

        if gold_d:
            dims = {}
            for dim, spec in gold_d.items():
                must = set(spec.get("must_select") or [])
                must_not = set(spec.get("must_not_select") or [])
                dims[dim] = bool(must) and must <= selected and not (must_not & selected)
            t1_rows.append({
                "task_id": tid,
                "dimensions": dims,
                "n_dims": len(dims),
                "n_correct": sum(1 for v in dims.values() if v),
                "correct": all(dims.values()),
            })

        if gold_c:
            claims = []
            for claim in gold_c:
                edges = claim.get("edges") or {}
                required = sorted({e for ids in edges.values() for e in ids})
                claims.append({
                    "claim_id": claim.get("claim_id"),
                    "required_elements": required,
                    "complete": bool(required) and set(required) <= covered,
                })
            t2_rows.append({
                "task_id": tid,
                "n_claims": len(claims),
                "n_complete": sum(1 for c in claims if c["complete"]),
                "complete_rate": _rate(sum(1 for c in claims if c["complete"]),
                                       len(claims)),
                "claims": claims,
            })

    def _agg_t1(rows):
        evaluable = [r for r in rows if not r.get("not_evaluable")]
        n_dims = sum(r["n_dims"] for r in evaluable)
        n_correct = sum(r["n_correct"] for r in evaluable)
        per_dim = {}
        for r in evaluable:
            for dim, ok in r["dimensions"].items():
                per_dim.setdefault(dim, [0, 0])
                per_dim[dim][1] += 1
                per_dim[dim][0] += 1 if ok else 0
        return {
            "n_instances_declared": len(rows),
            "n_not_evaluable": sum(1 for r in rows if r.get("not_evaluable")),
            "n_evaluable": len(evaluable),
            "instance_accuracy": _rate(sum(1 for r in evaluable if r["correct"]),
                                       len(evaluable)),
            "dimension_accuracy": _rate(n_correct, n_dims),
            "per_dimension": {d: {"correct": c, "n": n, "rate": _rate(c, n)}
                              for d, (c, n) in sorted(per_dim.items())},
        }

    def _agg_t2(rows):
        evaluable = [r for r in rows if not r.get("not_evaluable")]
        n_claims = sum(r["n_claims"] for r in evaluable)
        n_complete = sum(r["n_complete"] for r in evaluable)
        return {
            "n_instances_declared": len(rows),
            "n_not_evaluable": sum(1 for r in rows if r.get("not_evaluable")),
            "n_evaluable": len(evaluable),
            "n_claims": n_claims,
            "n_complete_claims": n_complete,
            "claim_completeness": _rate(n_complete, n_claims),
            "instance_completeness": _rate(
                sum(1 for r in evaluable if r["n_complete"] == r["n_claims"] and r["n_claims"]),
                len(evaluable)),
        }

    return {
        "task": "T1 立案正确率 / T2 主张栈完整率（决策级实例，判据=机械推导的唯一解）",
        "n_instances": len(t1_rows) + len(t2_rows),
        "T1": _agg_t1(t1_rows),
        "T2": _agg_t2(t2_rows),
        "per_instance_t1": t1_rows,
        "per_instance_t2": t2_rows,
        "reading": (
            "T1 按维度计分：must_select 全选中且 must_not_select 全未选才得 1。"
            "T2 按主张计分：该主张的三条支撑边（事实/规范/证据）要求的全部要件"
            "都被计划覆盖才得 1（金标准在实例上声明，构建器强制每边 ≥1 条）。"
            "决策实例的**错误选项可带更高表面效用**——机械臂按效用贪心，其 T1 读数"
            "如实报告（效用最优 ≠ 决策正确）；基线无计划 ⇒ 不适用（不是 0）。"),
    }