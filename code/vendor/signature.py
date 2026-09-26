"""注入签名：把"注入计划留下的可观察痕迹"变成可机械比对的检出判定依据。

## 为什么要有这个模块（旧规则错在哪）

此前的判定是**缺陷码等值**：``expected.validity ∩ reported_codes``。这等于要求每个
被测系统都使用我们的内部缺陷码。本文方法与通用质检共用一套码，问题看不出来；但共享
基线 B1–B4 报的是自由文本，其码形如 ``arm_issue:…``（见 `impl/baselines/arms.py`），
与内部码永不相等——于是**不论基线是否真的找到了缺陷，效力性检出率恒为 0.000**。

实测证据：B1 就 K1 案的跨文书不一致报出了"两份文书对同一合同的编号记载不一致……
影响文书的法律效力"，措辞、定位都对，旧规则仍计为漏检。所以旧规则测的是"术语是否
相同"，不是"缺陷是否被找到"。这个度量口径必须换掉，否则整套对比（含消融、含 B1–B4
矩阵）在结构上不可解释。

## 新规则：注入签名

对每个注入实例，金标准给出一个**签名**——"该缺陷在文本上留下的可观察痕迹"：

- **witness（见证串）**：插入/替换类的见证串必须**出现在缺陷件、且不出现在干净件**
  （``role: insert``）；删除类的见证串必须**只出现在干净件**（``role: delete``）。
- **pattern（模式）**：标点类这类"痕迹是位置而不是字串"的缺陷，用正则刻画，
  核验器要求它**匹配缺陷件、且不匹配干净件**。
- **lexeme（定义性词表）**：关系类缺陷（如"两份文书对某字段的记载不一致"）在文本上
  未必留下独有字串，其判定依据是法律要件本身的措辞（"不一致""不符""矛盾"…）。
  这类词表**不是** diff 导出的，因此单独标出、单独归入宽松层（见"两层聚合"）。

**判定规则（一句话）**：系统的定位引文里含有**一段连续文字**，它出自该注入的痕迹串，
**且在"不该有它的那份文书"里找不到**。核验器在生成期就把这些片段算好写进任务集
（``runs``），判定时只做包含测试——没有阈值、没有可调参数，审阅者可以直接在
`taskset.json` 里看到"到底什么算命中"。

片段粒度取 4 字（``MIN_RUN``）：改动往往只差一两个字（合同编号 YH-2023-0315 →
YH-2023-0415），要求整串命中会把正确的定位判成漏检；4 字连续相同在中文里已不可能是
巧合，且刚好跨过差异处。不足 4 字的见证串（错别字"依发"）取整串。

**任何系统一律按同一规则判定**：本文方法、本篇的通用质检对照、共享基线 B1–B4，
判定只读系统报出的**定位引文**（``evidence`` / ``locus`` / ``message``），不读缺陷码。

## 两层聚合（同一规则，两种口径）

一个子句（clause）由若干项（term）构成，项间"与"、子句间"或"。子句分为：

- **grounded（有痕迹根据）**：只由 witness / pattern 构成——"引文落在注入改动上"。
- **definitional（定义性）**：含 lexeme 项——"用了该缺陷类的要件措辞"。

``tier1``（宽松，主口径）计所有子句；``tier2``（严格）只计 grounded 子句。
两者同时报告：tier1 与 tier2 之差，恰好说明"系统说对了类别但没有指到改动本身"
还是"确实指到了"。主指标用 tier1（对各方最宽容，且与缺陷码无关），
tier2 作为稳健性口径。

## 反退化守卫

定义性词表里的词可能本来就在文书里出现（如"合同编号"），理论上"把整篇文书当引文"
可以蒙混。故设三道守卫：(1) 定位引文超过 ``MAX_EVIDENCE_CHARS`` 的 finding 不授信
（真实的定位引文是短的，本文方法的 evidence 本就截到 160 字以内）；(2) 干净实例上的
效力性 finding 一律计入误报——蒙混者在干净件上无处可蒙；(3) 各系统的输出格式均要求
"一条问题一行、附原文片段"，见各臂提示词。守卫一、二对称适用于所有系统。
"""

