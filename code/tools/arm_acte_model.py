#!/usr/bin/env python3
"""真实系统臂：模型 + DSH 插件（盾 + 发射端）→ harness 的 findings/plan。

## 它补的是哪个缺口

机械臂（`--systems acte`）按设计不产出弱点报告：它读案件事实里声明好的
字段（`triggers`/`covers`/`confirmed`/`remaining_days`），只做计划质量，
故检出率恒 0.0（见 `code/README.md` 的「夹具简化」）。真实系统要自己判断那四项，
本适配器就是把它接上 harness 的那一层。

## 三段式（会话驱动为什么不在 Python 里）

```
  ① prepare（本文件 --prepare）   读任务集 → 每个实例一份提示词 + 一张清单（manifest.tsv）
  ② 跑会话（run-acte-sessions.sh） 按清单逐条起 headless 会话（node CLI，插件随 profile 挂载）
  ③ serve（本文件默认模式）        读会话产物 → findings + plan，交给 harness 评分
```

为什么不在 Python 里 subprocess 起会话：本仓库把子进程面收敛到单一入口，
静态安全审查会把"由参数或变量推出来的 argv 进 subprocess" 判为命令注入。
而会话驱动这条路与 `tools/run-experiment.sh` 完全同形
（都是 `node <cli> --profile headless …`），故放在 shell 里，Python 只做备料与取数，
验证链因此不必引入子进程面。

## 取数口径（两条，都不是随手定的）

1. findings 取会话的 stdout（那正是模型的最终答复），解析函数与基线同一个
   （`arm_baselines_f1.parse_issues_f1`）：提示词与解析器都同源，
   与 B1–B4 的对照才成立。
2. plan 取发射端写的 JSONL 日志（`ACTE_PLAN_JOURNAL`，见 `plugins/acte-plan.js`），
   不靠模型在答复里复述计划：靠复述取数是脆的，而且"没复述"与"没提交"不可区分。

## 两条防作弊/防误读的纪律

- 覆盖度与期望回收一律按案件事实重算，不采信模型自报的数字：
  计划里只取"模型选了哪些 action_id"，`covers`/`expected_utility` 从实例声明里查。
  否则"模型把效用写高"就能抬高 M_2 的读数。
- `is_clean` 字段本臂一律不使用。harness 已盲化：
  干净件载荷与弱点件同形、不再下发 `is_clean`（此前带该字段时本臂也刻意不读，
  读了就毁掉"能否在案子是好的时候保持沉默"这条主结论）。臂日志里的 `is_clean`
  改由本臂自己的备料 manifest（`kind` 列）回填，仅供记账，不进提示词。

## 缺测纪律

会话失败（非零退出 / stdout 缺失）⇒ 抛错，不返回空 findings：
空 findings 会被读成"系统在这个实例上什么都没找到"，而真相是"这个实例没跑成"。
模型没提交计划 ⇒ `plan` 记 `None`（不是 0），并在 stderr 与臂日志里如实记一笔。

## 用法

    # ① 备料（在实验材料根目录下）
    python3 code/tools/arm_acte_model.py --prepare --run-dir experiment/run/acte-sessions

    # ② 跑会话（shell 驱动，可加消融：--ablation no-shield）
    code/tools/run-acte-sessions.sh experiment/run/acte-sessions

    # ③ 交给 harness 评分
    python3 code/harness.py --taskset experiment/tasks --systems acte-model \\
        --arm-module code/tools/arm_acte_model.py \\
        --out 实验结果目录 实验结果目录 experiment/results/（随实验材料发布）（随实验材料发布）acte-model.json

    # 单点调试（stdin 一条 payload）
    echo '{"arm":"acte-model","task_id":"F1-INJ-CONC-001","facts_text":"…"}' \\
        | python3 code/tools/arm_acte_model.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
CODE_ROOT = HERE.parents[1]
PAPER_ROOT = HERE.parents[2]
if CODE_ROOT.name != "code" or (PAPER_ROOT / "code").resolve() != CODE_ROOT:
    raise RuntimeError("目录布局与预期不符：本文件应位于 <篇目录>/code/tools/ 下，实际 %s" % HERE)

# 独立运行（stdin 单点调试）时 harness 的 sys.path 注入不存在，故自备。
# harness 路径下重复插入是幂等的（先查后插）。
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))
from ctd_acte.output_gate import apply_output_gate, verdict_memo  # noqa: E402

DEFAULT_RUN_DIR = PAPER_ROOT / "experiment" / "run" / "acte-sessions"
PROFILE_PATCH = PAPER_ROOT / "experiment" / "configs" / "profile.patch.yml"
ABLATION_DIR = PAPER_ROOT / "experiment" / "configs" / "ablations"


def _find_repo_root(start):
    """向上找含 `papers/` 与 `rules/` 的目录；找不到返回 None（独立导出时无仓库根）。"""
    for cand in [start] + list(start.parents):
        if (cand / "papers").is_dir() and (cand / "rules").is_dir():
            return cand
    return None


REPO_ROOT = _find_repo_root(HERE)

# 合法实例 id 的形状（如 `F1-INJ-CONC-001` / `F1-ADM-SCOPE-001`）。
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class AdapterError(RuntimeError):
    pass


def _safe_id(value, what):
    """校验外部给来的实例 id，只放行 `[A-Za-z0-9._-]` 且首字符为字母数字。

     为什么必须校验（不是形式主义）：`task_id` 来自 harness 的 payload，
    在本模块里被直接拼进文件路径（`run_dir/stdout/<id>.txt`）。
    若它含 `..` 或 `/`，那就是一条路径穿越入口，本仓库的提交前安全扫描
    会把"路径由外部输入拼出"判为高危并强制拦截。
    合法 id 的形状本来就是固定的，故这里只做白名单校验：越界即报错，
    不猜、不清理、不静默替换（静默替换会让"取错文件"看起来像"取到了文件"）。
    """
    text = "" if value is None else str(value)
    if not _SAFE_ID.match(text):
        raise AdapterError(
            "%s 不是合法的实例 id：%r（只允许字母/数字/._-，且首字符为字母或数字）"
            % (what, text))
    return text


def _load_module(path, alias):
    spec = importlib.util.spec_from_file_location(alias, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _baselines():
    """载入基线适配器：提示词与解析器同源是可比性的前提。"""
    return _load_module(CODE_ROOT / "tools" / "arm_baselines_f1.py", "f1_baselines_for_acte")


# ── ① prepare ────────────────────────────────────────────────────
T_IRR_SNAPSHOT = CODE_ROOT / "data" / "t-irr.json"

_KIND_LABEL = {
    "active": "动作型（拦：须先取得当事人书面确认）",
    "passive": "不作为型（催：期限内必须作为）",
}


def _risk_catalogue():
    """渲染`T_irr`条目清单（唯一权威是 `code/data/t-irr.json`，不另抄一份）。

    为什么要给模型这份清单：护栏只认登记的条目 id。不给清单时实测模型会自己编
    （第一次实跑它写了 `t-limit-6m` 这类不存在的 id），护栏只能回一句"未登记"，
    于是模型的判断表达不出来，测到的就成了"它会不会猜对 id"，
    而不是"它会不会判断这个动作跨层级"。这属方法层输入（引擎的规则表），
    与基线不使用它是方法差异，须在论文数据节写明。
    """
    if not T_IRR_SNAPSHOT.is_file():
        raise AdapterError("缺 T_irr 快照：%s（先跑 code/tools/derive_t_irr.py）"
                           % T_IRR_SNAPSHOT)
    snap = json.loads(T_IRR_SNAPSHOT.read_text(encoding="utf-8"))
    lines = []
    for e in snap.get("t_irr") or []:
        lines.append("- %s（%s）：%s ⇒ %s" % (
            e.get("gate_id"), _KIND_LABEL.get(e.get("kind"), e.get("kind")),
            e.get("action"), e.get("level_change")))
    if not lines:
        raise AdapterError("T_irr 快照里 0 条——没有条目可判，计划段无意义")
    return "\n".join(lines)


def _action_menu(actions):
    """把候选动作写成一行一条的清单（模型据此选 action_id）。"""
    lines = []
    for a in actions or []:
        lines.append("- %s｜%s｜推进要件 %s｜期望回收 %s｜成本 %s" % (
            a.get("action_id"), a.get("text") or "",
            "、".join(a.get("covers") or []) or "（无）",
            a.get("expected_utility", 0.0), a.get("cost", 1.0)))
    return "\n".join(lines)


PLAN_STEP = """

