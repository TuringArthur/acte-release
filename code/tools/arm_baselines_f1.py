#!/usr/bin/env python3
"""基线适配器：把共享基线 B1–B5 挂到"案情弱点评估"这一任务形态上。

## 为什么需要这一层（而不是直接用共享基线管线）

共享基线管线的提示词原先是为文书质检写的，输入是"几份文书"，问的是
"这份文书有什么问题"。本任务形态的输入是案件事实文本，问的是"本案在立案与诉讼准备上
有什么弱点/风险"。两者共用的是同一套基线口径（B1 纯提示 / B2 检索增强 /
B3 两段式自检 / B4 角色分工），不是同一段提示词。

故本适配器：

* 复用共享基线管线的模型调用管线（`_chat_with_retry` / `chat`）：
  凭据只从环境变量读、出站前校验 host、退避重试，这些都不重写；
* 替换提示词与输入形状（`facts_text` 而非 `documents`）；
* 保持输出形状与本仓 harness 的约定一致：`run(payload) -> {"findings": [...]}`。

## 一条决定性的提示约束：必须引原句

判定走弱点注入签名，系统的 `evidence` 要引到注入留下的痕迹才算检出。
故提示词明确要求逐条附原文片段，与既有基线口径一致（同样是"一条问题一行、附原文片段"）。

这不是为难基线，而是公平性的前提：若不要求引原句，基线报"本案存在时效风险"
却不说依据哪句话，签名判定无从授信，那测的就成了"格式是否合我们的胃口"，
而不是"有没有发现问题"。这里有一个已知的教训：旧口径曾要求基线使用本系统的内部码名，
于是不论基线是否真的找到了缺陷，效力性检出率恒为 0.000。

## 断点续传与可重判

每次调用把模型原始回复落盘到 会话运行目录 会话运行目录 `experiment/run/baselines/<arm>/<task_id>.json`
（`CTD_LA_BASELINE_CACHE_DIR` 可改根目录；`CTD_LA_BASELINE_RUN_TAG` 分 k 槽），
记录含输入指纹 `input_sha256`（臂 + payload + B2 语料 + 模型/端点）：

* 续传：重跑 harness 时命中指纹一致的落盘回复 ⇒ 不联网直接用，
  中途断电后原命令再跑一遍即从断点继续，只有没跑成的实例才打 API；
* 换模型自动失效：指纹含模型 id/端点 ⇒ 跨模型臂不会误用旧模型的回复；
* 可重判：原始回复常驻磁盘 ⇒ 判定口径变更（如合取）后可像会话臂一样
  免 API 重判，这补上了基线此前"无存留产物、不可重判"的溯源缺口；
* 空回复不落盘：上游故障的空串多半是暂时的，固化它会把故障变成"读数"。

`meta.cache` 记 `hit` / `miss+saved` / `stale+saved`，读结果文件可核。

## 凭据（只从环境变量读，源码/示例/测试里不出现任何凭据字面量）

沿用 `arms.py` 的约定：端点 `CTD_LA_MODEL_BASE_URL` 或 `DEEPSEEK_BASE_URL`（须含
API 前缀如 `/v1`）；密钥 `CTD_LA_MODEL_API_KEY` 或 `DEEPSEEK_API_KEY`；
模型 `CTD_LA_MODEL_ID` 或 `DSH_MODEL_ID`。`CTD_LA_ARM_MODE=dry-run` 时不联网。

## 用法

    # 由 harness 挂载（推荐）
    python3 code/harness.py --taskset experiment/tasks \\
        --systems acte,b1 --arm-module <path>/arm_baselines_f1.py

    # 单点调试
    echo '{"arm":"b1","facts_text":"案件事实：…"}' | python3 code/tools/arm_baselines_f1.py
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
PAPER_ROOT = HERE.parents[2]
CODE_ROOT = PAPER_ROOT / "code"


def _find_repo_root(start):
    """向上找含 `papers/` 与 `rules/` 的目录；找不到返回 None（独立导出时无仓库根）。"""
    for cand in [start] + list(start.parents):
        if (cand / "papers").is_dir() and (cand / "rules").is_dir():
            return cand
    return None


REPO_ROOT = _find_repo_root(HERE)
DEFAULT_PLUMBING = ((REPO_ROOT / "impl" / "baselines" / "arms.py")
                    if REPO_ROOT else None)


class AdapterError(RuntimeError):
    pass


def load_plumbing(path):
    """按操作者给出的路径加载共享基线的模型调用管线（不重写凭据与网络逻辑）。"""
    if path is None or not Path(path).is_file():
        raise AdapterError(
            "共享基线管线不存在：%s\n"
            "  用 --plumbing 指定，或设 CTD_LA_PLUMBING 环境变量。\n"
            "  本适配器**复用** arms.py 的模型调用与解析管线，不重写它——"
            "凭据处理与出站校验只有一处实现，才不会漂。"
            % path)
    spec = importlib.util.spec_from_file_location("ctd_la_arm_plumbing", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 本任务形态的提示词（与文书质检口径同源不同题）────────────────
#
# 四套基线的分工与共享管线完全一致，只有"问什么"变了：
#   文书质检：请审阅这几份法律文书，指出存在的问题（层次/问题类型/原文片段/说明）
#   本任务：请审阅本案案情，指出立案与诉讼准备上的弱点（弱点类别/原文片段/后果/依据）
#
# B5（通用规约检查点，本模块新增、共享管线无）：约束来源是人工书写的
# 通用时序规约，见下方 `B5_SPEC_CASE` 的防污染注释；提示与两段结构见 `_arm_b5`。
#
# 输出格式刻意与既有口径保持同构（四字段竖线分隔、末字段为说明）：
# 这样解析器可以共用，且"要求引原文片段"这一条对各方一致。

ISSUE_FORMAT_CASE = """\
逐条列出你发现的弱点或法律风险，每条一行，用竖线分隔四个字段：
弱点类别 | 原文片段 | 后果 | 说明
其中「原文片段」必须**逐字摘自上面的案情原文**（一段连续文字，不要改写、不要概括）。
若确实没有发现弱点，只输出一行：无弱点。"""

B1_SYSTEM_CASE = (
    "你是一名执业律师，擅长在起诉前审查案件的立案条件与诉讼风险。"
    "回答简洁、逐条给出，不要泛泛而谈。"
)

B2_SYSTEM_CASE = (
    "你是一名执业律师，擅长在起诉前审查案件的立案条件与诉讼风险。"
    "可参考随附的法律条文摘录；条文与本案无关时不要强行套用。"
    "回答简洁、逐条给出。"
)

B3_SYSTEM_CASE = (
    "你是一名执业律师，擅长在起诉前审查案件的立案条件与诉讼风险。"
    "按流程作答：先逐项排查，再复核是否有遗漏或误判，最后给出结论。"
)

B4_ROLES_CASE = (
    ("形式审查员", "你负责审查立案的形式要件：管辖、期限、主体资格、请求的可实现性。"
                   "逐条给出你发现的问题，附案情原文片段。"),
    ("实体审查员", "你负责审查实体与证据：请求权基础、要件事实、证据是否足以支撑主张。"
                   "逐条给出你发现的问题，附案情原文片段。"),
    ("综合裁断员", "你是负责汇总的资深律师。综合各审查员意见，给出最终的问题清单。"
                   "只保留有案情原文支撑的条目，并逐条附上原文片段。"),
)

# ── B5：通用规约检查点基线（运行时强制类对照）────────────────────
#
# 模拟 AgentSpec / Agent-C 思路的"**人工书写的通用时序规约**"作为约束来源：
# 规约是与具体法律领域无关的运行时谓词（期限状态、待办义务、前置条件、
# 不可逆后果、顺序、留痕），由人工一次写死、对所有案件通用。
#
# ★ 防对照污染的硬约束：下表**不得**抄本系统闸门登记表（t-irr.json /
#   拦截表）的具体条目、触发条件措辞或痕迹短语。B5 的全部约束必须是
#   "通用运行时规约"水平的抽象——这正是论文表 1 的对比轴：
#   约束来源 = 人工书写通用规约（B5）vs 法律闸门登记表派生（本系统）。
#   若 B5 用了派生条目的措辞，对照就变成了"抄没抄我们的表"，轴失效。
B5_SYSTEM_CASE = (
    "你是一名执业律师，擅长在起诉前审查案件的立案条件与诉讼风险。"
    "本次审查以一份**通用运行时规约**为准绳：它是与具体法律领域无关的行动前检查点，"
    "不针对任何特定法律，也不需要引用法条。回答简洁、逐条给出，不要泛泛而谈。"
)

B5_SPEC_CASE = """\
通用运行时规约（行动前检查点，对任何案件、任何领域的行动都适用）：
S1 期限状态：拟采取或拟暂缓的每个动作，是否存在已届满或即将届满的期限？
S2 应为而未为：是否存在应当在期限内完成、但至今尚未完成的动作？
S3 前置事项：是否存在必须先行完成、或须经当事人确认后方可继续的前置事项，而尚未完成？
S4 不可逆后果：拟采取的动作一旦作出，是否触发不可逆的、或使处境显著加重的后果？
S5 顺序与冲突：多个动作之间是否存在必须遵守的先后顺序？拟采取的动作是否与既有动作重复或相互冲突？
S6 凭据与留痕：支撑关键判断的事实是否都有可出示的凭据？关键沟通与确认是否留有可回溯的记录？"""


def _facts_block(payload):
    text = (payload.get("facts_text") or "").strip()
    if not text:
        return "（未提供案情）"
    return "本案案情如下：\n\n" + text


def _task_tail(payload):
    return ("\n\n请指出**本案在立案与诉讼准备上**存在的弱点或法律风险"
            "（例如管辖、期限、主体、请求范围、证据形式、程序风险等）。\n" + ISSUE_FORMAT_CASE)


def _user_prompt(payload, retrieved=None):
    body = _facts_block(payload)
    if retrieved:
        body += "\n\n可参考的法律条文摘录：\n" + retrieved
    posture = payload.get("posture")
    if posture:
        # 姿态只作为背景说明（与本引擎的姿态参数对齐），不暗示答案
        body += "\n\n（本案的办案姿态：%s）" % posture
    return body + _task_tail(payload)


def _ride_along(payload):
    """把与本臂无关的字段一并回传，便于 harness 溯源。"""
    return {"task_id": payload.get("task_id"), "arm": payload.get("arm")}


# ── 解析（与 arms.py 同构，但不做 doc_id 归属：本题是单一案情文本）──
def _is_header_like(parts):
    words = {"弱点类别", "原文片段", "后果", "说明", "层次", "问题类型",
             "---", "--", "—", "-", ""}
    return all(p in words for p in parts)


def parse_issues_f1(reply, payload, plumbing, full_line_in_message=False):
    """把模型的答复解析成 findings。

    :param reply: 模型原文。
    :param full_line_in_message: 引文口径敏感性开关（默认关闭，主口径不变）。
        开启时把整行原文放进 `message`，判定机会一并检索 evidence / locus /
        message 三个字段，故整行口径下"把痕迹句引在任一分栏里"都能被看到。
        为什么要这个开关：实测四条漏检逐条打开，
        全部是"找到了但改了措辞或把痕迹句引在未被取作 evidence 的分栏里"，
        与 基线读数记录 §5 的 caveat 同型，
        故需要一个口径把"没找到"与"没引原句"分开报，否则检出率会被系统性低估
        而读者看不出来。
         主口径必须保持关闭：开启后等价于放宽引文要求，与基线读数不可比。
    :returns: `[{code, layer, defect_layer, locus, evidence, message, severity}, …]`

    `defect_layer` 一律标 `validity`（对基线从宽）：若模型没标注层次，
    我们按效力性问题记账，不因为缺少我们的分类而扣它的检出。这与既有
    基线口径一致（共享管线约定：报出的问题默认算 validity）。
    """
    findings = []
    seen = set()
    for raw_line in (reply or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line in ("无弱点", "无问题"):
            continue
        parts = [p.strip() for p in re.split(r"[|｜]", line)]
        if len(parts) < 3:
            continue
        if _is_header_like(parts):
            continue
        code = parts[0]
        # 按提示词规定的栏位取数：
        #     弱点类别 | 原文片段 | 后果 | 说明
        #   旧实现取"最长分栏 + 末栏"，于是当说明栏最长时（很常见，说明本来就是
        #   最长的一栏），判定只搜 evidence/locus/message 三处，而 evidence=message=说明栏
        #   ⇒ 原文片段栏被整个丢掉，引到了痕迹也判不出。
        #   实测（扩样后重跑真实系统臂）：输出里痕迹串 grep 得到（如「与被告丙连带赔偿」
        #   明明在第 2 栏），检出却记 0；逐条打开 13 条"漏检"才发现全是取数丢栏。
        #   故改为按规定的栏位取：evidence ← 第 2 栏（原文片段）；
        #   message ← 其余后续栏（后果 + 说明）。
        #   刻意不把第 1 栏（弱点类别）并进 message：那一栏常写成问题名（如"重复起诉"），
        #   并进去会让断言子句被自己的标题自动满足，那样断言就不再是"系统在主张什么"。
        quote, tail_parts = "", []
        if len(parts) >= 2:
            quote = parts[1]
            tail_parts = parts[2:]
        if len(quote) < 4:
            # 栏位错位（模型没按格式写）时退化为旧行为：取最长的一栏作引文，
            # 其余各栏一并进 message，不丢信息是这次修的主旨。
            candidates = [p for p in parts[1:] if len(p) >= 4]
            if not candidates:
                continue
            quote = max(candidates, key=len)
            tail_parts = [p for p in parts[1:] if p != quote]
        evidence = quote
        message = "｜".join(tail_parts) if tail_parts else (code or parts[-1])
        key = (code, evidence[:40])
        if key in seen:
            continue
        seen.add(key)
        findings.append({
            "code": "arm_issue:%s" % (code or "未分类"),
            "layer": "validity",
            "defect_layer": "validity",
            "locus": None,
            "evidence": evidence,
            # 敏感性口径下把整行放进 message（判定机三个字段都检索）。evidence 仍取
            # 最长分栏并受 240 字反退化上限约束，故这不等于"整段抄底本"。
            "message": line if full_line_in_message else (message or code),
            "severity": None,
        })
    return findings


# ── 四套基线 ────────────────────────────────────────────────────
def _arm_b1(payload, chat, plumbing):
    return chat([{"role": "system", "content": B1_SYSTEM_CASE},
                 {"role": "user", "content": _user_prompt(payload)}]), {}


def _arm_b2(payload, chat, plumbing, corpus):
    """B2 检索增强：用确定性关键词重叠从语料检索条文（与 arms.py 同一手法）。"""
    query = (payload.get("facts_text") or "")[:2000]
    snippets = []
    if corpus and Path(corpus).is_dir():
        qchars = {c for c in query if "\u4e00" <= c <= "\u9fff"}
        scored = []
        for f in sorted(Path(corpus).glob("*.txt")):
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            overlap = len(qchars & {c for c in text if "\u4e00" <= c <= "\u9fff"})
            if overlap:
                scored.append((overlap, f.name, text[:400]))
        scored.sort(reverse=True)
        snippets = ["【%s】%s" % (n, t) for _, n, t in scored[:3]]
    retrieved = "\n\n".join(snippets)
    reply = chat([{"role": "system", "content": B2_SYSTEM_CASE},
                  {"role": "user", "content": _user_prompt(payload, retrieved=retrieved)}])
    return reply, {"n_retrieved": len(snippets), "corpus": bool(corpus)}


def _arm_b3(payload, chat, plumbing):
    """B3 单体通用 Agent：两段式（检查 → 自我复核），不区分案型。"""
    checklist = chat([{"role": "system", "content": B3_SYSTEM_CASE},
                      {"role": "user", "content":
                       "先只做第一步：列出你在本案中要逐项排查的**检查清单**"
                       "（不超过 12 条，每行一条，不要给结论）。\n\n"
                       + _facts_block(payload)}])
    reply = chat([{"role": "system", "content": B3_SYSTEM_CASE},
                  {"role": "user", "content":
                   _user_prompt(payload)
                   + "\n\n你上一轮列出的检查清单如下，请按它逐项排查后给出结论：\n" + checklist}])
    return reply, {"self_check": True}


def _arm_b4(payload, chat, plumbing):
    """B4 角色分工多智能体：三角色分头看，再汇总（AgentCourt 式）。"""
    opinions = []
    for title, sys_prompt in B4_ROLES_CASE[:2]:
        opinions.append("【%s】\n%s" % (title, chat([
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": _facts_block(payload) + _task_tail(payload)}])))
    judge = chat([{"role": "system", "content": B4_ROLES_CASE[2][1]},
                  {"role": "user", "content": _facts_block(payload) + _task_tail(payload)
                   + "\n\n以下是各审查员的意见：\n" + "\n\n".join(opinions)}])
    return judge, {"n_opinions": len(opinions)}


def _arm_b5(payload, chat, plumbing):
    """B5 通用规约检查点基线：两段单发（违规清单 → 检查点拦截式确认与补漏）。

    约束来源是**人工书写的通用时序规约** `B5_SPEC_CASE`（AgentSpec/Agent-C 思路），
    与本系统的"法律闸门登记表派生"正面对照（论文表 1 的对比轴）。
    两段的语义：第一段按规约逐项检查、列违规/风险清单；第二段模拟运行时
    检查点的拦截语义——对第一段清单逐条确认或剔除，再按规约补漏。
    输出格式与 B1–B4 完全同构（`ISSUE_FORMAT_CASE`），故共用解析与判定口径。
    """
    checklist = chat([{"role": "system", "content": B5_SYSTEM_CASE},
                      {"role": "user", "content":
                       "请把下面的通用运行时规约当作行动前的检查点，"
                       "逐条对照本案，先输出违规/风险清单"
                       "（每条注明命中的规约编号，如 S3，其他按输出格式作答）。\n\n"
                       + B5_SPEC_CASE + "\n\n" + _facts_block(payload) + _task_tail(payload)}])
    reply = chat([{"role": "system", "content": B5_SYSTEM_CASE},
                  {"role": "user", "content":
                   _user_prompt(payload)
                   + "\n\n你上一轮按通用运行时规约列出的违规/风险清单如下：\n" + checklist
                   + "\n\n现在执行**检查点拦截复核**（模拟运行时规约的逐点拦截语义）："
                     "逐条确认或剔除上一轮清单——确有案情原文支撑且确实命中规约的保留，"
                     "泛泛的提醒、建议形态或无原文支撑的剔除；"
                     "并再次对照通用运行时规约逐条补漏（上一轮遗漏的违规一并补上）。\n"
                     + ISSUE_FORMAT_CASE}])
    return reply, {"spec_check": True, "n_spec_items": len(
        [l for l in B5_SPEC_CASE.splitlines() if l.startswith("S")])}


ARM_IDS = ("b1", "b2", "b3", "b4", "b5")


# ── 断点续传：逐实例回复落盘（见模块头「断点续传与可重判」）──────────
def _cache_dir(arm):
    root = os.environ.get("CTD_LA_BASELINE_CACHE_DIR") or str(
        PAPER_ROOT / "experiment" / "run" / "baselines")
    d = Path(root) / re.sub(r"[^A-Za-z0-9._-]", "_", arm)
    tag = (os.environ.get("CTD_LA_BASELINE_RUN_TAG") or "").strip()
    if tag:
        d = d / re.sub(r"[^A-Za-z0-9._-]", "_", tag)
    return d


def _input_sha256(arm, payload):
    """输入指纹：臂 + payload + B2 语料 + 模型/端点。

    含模型 id 是硬要求，跨模型稳健性轮换模型重跑时，
    指纹必须失配，否则会静默复用旧模型的回复。
    """
    h = hashlib.sha256()
    h.update(arm.encode("utf-8"))
    h.update(b"\x00")
    h.update(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                        default=str).encode("utf-8"))
    for key in ("CTD_LA_MODEL_ID", "DSH_MODEL_ID",
                "CTD_LA_MODEL_BASE_URL", "DEEPSEEK_BASE_URL"):
        h.update(b"\x00")
        h.update(str(os.environ.get(key) or "").encode("utf-8"))
    if arm == "b2":
        corpus = os.environ.get("CTD_LA_CORPUS")
        if corpus and Path(corpus).is_dir():
            for f in sorted(Path(corpus).glob("*.txt")):
                h.update(b"\x00")
                h.update(f.name.encode("utf-8"))
                h.update(f.read_text(encoding="utf-8").encode("utf-8"))
    return h.hexdigest()


def _cache_read(cache_file, sha):
    """命中且指纹一致才返回回复；否则 None（stale/损坏都当 miss 重打）。"""
    if not cache_file.is_file():
        return None, None
    try:
        rec = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if rec.get("input_sha256") == sha and rec.get("reply"):
        return rec.get("reply"), (rec.get("arm_meta") or {})
    return None, None


def _cache_write(cache_file, arm, task_id, sha, reply, extra):
    """原子落盘（先 `.part` 再 replace）：被杀只会留下 .part，不会留半截正式文件。

    混模型守卫：同名缓存文件若属于另一个模型/
    端点，拒绝覆盖并报错。原因：sha 失配本就当 miss 重打（payload 演化时覆盖是
    正当刷新），但换模型时静默覆盖会毁掉旧模型的原始回复，结果 JSON 只存
    指标，原始回复只在缓存里，毁了就是复现缺口。同模型的 stale 刷新不受影响。
    """
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    if cache_file.is_file():
        try:
            old = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        old_env, new_env = old.get("model_env"), _model_env()
        if old_env is not None and old_env != new_env:
            raise AdapterError(
                "缓存文件 %s 属于另一模型/端点（%s ≠ %s）——"
                "跨模型重跑请换 CTD_LA_BASELINE_CACHE_DIR 或 CTD_LA_BASELINE_RUN_TAG；"
                "静默覆盖会毁掉旧模型的原始回复。"
                % (cache_file, old_env.get("model"), new_env.get("model")))
    tmp = Path(str(cache_file) + ".part")
    tmp.write_text(json.dumps(
        {"task_id": task_id, "arm": arm, "input_sha256": sha,
         "model_env": _model_env(), "reply": reply, "arm_meta": extra},
        ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(cache_file)


def _model_env():
    """模型/端点指纹（不含密钥）：混模型守卫与溯源用。"""
    def _host(url):
        m = re.match(r"https?://([^/]+)", url or "")
        return m.group(1) if m else (url or "")
    return {
        "model": os.environ.get("CTD_LA_MODEL_ID") or os.environ.get("DSH_MODEL_ID") or "",
        "base_url_host": _host(os.environ.get("CTD_LA_MODEL_BASE_URL")
                               or os.environ.get("DEEPSEEK_BASE_URL") or ""),
    }


def run(payload, plumbing=None):
    """执行一个臂。接口与 共享基线管线 的 `run` 同形。"""
    arm = (payload.get("arm") or "").lower()
    if arm not in ARM_IDS:
        raise AdapterError("未知基线臂：%r（可选 %s）" % (arm, "、".join(ARM_IDS)))

    mode = os.environ.get("CTD_LA_ARM_MODE", "http").strip().lower()
    if mode == "dry-run":
        return {"findings": [], "plan": None,
                "meta": {"arm": arm, "mode": "dry-run",
                         "warning": "dry-run 不联网、不产出 findings；"
                                    "其指标仅供流水线自检，不得作为基线结果。"}}

    # ── 断点续传：先查落盘回复（命中则完全不加载管线、不联网）────────
    sha = _input_sha256(arm, payload)
    safe_tid = re.sub(r"[^A-Za-z0-9._-]", "_",
                      str(payload.get("task_id") or "no-id"))
    cache_file = _cache_dir(arm) / ("%s.json" % safe_tid)
    reply, extra = _cache_read(cache_file, sha)
    cache_state = "hit" if reply is not None else "miss"

    pl = plumbing
    if reply is None:
        pl = plumbing or load_plumbing(
            os.environ.get("CTD_LA_PLUMBING") or DEFAULT_PLUMBING)
        chat = pl._chat_with_retry
        if arm == "b1":
            reply, extra = _arm_b1(payload, chat, pl)
        elif arm == "b2":
            reply, extra = _arm_b2(payload, chat, pl, os.environ.get("CTD_LA_CORPUS"))
        elif arm == "b3":
            reply, extra = _arm_b3(payload, chat, pl)
        elif arm == "b4":
            reply, extra = _arm_b4(payload, chat, pl)
        else:
            reply, extra = _arm_b5(payload, chat, pl)
        extra = extra or {}
        # 空回复不落盘（多半是上游故障；固化会把故障变成"读数"）。
        if reply:
            _cache_write(cache_file, arm, payload.get("task_id"),
                         sha, reply, extra)
            cache_state = "miss+saved"

    findings = parse_issues_f1(reply, payload, pl)
    meta = {"arm": arm, "mode": mode, "n_reported": len(findings),
            "reply_chars": len(reply or ""), "cache": cache_state,
            **_ride_along(payload), **extra}
    # `plan` 显式给 None：本臂不做动作选择，故计划质量指标记「不适用」而不是 0
    return {"findings": findings, "plan": None, "meta": meta}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plumbing", default=os.environ.get("CTD_LA_PLUMBING")
                    or DEFAULT_PLUMBING, help="共享基线管线模块路径")
    ap.add_argument("--arm", default=None, help="臂 id（b1–b5）；也可由 stdin 的 payload 给出")
    args = ap.parse_args(argv)

    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    if args.arm:
        payload["arm"] = args.arm
    try:
        out = run(payload, load_plumbing(args.plumbing))
    except AdapterError as exc:
        sys.stderr.write("\n%s\n" % exc)
        return 2
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())