from __future__ import annotations

import re
import unicodedata

# 定位引文的长度上限：超过即视为"没定位"，不授信（见模块开头"反退化守卫"）
MAX_EVIDENCE_CHARS = 240

# 判定用的最短连续片段（汉字字数）：4 字。中文里 4 字连续相同已不可能是巧合，
# 且严格跨越词边界；作为"这段文字确实出自该痕迹"的最小证据足够。
# （长度不足 4 字的见证串取整串——如错别字"依发"，必须整串命中。）
MIN_RUN = 4

_WITNESS = "witness"
_PATTERN = "pattern"
_LEXEME = "lexeme"

# 见证串模板占位符 → 注入算子里的字段名
_TEMPLATE_FIELDS = {
    "{to}": "to",
    "{replace}": "replace",
    "{find}": "find",
    "{section}": "section",
    "{body}": "body",
    "{text}": "text",
    "{anchor}": "anchor",
    "{mark}": "mark",
}


class SignatureError(RuntimeError):
    """签名与注入计划不一致（见证串没有痕迹根据、片段在"不该有它的文书"里能找到等）。"""


def normalize(text):
    """归一化：NFC + 去掉所有空白。

    去空白是必要的——模型引文常把原句换行或加空格，逐字节比对会把正确的定位判成漏检。
    标点保留（标点类缺陷的痕迹就是标点本身）。
    """
    if not text:
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", str(text)))


# 词表项按**整词**匹配（曾用"alt 的任意 ≥min_len 子串"匹配，实测会误授信：
# alt 里的"合同编号"被 finding 里的"合同"满足，于是"合同应为…与当事人角色矛盾"这种
# 与跨文书矛盾毫无关系的报告被算成检出。缩短形式不必靠切词——`alts` 里本来就把
# "编号""案号"等变体逐一列出来了。）


def witness_runs(alt, absent_norm):
    """见证串在"不该有它的那份文书"里找不到的连续片段（判定用）。

    对见证串 `alt` 取长度 ``MIN_RUN`` 的滑窗（整串短于 ``MIN_RUN`` 时取整串），
    保留那些在 `absent_norm` 里**找不到**的片段。命中这些片段之一 ⇒ 引文含有一段
    "不可能从那份文书里抄到"的文字 ⇒ 确实指到了本次注入。

    为什么不是"取最长可用片段"：改动往往只差一两个字（合同编号 YH-2023-0315 →
    YH-2023-0415），要求整串命中会把正确的定位判成漏检；要求 4 字则刚好跨过差异处。
    每个滑窗是否"找得到"都是直接比对出来的，故这里没有任何需要调的门槛。
    """
    n = len(alt)
    if n == 0:
        return []
    width = min(n, MIN_RUN)
    runs = []
    for i in range(n - width + 1):
        piece = alt[i:i + width]
        if piece not in absent_norm and piece not in runs:
            runs.append(piece)
    return runs


# ── 由注入算子取模板变量的实际字面量 ────────────────────────────
def mutation_vars(mutation, extra=None):
    """把注入算子里的字段取出来，供 ``{to}``/``{find}`` 之类占位符代入。"""
    out = {}
    for var, field in _TEMPLATE_FIELDS.items():
        if field in mutation:
            out[var] = str(mutation[field])
    for key, value in (extra or {}).items():
        out["{" + key + "}"] = str(value)
    return out


def _render(template, variables, where):
    out = template
    for var, value in variables.items():
        out = out.replace(var, value)
    if "{" in out and "}" in out:
        raise SignatureError("签名模板有未代入的占位符：{!r}（{}）".format(template, where))
    return out