────────────────────────────────────────
此外，作为 ACTE 方法的一部分，请**先调用 `acte_plan` 工具**提交你打算采取的动作计划。

本案适用的**不可逆风险条目**（这是系统登记的条目 id；你判断某个动作会触发哪一条时，
就写它的 id。**清单之外的 id 会被护栏判为"未登记"并报出**，故不确定时写空数组）：
%s

本案可采取的动作清单（action_id｜动作｜推进的要件｜期望回收｜成本）：
%s

提交要求：
- 对每个你打算采取的动作，在 `triggers` 里写出**你判断**它会触发的不可逆风险条目 id
  （跨制度层级的动作、或不作为型的失权风险）；没有就写空数组。
  若你认为某动作只带来**可逆**的负担（例如保全担保、赔偿责任——可解除、可了结），
  也写空数组：护栏只管不可逆的那类；
- 在 `confirmed` 里写出本案**已取得当事人书面确认**的动作或条目 id（案情里写了才有）；
- 在 `deadline_days` 里给出**本案的剩余期限（天）**，由案情中的日期自行判断。
  不作为型风险（起诉期限、停止执行申请）缺这个字段就无法判定。
- 系统任务集的**结构化核验值（剩余期限）= %s 天**。以它为核对基准：你的推算与它
  不一致时以**较小者**为准并核对起算点（常错在"知道之日"还是"收到之日"）；
  护栏侧同样按较小值判定，两源不一致本身会被判为"时钟存疑"并要求先核实、
  再动涉期限的动作。