# ── 物化 + 核验 ─────────────────────────────────────────────────
def materialize(spec, mutation, defect_text, clean_text, where, extra_vars=None):
    """把签名声明（`defects.yaml` 的 ``signature``）物化成可判定的数据，并逐项核验。

    :param spec: 签名声明
    :param mutation: 该实例的注入算子
    :param defect_text: 注入后的目标文书全文
    :param clean_text: 应当"不含该痕迹"的对照文本（本例取同案其他文书 + 该文书注入前全文）
    :param where: 报错定位用（如 ``"K1-01-complaint__V-name-blacklist"``）
    :returns: ``{"doc_scope":…, "clauses":[{…}]}``
    :raises SignatureError: 见证串无痕迹根据、或 ``min_len`` 未超过巧合下限
    """
    variables = mutation_vars(mutation, extra_vars)
    defect_norm = normalize(defect_text)
    clean_norm = normalize(clean_text)
    clauses = []
    dropped_clauses = []

    for idx, clause_spec in enumerate(spec.get("clauses") or []):
        where_clause = "{} 第 {} 子句".format(where, idx + 1)
        terms = []
        grounded = True
        dropped = []
        for term_spec in clause_spec.get("terms") or []:
            try:
                term = _materialize_term(
                    term_spec, variables, defect_norm, clean_norm, where_clause
                )
            except SignatureError as exc:
                # 显式声明 optional_grounding 的子句：见证串没有痕迹根据时**降级为
                # 定义性判定**并如实记录原因，而不是把整套任务集卡住。
                # 未声明的子句一律报错——静默放宽等于把金标准悄悄调松。
                if not clause_spec.get("optional_grounding"):
                    raise
                dropped.append({"spec": term_spec, "reason": str(exc)})
                grounded = False
                continue
            terms.append(term)
        if not terms:
            # 只可能发生在 optional_grounding 子句（其余子句的项一有问题就抛错了）。
            # 把整条子句摘掉并如实记录——否则会留下一条永远不成立、又看不出来的子句。
            dropped_clauses.append({
                "clause": idx + 1,
                "reason": dropped[0]["reason"] if dropped else "全部项都不可用",
            })
            continue
        if all(t["kind"] == _LEXEME for t in terms):
            # 整条子句全靠定义性词表 ⇒ 不算"有痕迹根据"（tier2 不计）。
            # 注意**不能**写成"含有任一词表项就不算"：跨文书类的子句是
            # 「引到改动处 **且** 明确断言不一致」——它确实依赖痕迹，只是额外要求
            # 系统把"不一致"说出来。否则这一类永远进不了 tier2。
            grounded = False
        clauses.append({"grounded": grounded, "terms": terms, "dropped_terms": dropped})

    if not clauses:
        raise SignatureError("{}：签名没有任何子句".format(where))
    return {
        "doc_scope": spec.get("doc_scope") or "target",
        "clauses": clauses,
        "dropped_clauses": dropped_clauses,
    }


def _materialize_term(term_spec, variables, defect_norm, clean_norm, where):
    kind = term_spec.get("kind") or _WITNESS
    if kind == _PATTERN:
        return _materialize_pattern(term_spec, where, defect_norm, clean_norm)
    if kind == _LEXEME:
        return _materialize_lexeme(term_spec, variables, where, defect_norm)
    return _materialize_witness(term_spec, variables, defect_norm, clean_norm, where)


def _materialize_pattern(term_spec, where, defect_norm, clean_norm):
    regex = term_spec.get("regex")
    if not regex:
        raise SignatureError("{}：pattern 项缺 regex".format(where))
    if re.search(r"\\s|\\n|\\t", regex):
        raise SignatureError(
            "{}：pattern 不得含空白类（判定文本已去空白，含空白永远不匹配）：{!r}".format(where, regex)
        )
    try:
        compiled = re.compile(regex)
    except re.error as exc:
        raise SignatureError("{}：pattern 正则非法：{}".format(where, exc))
    if not compiled.search(defect_norm):
        raise SignatureError("{}：pattern 在缺陷件里没有命中，不能作为该缺陷的痕迹：{!r}".format(where, regex))
    hit = compiled.search(clean_norm)
    if hit:
        raise SignatureError(
            "{}：pattern 在干净件里也命中（{!r} 命中 {!r}），"
            "它刻画的不是本次注入的痕迹".format(where, regex, hit.group(0))
        )
    return {"kind": _PATTERN, "regex": regex, "min_len": 0, "floor": 0, "alts": []}


def _materialize_lexeme(term_spec, variables, where, defect_norm=None):
    """定义性词表项：核验"可满足"，以及（可选）"不可能从所给文书抄到"。

    ``require_unquotable: true`` 时逐个选项剔除**在该文书里能找到**的那些——能找到
    就意味着系统可以原样抄出来，它就不构成"系统在主张什么"的证据；剔除了哪些如实记录。
    全部被剔除即报错（该词表对本篇文书没有判别力）。
    """
    alts = [_render(t, variables, where) for t in (term_spec.get("alts") or [])]
    if not alts:
        raise SignatureError("{}：lexeme 项缺 alts".format(where))
    kept, dropped = list(alts), []
    if term_spec.get("require_unquotable") and defect_norm is not None:
        kept = [a for a in alts if a not in defect_norm]
        dropped = [a for a in alts if a in defect_norm]
        if not kept:
            raise SignatureError(
                "{}：lexeme 的所有选项都能在所给文书里找到，对本篇没有判别力：{}".format(where, alts))
    min_len = int(term_spec.get("min_len") or min(len(a) for a in kept))
    too_long = [a for a in kept if len(a) < min_len]
    if too_long:
        raise SignatureError(
            "{}：lexeme 的 min_len={} 大于这些选项的长度，永远不可能命中：{}".format(
                where, min_len, too_long
            )
        )
    return {"kind": _LEXEME, "alts": kept, "min_len": min_len, "floor": None,
            "unquotable_dropped": dropped}


def _materialize_witness(term_spec, variables, defect_norm, clean_norm, where):
    role = term_spec.get("role") or "insert"
    alts = [_render(t, variables, where) for t in (term_spec.get("alts") or [])]
    if not alts:
        raise SignatureError("{}：witness 项缺 alts".format(where))

    # 该角色的"另一份文件"：insert 的痕迹在缺陷件、不该在干净件；delete 反之
    if role == "insert":
        present_doc, absent_doc, absent_name = defect_norm, clean_norm, "干净件"
    else:
        present_doc, absent_doc, absent_name = clean_norm, defect_norm, "缺陷件"

    resolved = []
    for alt in alts:
        # 痕迹根据：整串必须出现在"该有它"的那份、且不在"不该有它"的那份
        grounded = alt in present_doc and alt not in absent_doc
        # 判定用片段：整串（短于 MIN_RUN 时）或 MIN_RUN 滑窗中在该"不该有"的那份里找不到的
        runs = witness_runs(alt, absent_doc) if grounded else []
        resolved.append({"text": alt, "grounded": grounded, "runs": runs})

    usable = [r for r in resolved if r["runs"]]
    if not usable:
        raise SignatureError(
            "{}：role={} 的见证串没有一个可用——要么整串并未'出现在{}、不出现在{}'"
            "（没有痕迹根据），要么它的 {} 字片段全都能在{}里找到（引它不能证明定位）。"
            "见证串：{}".format(where, role,
                              "缺陷件" if role == "insert" else "干净件", absent_name,
                              MIN_RUN, absent_name, alts)
        )
    return {
        "kind": _WITNESS,
        "role": role,
        "alts": [r["text"] for r in resolved],
        "resolved": resolved,
        "runs": sorted({run for r in usable for run in r["runs"]}),
        "grounded_alts": [r["text"] for r in resolved if r["grounded"]],
    }