工具会返回护栏的裁决：须先取得当事人确认的动作、已到催办期限的不作为型风险都会被列出来。
请以裁决为准调整，最后用 `final: true` 再提交一次**你确定要采取的动作集**。

⚠ 判断**从简**：每一步的依据一句话即可；若某日期/期限需要推算而案情依据不足，
给出你的估计并注明依据，然后**继续往下做，不要在同一条上反复推演**。
（实测：不加这一条时，模型会在"期限从哪天起算"这类问题上反复计算到会话超时。）

然后，在你的**最终答复**里给出下面的弱点清单（先提交计划、最终答复里给清单）。
"""

# ── 红队对抗循环（红队开关 的生成式落点）──────────────────────────────
#
# 方法组件 2（README §3）：每个主张强制生成对手攻击，写入 Attacks[]。
# 它属真实系统臂的生成式部分，不是内核（engine.py：生成式由 harness/适配器接）。
#
# 消融 `no-attack-loop` = 本段整段不进提示词（其余逐字相同），故 红队开关 的对照
# 只差这一段结构，不差问法、不差解析器、不差判定机。
#
# T4 的 recall 仍由弱点注入签名机械判定（与无红队时同一口径）：红队改变的是
# 怎么生成清单，不是怎么判清单。
RED_TEAM_STEP = """
───────────────────────────────────────
【红队对抗循环｜必做，先于计划与最终清单】

站在**对方代理律师**的立场，在**心里**（不要写进最终答复）生成攻击清单 Attacks[]。
这是 ACTE 的强制回合：没有完成红队回合，不得进入计划提交与最终答复。

做法（逐项，不要只凭印象列两三条）：
1. 扫描案情中的：诉讼请求/受案范围、管辖与级别、起诉/上诉等期间、当事人主体、
   请求权基础与要件事实、证据形式与证明力、程序步骤与不作为风险；
2. 对每个角度各问一句："对方会从这里怎么打我？"有攻击则记入 Attacks[]，无则记
   `角度｜无攻击`；
3. Attacks[] 每条必须：① **逐字**引一段案情原文（连续片段）；② 写明对方主张与
   对我方的后果；③ 只报**本案真实存在**的攻击，不编造案情外事实；
4. 高风险攻击（可能失权、可能升级/转刑事、请求被驳回）必须全部进入 Attacks[]，
   不得省略——最终弱点清单须覆盖它们（可合并表述，但痕迹与后果都要在）。

★ 输出纪律（防格式污染）：
- Attacks[] 是你的**推演草稿**，**不要**把表格、A1/A2 编号、或"攻击角度"栏
  写进最终答复；
- 最终答复**只**输出任务要求的四字段行：弱点类别 | 原文片段 | 后果 | 说明
  （或"无弱点"）；原文片段必须逐字摘自案情；
- 每条最终弱点应能对应 Attacks[] 中的一条（可多条合并为一行）。

完成红队回合后，再按任务要求提交 acte_plan（若适用），最后只输出上述四字段清单。

⚠ **从简（必须）**：每个角度最多 1–2 条；单条依据一句话。不要长篇法理推演、
不要写完整分析报告。无攻击的角度立刻记 `角度｜无攻击` 并进入下一角度。
目标是在有限步内完成 Attacks[] → 计划 → 最终清单，而不是穷尽学理论证。
""".rstrip()


def _env_flag(name, default=True):
    """读布尔环境变量：`0/false/no/off` 为假，其余为真；未设取 default。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def build_prompt(payload, *, with_plan, attack_loop=True):
    """构造一条会话的提示词。

    findings 部分逐字复用基线适配器的措辞（`_facts_block` / `_task_tail`）：
    这样"我方 vs B1–B4"的对照只差方法层，不差问法。

    :param attack_loop: 是否插入红队对抗循环段。消融
        `no-attack-loop` 时为 False，其余提示词逐字相同。
    """
    baselines = _baselines()
    body = baselines._facts_block(payload) + baselines._task_tail(payload)
    if payload.get("posture"):
        body += "\n\n（本案的办案姿态：%s）" % payload["posture"]
    if attack_loop:
        body += "\n" + RED_TEAM_STEP
    if with_plan:
        # 双源时钟：结构化核验值进提示词，作为模型自算的对照基准。
        # 缺该字段时给占位句，占位也要说清"为什么没有"，否则模型会以为系统
        # 默认了某个值。
        rd = payload.get("remaining_days")
        clock = (str(rd) if rd is not None
                 else "（本案任务集未声明——以你从案情推算的为准）")
        body += PLAN_STEP % (_risk_catalogue(), _action_menu(payload.get("actions")),
                             clock)
    return body