# ── 判定 ────────────────────────────────────────────────────────
def _term_ok(term, text_norm):
    if term["kind"] == _PATTERN:
        return re.search(term["regex"], text_norm) is not None
    if term["kind"] == _WITNESS:
        # 命中任一"不可能从那份文书里抄到"的片段即算指到了注入处
        return any(run in text_norm for run in term["runs"])
    for alt in term["alts"]:
        if len(alt) >= term["min_len"] and alt in text_norm:
            return True
    return False


def term_matches(term, text):
    """单项判定（对外暴露，供测试与审阅直接验证某一项"到底按什么算命中"）。"""
    return _term_ok(term, normalize(text))


def match(texts, signature, tier=1):
    """判定一组文本（定位引文等）是否命中签名。

    :param texts: 可迭代的字符串（通常是 ``evidence``、``locus``、``message``）
    :param signature: :func:`materialize` 的产物
    :param tier: 1 = 所有子句；2 = 只算有痕迹根据的子句
    :returns: 命中的子句序号（``0`` 起）或 ``None``
    """
    if not signature:
        return None
    fields = [normalize(t or "") for t in texts]
    blob = "".join(fields)
    if not blob:
        return None
    for idx, clause in enumerate(signature.get("clauses") or []):
        if tier == 2 and not clause.get("grounded"):
            continue
        if clause.get("grounded"):
            # 有痕迹根据的子句：见证/模式项本身就把它钉在改动上，故只在整段文本里找
            if all(_term_ok(term, blob) for term in clause["terms"]):
                return idx
        else:
            # **无痕迹根据的子句**（整条全是定义性词表项）：要求各项落在**同一个文本字段**里。
            # 理由：这类子句没有任何东西把它钉在改动上，只能靠"把字段与断言连起来说"来立论。
            # 若允许跨字段拼凑，"字段名"和"不一致"可以来自两处互不相干的文字——实测踩过：
            # B1 报「效力 | 文字 | 标点不一致 | “一案〔本诉案号：(2024)海民初字第02488号〕”」，
            # 引文说标点、说明里抄了一段含"案号"的原文，就被算成"检出跨文书合同编号矛盾"。
            for field in fields:
                if field and all(_term_ok(term, field) for term in clause["terms"]):
                    return idx
    return None


def finding_texts(finding):
    """取出一个 finding 的定位引文三元组。

    ``evidence`` 与 ``locus`` 是"定位"字段；``message`` 是"说明"字段。本文方法的
    缺栏报告把栏名写在 message 里（``evidence`` 只放文书类型），故三者都取。
    """
    return (getattr(finding, "evidence", None), getattr(finding, "locus", None),
            getattr(finding, "message", None))


def is_overbroad(finding):
    """定位引文过长 ⇒ 未真正定位，不授信（见模块开头"反退化守卫"）。"""
    evidence = getattr(finding, "evidence", None) or ""
    return len(evidence) > MAX_EVIDENCE_CHARS


def describe(signature):
    """人类可读的签名摘要（写进结果文件，便于审阅"到底按什么判的"）。"""
    out = []
    for idx, clause in enumerate(signature.get("clauses") or []):
        parts = []
        for term in clause["terms"]:
            if term["kind"] == _PATTERN:
                parts.append("模式 {!r}".format(term["regex"]))
            elif term["kind"] == _LEXEME:
                parts.append("词表{}/≥{}".format("/".join(term["alts"]), term["min_len"]))
            else:
                n_runs = sum(len(r["runs"]) for r in term["resolved"])
                parts.append("痕迹{}（{}字片段×{}，{})".format(
                    "/".join(term["alts"]), MIN_RUN, n_runs,
                    "加进来" if term.get("role") == "insert" else "拿掉"))
        out.append({
            "clause": idx + 1,
            "grounded": clause.get("grounded"),
            "require": " 且 ".join(parts),
        })
    result = {"doc_scope": signature.get("doc_scope"), "clauses": out}
    if signature.get("dropped_clauses"):
        result["dropped_clauses"] = signature["dropped_clauses"]
    return result