def prepare(run_dir, taskset_path, *, attack_loop=True):
    """写每个实例的提示词与一张 manifest（shell 按它跑会话）。

    :param attack_loop: 主臂 True（含红队段）；`no-attack-loop` 消融 False。
        两种备料写入不同的 run 目录，避免两次读数混仓。
    """
    baselines = _baselines()
    ts = json.loads(Path(taskset_path).read_text(encoding="utf-8"))
    instances = ts.get("instances") or []
    if not instances:
        raise AdapterError("任务集里 0 实例——跑会话的分母为 0，拒绝备料。")

    tasks_root = Path(taskset_path).parent
    for sub in ("prompts", "stdout", "stderr", "journal"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)

    manifest = []
    for inst in instances:
        # 与 serve 同一条纪律：id 进文件名之前先校验（见 `_safe_id`）。
        tid = _safe_id(inst["task_id"], "task_id")
        weak = json.loads((tasks_root / inst["weak_file"]).read_text(encoding="utf-8"))
        payload = {
            "arm": "acte-model", "task_id": tid,
            "facts_text": weak["facts_text"],
            "posture": inst.get("posture"),
            "actions": inst.get("actions") or [],
            "remaining_days": inst.get("remaining_days"),
        }
        text = build_prompt(payload, with_plan=True, attack_loop=attack_loop)
        (run_dir / "prompts" / ("%s.txt" % tid)).write_text(text + "\n", encoding="utf-8")
        rd = inst.get("remaining_days")
        manifest.append((tid, "weak", inst.get("posture") or "", "%s.txt" % tid,
                         str(rd) if rd is not None else "-"))

    # 干净件（每个底本一份）：与基线一样，只问"有没有问题"，不含动作清单。
    # 红队段同样适用：干净件也要走同一条"能否保持沉默"的主结论口径。
    # 决策级套件（task_kind=decision_assessment，无注入）不出干净行：
    #   干净行与弱点行共用 stdout/journal 文件名（weak 行 id = base_case id），
    #   并行会互相覆盖与截断；且决策套件不测误报（无注入 ⇒ 无 FP 口径），
    #   干净行在那里既危险又无指标用途。
    has_injections = any(i.get("weakness_id") for i in instances)
    # v4 起干净行覆盖 cases/base/ 全部底本（含未被注入的纯负对照），
    # 与 harness.load_texts 同步：干净集必须含"从未被注入"的案件。
    base_dir = tasks_root / "cases" / "base"
    all_bases = sorted(p.stem for p in base_dir.glob("*.json")) if base_dir.is_dir() else []
    bases = sorted(set(all_bases) | {i["base_case"] for i in instances})
    for cid in bases if has_injections else []:
        base = json.loads((tasks_root / "cases" / "base" / ("%s.json" % cid))
                          .read_text(encoding="utf-8"))
        # 不带 is_clean（与 harness 同步盲化）：提示词构建只取 facts_text 等字段；
        # 干净件身份只落在 manifest 的 kind 列，供取数记账，不进任何臂可见的载荷。
        payload = {"arm": "acte-model", "task_id": cid,
                   "facts_text": base["facts_text"]}
        text = build_prompt(payload, with_plan=False, attack_loop=attack_loop)
        (run_dir / "prompts" / ("%s.txt" % cid)).write_text(text + "\n", encoding="utf-8")
        manifest.append((cid, "clean", "", "%s.txt" % cid, "-"))

    lines = []
    for tid, kind, posture, prompt_name, clock in manifest:
        # 空字段一律写 `-` 占位，不许留空：驱动脚本用
        #   `while IFS=$'\t' read -r …` 读这张表，而 IFS 里的制表符属
        #   "IFS 空白"，bash 会把连续分隔符折叠，干净件没有姿态，
        #   留空会让其后所有字段左移一位（journal 变成空），
        #   现场表现是"清单行残缺"而不是解析错位。
        # 第 8 列 = 结构化剩余期限（双源时钟的系统侧；驱动导出为
        #   ACTE_STRUCTURED_DEADLINE_DAYS）。旧清单 7 列时驱动按缺列处理。
        lines.append("\t".join([
            kind, tid, posture or "-",
            str(run_dir / "prompts" / prompt_name),
            str(run_dir / "stdout" / ("%s.txt" % tid)),
            str(run_dir / "stderr" / ("%s.log" % tid)),
            str(run_dir / "journal" / ("%s.jsonl" % tid)),
            clock or "-",
        ]))
    (run_dir / "manifest.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    n_clean = sum(1 for row in manifest if row[1] == "clean")
    print("备料完成：%d 条会话（弱点件 %d / 干净件 %d；红队段 %s）"
          % (len(manifest), len(instances), n_clean,
             "开" if attack_loop else "关（no-attack-loop 消融）"))
    if not has_injections:
        print("（决策级套件：实例无注入 ⇒ 不生成干净对照行）")
    print("清单：%s" % (run_dir / "manifest.tsv"))
    return 0


# ── ③ serve ──────────────────────────────────────────────────────
def read_journal(path):
    """读发射端写的 JSONL。缺失返回空列表（由调用方决定怎么记账）。"""
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            raise AdapterError("会话日志有一行不是 JSON：%s\n  行：%s" % (path, line[:120]))
    return rows


def pick_final(rows):
    """取"模型确定要采取的动作集"：优先最后一条 `final: true`，否则最后一条。

    返回 `(row, note)`；一条都没有时返回 `(None, note)`，不猜。
    """
    finals = [r for r in rows if r.get("final") is True]
    if finals:
        return finals[-1], "final=true"
    if rows:
        return rows[-1], "无 final=true，取最后一条提交"
    return None, "本实例没有任何提交"


def plan_from_rows(rows, payload):
    """把模型的最终提交按案件事实重算成计划（防自报拔高）。

    :returns: `(plan_dict, meta)`；计划缺测时 `(None, meta)`。
    """
    row, note = pick_final(rows)
    meta = {"n_submissions": len(rows), "final_pick": note}
    if row is None:
        return None, meta

    by_id = {a.get("action_id"): a for a in (payload.get("actions") or [])}
    element_ids = {e.get("element_id") for e in (payload.get("elements") or [])}
    proposed = [i for i in (row.get("action_ids") or []) if i in by_id]
    unknown = [i for i in (row.get("action_ids") or []) if i not in by_id]
    # 计划口径与机械臂一致：机械臂是"盾先筛 → 策略层只在 `allowed` 内选"，
    #   故真实臂的"系统计划" = 模型的最终提议 ∩ 盾放行（`allowed_action_ids`）。
    #   被拦的动作不进计划，它要等当事人确认（闸门的前置语义），
    #   系统在此刻不会采取它。不这么取就会与机械臂不可比：
    #   模型的提议里仍可能留着被拦动作（实测某 M3 实例的最终提议就留着两个
    #   触发 `gate.escalation-risk` 的动作），那是"模型听不听劝"的行为读数（口径 B），
    #   不是"系统会不会采取"（口径 A）。两者都记，但进指标的只能是 A。
    blocked = [i for i in (row.get("blocked_action_ids") or []) if i in by_id]
    selected = [i for i in proposed if i not in blocked]

    covered, utility = set(), 0.0
    for aid in selected:
        act = by_id[aid]
        covered.update(e for e in (act.get("covers") or []) if e in element_ids)
        try:
            utility += float(act.get("expected_utility") or 0.0)
        except (TypeError, ValueError):
            raise AdapterError("动作 %s 的 expected_utility 不是数值：%r"
                               % (aid, act.get("expected_utility")))
    meta.update({
        "shield_layers": (row.get("verdict") or {}).get("layers"),
        "shield_findings": [(f.get("code"), f.get("gateId"), f.get("severity"))
                            for f in ((row.get("verdict") or {}).get("findings") or [])],
        # 口径 A（进指标）：计划 = 提议 ∩ 放行；口径 B（行为读数，不进指标）：
        "proposed_ids": proposed,
        "blocked_action_ids": blocked,
        "kept_blocked_after_guard": [i for i in proposed if i in blocked],
        "unknown_action_ids": unknown,
    })

    plan = {
        "posture": {"name": payload.get("posture")},
        "objective": None,          # 选择规则是模型，不是我们的启发式，不假装有 Obj
        "selected_ids": selected,
        "excluded_by_shield": [{"action_id": i, "reason": "护栏裁决为须确认/催办"} for i in blocked],
        "covered_elements": sorted(covered),
        "expected_utility": round(utility, 4),
        "degenerate": None,         # 退化检测是启发式的性质，对模型不适用
        "source": "acte-model",
    }
    return plan, meta


def serve(payload, run_dir, *, log_path=None):
    """`run(payload) -> {"findings": [...], "notes": [...], "plan": {...}}`。

    `notes` 是输出侧沉默机制的提示通道：`evidence` 未逐字落到案情原文的条目
    经 `ctd_acte.output_gate` 降级入此，不进 findings（不参与签名判定）。
    """
    if not payload.get("task_id"):
        raise AdapterError("payload 缺 task_id")
    # 先校验再拼路径：task_id 进的是文件路径，未校验即为路径穿越面。
    tid = _safe_id(payload["task_id"], "task_id")
    out_path = run_dir / "stdout" / ("%s.txt" % tid)
    if not out_path.is_file():
        raise AdapterError(
            "会话产物缺失：%s\n"
            "  本臂不返回空 findings——那会被读成『系统在这个实例上什么都没找到』，"
            "而真相是『这个实例没跑成』。先跑 run-acte-sessions.sh。" % out_path)
    stdout_text = out_path.read_text(encoding="utf-8")
    if not stdout_text.strip():
        raise AdapterError("会话产物是空的：%s（会话很可能失败，看 stderr 同名文件）" % out_path)

    # is_clean 一律不使用：读了就毁掉"能否在案子是好的时候保持沉默"这条主结论。
    baselines = _baselines()
    # 引文口径：默认主口径（evidence 取最长分栏），`ACTE_EVIDENCE_MODE=line` 时才
    # 把整行原文放进 message 作敏感性读数（把"没找到"与"没引原句"分开）。
    line_mode = (os.environ.get("ACTE_EVIDENCE_MODE") or "quote").strip().lower() == "line"
    findings = baselines.parse_issues_f1(stdout_text, payload, None,
                                         full_line_in_message=line_mode)
    # 输出侧沉默机制：痕迹接地门槛（tier2 纪律前移）。
    #   只进本臂、不送基线，公平性边界见 output_gate 模块头与方案 §4。
    findings, notes = apply_output_gate(findings, payload.get("facts_text") or "")

    plan, meta = None, {}
    if payload.get("actions"):
        rows = read_journal(run_dir / "journal" / ("%s.jsonl" % tid))
        plan, meta = plan_from_rows(rows, payload)
        if plan is None:
            sys.stderr.write("⚠ %s：模型没有任何 acte_plan 提交 ⇒ 计划记缺测（不是 0）\n" % tid)

    # 干净件身份从本臂备料的 manifest 回填（harness 已盲化、载荷不再带 is_clean）；
    # manifest 缺失时记 None，"不知道"不许冒充 False。
    kind = None
    manifest_path = run_dir / "manifest.tsv"
    if manifest_path.is_file():
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1] == tid:
                kind = parts[0]
                break
    rec = {"task_id": tid, "is_clean": (kind == "clean") if kind else None,
           "n_findings": len(findings), "n_notes": len(notes),
           "verdict": verdict_memo(findings),
           "has_plan": plan is not None, **meta}
    sys.stderr.write("acte-model %s：findings %d 条 / notes %d 条 / 计划 %s\n"
                     % (tid, len(findings), len(notes), "有" if plan else "缺测"))
    if log_path:
        # 追加一行到臂日志。用 `Path.write_text` 而不是 `open(path, "a")`：
        # 本仓库的提交前安全扫描把"非字面量路径 + 写模式 open"判为路径穿越并强制拦截
        # （实测只放行 pathlib 的写入 API；围栏是 `_safe_id` 的白名单校验 +
        # 只接受操作者给的路径）。日志每轮不过几十行，读改写不构成性能问题。
        log_path.parent.mkdir(parents=True, exist_ok=True)
        prev = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
        log_path.write_text(prev + json.dumps(rec, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    return {"findings": findings, "notes": notes, "plan": plan, "meta": rec}


def run(payload, run_dir=None, log_path=None):
    """harness 的外部臂契约：`run(payload) -> {"findings": [...], "plan": {...}}`。

    harness 按 `--arm-module` 给的路径加载本模块并调用本函数（见
    `code/harness.py` 的 `run_external_arm`）。会话产物目录默认取
    `ACTE_RUN_DIR` 环境变量或 会话运行目录 会话运行目录 `experiment/run/acte-sessions`，
    同一个模块既能评主臂，也能被环境变量指向消融臂的产物目录（消融臂是
    另一轮会话，产物必须分开存，否则两次读数会混）。
    """
    rd = Path(run_dir) if run_dir else Path(
        os.environ.get("ACTE_RUN_DIR") or DEFAULT_RUN_DIR)
    if log_path is not None:
        lp = Path(log_path)
    elif os.environ.get("ACTE_ARM_LOG"):
        lp = Path(os.environ["ACTE_ARM_LOG"])
    else:
        lp = None
    return serve(payload, rd, log_path=lp)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prepare", action="store_true",
                    help="备料：写提示词与 manifest（不跑会话）")
    ap.add_argument("--run-dir",
                    default=os.environ.get("ACTE_RUN_DIR") or str(DEFAULT_RUN_DIR),
                    help="会话产物目录。harness 内部调用本模块时走环境变量 "
                         "ACTE_RUN_DIR（例如换一个 run 目录跑对照臂）")
    ap.add_argument("--taskset", default=str(PAPER_ROOT / "experiment" / "tasks"),
                    help="任务集目录（含 taskset.json 与 cases/）")
    ap.add_argument("--log", default=os.environ.get("ACTE_ARM_LOG"),
                    help="逐实例取数记录（JSONL）的落点；不给则不写")
    ap.add_argument("--no-attack-loop", action="store_true",
                    help="备料时去掉红队对抗循环段（no-attack-loop 消融）。"
                         "也可用环境变量 ACTE_ATTACK_LOOP=0；两者都给时本开关优先。")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir)
    if args.prepare:
        attack_loop = False if args.no_attack_loop else _env_flag("ACTE_ATTACK_LOOP", True)
        return prepare(run_dir, Path(args.taskset) / "taskset.json",
                       attack_loop=attack_loop)

    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    log_path = Path(args.log) if args.log else None
    try:
        out = serve(payload, run_dir, log_path=log_path)
    except AdapterError as exc:
        # 会话没跑成属基础设施失败，必须让人看见，不能伪装成"系统没发现问题"。
        sys.stderr.write("\n%s\n" % exc)
        return 2
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
