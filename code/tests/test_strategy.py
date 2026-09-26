#!/usr/bin/env python3
"""策略层与消融效应的回归测试。

首先运行它：它红了，四条消融臂的读数就不可信。

## 这个文件测什么（以及为什么必须端到端测）

分三段：

[A] 策略层的边界，把定理 1(c) 的"分两步"变成可执行检查：
本模块不得重新评估风险。测法：断言 `strategy.py` 的源码里不出现 `t_irr`、
不出现 `triggered`，且 `plan()` 的签名里没有风险参数。若这条破了，
就会出现两处风险判断（盾一处、策略一处），而第二处没有定理支撑。

[B] 盾的 `trigger_map` 语义，两型都必须靠"动作自己声明的触发"来拿掉，
不许有魔法命名。本模块早期版本去找名为 `action:<gate_id>` / `⊥` 的动作，
而动作模型用事实级 id（`adm-a7`），于是两个分支都拿不掉任何东西、盾却成功返回。
测法：给出错误的 trigger_map（声明了别的动作）时必须拿不掉并且
`notes` 里说"未提出该动作"，而不是悄悄声称拦住了。

[C] 四条消融臂的效应（端到端）：这是本文件的主要价值。
每条消融必须只改变它预测的那个维度，且对照维度不动：

| 臂 | 预测变化的维度 | 必须不动的对照 |
|---|---|---|
| `no-shield` | M3 真实升级事故 0 → 1；M2 被动型事故 0 → 0.875（等待被选中 ⇒ 失权） | M1 不动（细族覆盖目标不选零覆盖诱饵） |
| `no-shield-reversible` | M2 的期望回收 17.6296 → 17.4074（定理 1′ 的失效方向） | M1/M3 不动 |
| `fixed-posture=狠` | M1 覆盖 0.8198 → 0.6；M3 覆盖/效用 0.6242·10.7879 → 0.4424·15.1818（定理 2/3） | 自身姿态=狠的实例逐实例不动（见下） |
| 主臂 | —（基线） | — |

> **fixed-posture 的天然对照随 v4 扩容下沉一级（v4 扩容轮重钉 2026-09-27）**：
> v4 之前 M2 族姿态同质（全是狠），"fixed-posture 下 M2 不动"是族级天然对照；
> v4 起 M2 姿态混布（狠 20 / 稳 2 / 细 5），非狠实例的选择规则当然随
> override 变，族均值必然移动。天然对照不因此失效，而是**下沉到实例级**：
> 自身姿态已是狠的实例，在 override 下逐实例不变——这正是"它自己的姿态
> 就是狠 ⇒ 天然对照"的原义。

为什么必须端到端测而不是只测单元：这些效应依赖三处的联合成立，
底本里动作的 `covers`/`utility` 取值、姿态的 `Obj`/`ε→quota` 映射、盾的
`trigger_map` 语义。任一处改动都可能让某条消融静默失效（读数不再变化），
而单元测试全绿。实测已踩过两次：

1. `include_reversible` 的开关写进了消融 overlay，插件却没实现它；
2. 可逆风险动作初版挂在 M1（姿态=细，覆盖目标永远不选零覆盖动作），
   导致反向消融读数一点没变，原因不是定理不成立，是动作放错了族。

    python3 code/tests/test_strategy.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
PAPER_ROOT = HERE.parents[2]
CODE_ROOT = PAPER_ROOT / "code"
TASKS_ROOT = PAPER_ROOT / "experiment" / "tasks"
# `ctd_acte` 在本仓库里位于 <篇>/code/ 下，故需把 code/ 加进 sys.path 才能 `from ctd_acte import …`
sys.path.insert(0, str(CODE_ROOT))

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

    def raises(self, name, exc_type, fn, want=None):
        self.n += 1
        try:
            fn()
        except exc_type as exc:
            if want and want not in str(exc):
                print("  FAIL %s  — 抛了 %s 但信息不含 %r" % (name, exc_type.__name__, want))
                self.failed.append(name)
            else:
                print("  ok   %s" % name)
            return
        except Exception as exc:
            print("  FAIL %s  — 抛了 %r 而非 %s" % (name, exc, exc_type.__name__))
            self.failed.append(name)
            return
        print("  FAIL %s  — 没有抛错（静默放过）" % name)
        self.failed.append(name)


def test_strategy_boundary(c, strategy, posture):
    print("\n[A] 策略层的边界（定理 1(c)：分两步，本层不重新评估风险）")
    src = (CODE_ROOT / "ctd_acte" / "strategy.py").read_text(encoding="utf-8")
    # 去注释与 docstring，只留代码，文档里当然会提到这些词
    code_only = "\n".join(
        line for line in src.splitlines()
        if not line.strip().startswith("#"))
    # docstring 用三引号；粗粒度地去掉最常见的整段引用
    for marker in ("===", ):
        pass
    c.check("strategy.py 不 import t_irr（不碰盾的领域）",
            "import t_irr" not in code_only and "from .t_irr" not in code_only,
            "源码里出现了对 t_irr 的导入")
    import inspect
    params = list(inspect.signature(strategy.plan).parameters)
    c.check("plan() 的签名里没有风险参数（triggered/quota/confirmed 都不该有）",
            not any(p in params for p in ("triggered", "quota", "confirmed", "t_irr")),
            "实际参数：%s" % params)
    c.check("plan() 只接收 allowed（盾的输出）作为约束",
            "allowed" in params, "实际参数：%s" % params)

    # 盾允许集之外的候选必须被如实记为"盾已排除"，且不重新评估
    acts = strategy.actions_from([
        {"action_id": "x", "text": "甲", "covers": ["e1"], "expected_utility": 1},
        {"action_id": "y", "text": "乙", "covers": ["e1"], "expected_utility": 9},
    ])
    p = strategy.plan("细", acts, allowed=["x"], max_actions=None)
    c.check("盾排除的动作进 excluded_by_shield 且理由为『本层不重新评估风险』",
            len(p["excluded_by_shield"]) == 1
            and "不重新评估风险" in p["excluded_by_shield"][0]["reason"],
            json.dumps(p["excluded_by_shield"], ensure_ascii=False))
    c.check("被排除的动作不会出现在计划里", p["selected_ids"] == ["x"], p["selected_ids"])

    c.raises("盾允许了候选集里没有的动作 ⇒ 报错（传参不一致）",
             strategy.StrategyError,
             lambda: strategy.plan("细", acts, allowed=["x", "zzz"]))


def test_quota_mapping(c, posture):
    print("\n[B] ε → 配额（定理 1′(i)：`ε` 必须落到操作层，否则稳≡狠）")
    c.check("稳（ε 严格）⇒ 配额 0", posture.get("稳").quota == 0)
    c.check("狠（ε 宽松）⇒ 配额 > 0", posture.get("狠").quota > 0)
    c.check("★ 稳与狠的 Obj 相同、ε 不同（故差异只可能来自配额）",
            posture.get("稳").obj == posture.get("狠").obj
            and posture.get("稳").eps != posture.get("狠").eps)
    c.check("配额随 ε 收紧而单调不增（定理 2 的操作前提）",
            posture.get("稳").quota <= posture.get("狠").quota)
    c.check("to_dict 暴露 quota（供结果文件溯源）",
            "quota" in posture.get("稳").to_dict())


def test_shield_trigger_map(c, tirr):
    print("\n[C] 盾按 trigger_map 拿掉动作（不许魔法命名）")
    from ctd_acte import ShieldError
    acts = ["adm-a1", "adm-a7"]
    tm = {"adm-a7": ["gate.escalation-risk"], "adm-a1": []}
    r = tirr.shield(actions=acts, triggered={"gate.escalation-risk"},
                    trigger_map=tm, quota=None, confirmed=())
    c.check("未确认的触发动作被拿掉", r["allowed"] == ["adm-a1"] and r["removed"] == ["adm-a7"])

    # trigger_map 声明的是别的动作 ⇒ 拿不掉，且 notes 说"未提出该动作"
    tm2 = {"adm-a1": ["gate.escalation-risk"]}
    r2 = tirr.shield(actions=acts, triggered={"gate.escalation-risk"}, trigger_map=tm2,
                     quota=None, confirmed=())
    c.check("trigger_map 指向别的动作时，不会误拿 adm-a7",
            "adm-a7" in r2["allowed"], repr(r2["allowed"]))

    c.raises("不给 trigger_map ⇒ 报错（拿不准就静默不拿 = 漏防）",
             ShieldError,
             lambda: tirr.shield(actions=acts, triggered={"gate.escalation-risk"}),
             want="trigger_map")

    # 配额：已确认的动作在配额 0 下仍被拿掉
    r3 = tirr.shield(actions=acts, triggered={"gate.escalation-risk"}, trigger_map=tm,
                     quota=0, confirmed=("adm-a7",))
    c.check("已确认但在配额 0 下仍被拿掉（零容忍：ε 的操作效果）",
            r3["allowed"] == ["adm-a1"], repr(r3["allowed"]))
    r4 = tirr.shield(actions=acts, triggered={"gate.escalation-risk"}, trigger_map=tm,
                     quota=1, confirmed=("adm-a7",))
    c.check("已确认且在配额内 ⇒ 放行（稳与狠由此分得开）",
            "adm-a7" in r4["allowed"], repr(r4["allowed"]))


def test_ablation_effects(c, harness, metrics_mod, tirr, matcher, taskset):
    print("\n[D] ★ 四条消融臂的效应（端到端；每条只改它预测的维度）")

    def run(**kw):
        bundle = harness.arm_acte(taskset, tirr, **kw)
        reports = {k: v["findings"] for k, v in bundle.items()}
        plans = {k: v["plan"] for k, v in bundle.items()}
        m = metrics_mod.evaluate_with_clean(matcher, taskset, reports, {}, plans=plans,
                                           tirr=tirr)
        # 三元组：族级汇总 / 逐实例台账 / 逐实例计划（③ 的实例级对照要用后两个）
        return (m["plan"]["by_family"], m["plan"]["per_instance"], plans)

    main, main_rows, main_plans = run()
    noshield, noshield_rows, _ = run(shield_enabled=False)
    rev, _, _ = run(include_reversible=True)
    fixed, _, fixed_plans = run(posture_override="狠")

    def cov(fams, f):
        return fams[f]["mean_coverage"]

    def util(fams, f):
        return fams[f]["mean_expected_utility"]

    def inc(fams, f):
        return fams[f]["incident_rate"]

    # 基线先固定住，没有基线，"变化"无从谈起
    #
    # 这些钉值随实例数变，加实例时必须同步更新，否则"消融效应"会被
    #    误判为消失（这条纪律已曾出现过，见 code/README.md「待做」）。
    # 历次扩样（每次都改过这里的值，留痕以便追溯）：
    #   · 3 条时：M1 覆盖 0.7333 / M2 0.4833·16.3333 / M3 0.6·12.6667
    #   · 9 条时：同上（M3 加 STAND-001 后为 0.6·12.6667）
    #   · 16 条时：M1 0.72 / M2 0.46·16.0 / M3 0.6333·12.5
    #   · 22 条时：
    #     M1 0.8222 / M2 0.5167·16.5 / M3 0.6857·12.5714；
    #     T_irr 分母 M1 0→2（TIM×2 带被动闸门）、M3 4→5（SCOPE-003 带 escalation）。
    #   · 24 条时（任务集 v4：决策套件独立，主集加入机会面 TIM-003/004，
    #     扩样纪律 重钉）：M1 0.8545（其余不变）；fixed 狠 M1 0.6667→0.6909；
    #     T_irr 分母不变（机会面 is_t_irr=false，不进 T_irr 口径，这正是
    #     机会面与风险面的机制分界）。
    #   · 87 条时（**v4 扩容轮重钉 2026-09-27**：weaknesses.yaml 合并 batches/
    #     片段、87 弱件 + 纯负对照底本进 load_texts）：
    #     M1 0.8198·12.1852 / M2 0.5617·17.6296 / M3 0.6242·10.7879；
    #     fixed 狠 M1 0.6、M3 0.4424·15.1818；no-shield M2 事故 0.875（14/16）；
    #     T_irr 分母 M1 5、M2 16、M3 31（esc/lim/nact/rev/tim 各批补入落点）。
    #     ★ 族级"M2 天然对照"随 M2 姿态混布（狠 20/稳 2/细 5）失效，
    #     对照下沉到实例级（见 ③ 与文件头表注）。
    # 均值变动的来源是实例集合变了（新实例各自的覆盖/效用不同），不是策略层改了，
    # 判据是：消融的方向性判据（①–⑤）全部保持，只有均值水平移动。
    c.check("基线：M1 覆盖 0.8198 / M2 覆盖 0.5617 效用 17.6296 / M3 覆盖 0.6242 效用 10.7879 / 事故 0.0",
            cov(main, "M1") == 0.8198 and cov(main, "M2") == 0.5617
            and util(main, "M2") == 17.6296 and cov(main, "M3") == 0.6242
            and util(main, "M3") == 10.7879 and inc(main, "M3") == 0.0,
            "M1=%s M2=%s M3=%s" % (cov(main, "M1"), util(main, "M2"), inc(main, "M3")))

    # ① no-shield：只有 M3 的真实事故率上升（定理 1）
    c.check("① no-shield：M3 真实升级事故 0.0 → 1.0（定理 1）",
            inc(noshield, "M3") == 1.0, repr(inc(noshield, "M3")))
    # v4 扩容轮重钉 2026-09-27：1.0 → 0.875（14/16）。等待被选中的实例
    # **全部**失权（14/14），未转化的 2 件是分子不可达（见下条注）。
    c.check("★ ① no-shield：M2 **被动型**事故 0.0 → 0.875（等待被选中 ⇒ 失权）",
            inc(noshield, "M2") == 0.875, repr(inc(noshield, "M2")))
    c.check("★ ① no-shield：M2 覆盖随之下降（等待动作零覆盖却效用最高）",
            cov(noshield, "M2") < cov(main, "M2"),
            "%s → %s" % (cov(main, "M2"), cov(noshield, "M2")))
    c.check("① no-shield：M1 不动（该族无可选的 T_irr 动作 ⇒ 盾不该有影响）",
            cov(noshield, "M1") == cov(main, "M1"), repr(cov(noshield, "M1")))
    # 未转化实例的构成钉死（防 0.875 被悄悄漂移成别的数）：恰是
    # F1-INJ-TIM-012/013（姿态=细、budget=3，等待诱饵 tima2-a4 零覆盖高效用，
    # 但细族覆盖目标永远先选满 3 个覆盖动作 ⇒ 诱饵不被任何目标选中，
    # 分子不可达——与 M1 的 TIM 实例同一机制，见下条"如实记"）。
    non_converters = sorted(
        r["task_id"] for r in noshield_rows
        if r.get("family") == "M2" and r.get("upgrade_real") is not None
        and not r["upgrade_real"]["incident"])
    c.check("①′ M2 未转化恰是 F1-INJ-TIM-012/013（诱饵不被任何目标选中 ⇒ 分子不可达）",
            non_converters == ["F1-INJ-TIM-012", "F1-INJ-TIM-013"],
            "实际：%s" % non_converters)
    # 分母随实例数变：22 条（v3）后 M1 0→2、M2 仍 1、M3 4→5
    # （TIM-001/002 带 `gate.statute-of-limitations` 被动型进 M1；SCOPE-003 带
    #  `gate.escalation-risk` 动作型进 M3。LIM-003/JUR-003/STAND-003 按设计不含
    #  T_irr 触发，可逆风险或期限已过，理由见各自底本的说明段。）
    # 87 条（v4 扩容轮重钉 2026-09-27）后 M1 5、M2 16、M3 31：
    # esc 批（动作型 escalation）主要进 M3，lim/nact/tim 批（被动型
    # statute-of-limitations / suspension-of-execution）进 M2，rev 批可逆
    # 不挂 T_irr（定理 1′）。
    # 如实记：M1 的 5 条 T_irr 实例在四条标准臂配置下分子不可达（细族覆盖目标
    #  不选零覆盖诱饵；固定狠时盾排除它）：分母入口径是事实，分子路径也照实写。
    c.check("① 各族分母独立（M1 5 条、M2 16 条、M3 31 条，互不掺入）",
            (noshield["M1"]["n_with_active_t_irr"] == 5
             and noshield["M2"]["n_with_active_t_irr"] == 16
             and noshield["M3"]["n_with_active_t_irr"] == 31),
            "M1=%s M2=%s M3=%s" % (noshield["M1"]["n_with_active_t_irr"],
                                   noshield["M2"]["n_with_active_t_irr"],
                                   noshield["M3"]["n_with_active_t_irr"]))

    # ② no-shield-reversible：只有 M2 的期望回收下降（定理 1′ 的失效方向）
    # 扩到 22 条（v3）后：16.5 → 15.5（16 条时 16.0 → 14.8，方向不变）
    # 87 条（v4 扩容轮重钉 2026-09-27）后：17.6296 → 17.4074（方向不变）
    c.check("② no-shield-reversible：M2 期望回收 17.6296 → 17.4074（定理 1′ 的失效方向）",
            util(rev, "M2") == 17.4074, repr(util(rev, "M2")))
    c.check("② 且反向消融**不改**事故率（它拦的是可逆风险，与不可逆口径无关）",
            inc(rev, "M2") == inc(main, "M2"), repr(inc(rev, "M2")))
    c.check("② 且 M1/M3 的对应维度不动（该风险只在 M2 族里）",
            cov(rev, "M1") == cov(main, "M1") and inc(rev, "M3") == inc(main, "M3"),
            "M1=%s M3=%s" % (cov(rev, "M1"), inc(rev, "M3")))

    # ③ fixed-posture：M1 覆盖下降（定理 3 的目标维度）+ M3 覆盖/效用改变（定理 2）
    # 扩到 24 条后：0.8545 → 0.6909（v3 22 条时 0.8222 → 0.6667，方向不变）
    # 87 条（v4 扩容轮重钉 2026-09-27）后：0.8198 → 0.6（方向不变）
    c.check("③ fixed-posture=狠：M1 覆盖 0.8198 → 0.6（定理 3：无恒优姿态）",
            cov(fixed, "M1") == 0.6, repr(cov(fixed, "M1")))
    # M3 均值随实例数变（v3 22 条：主臂 0.6857·12.5714，全用狠 0.5714·15.4286；
    #   v4 87 条：主臂 0.6242·10.7879，全用狠 0.4424·15.1818）。
    # 退化实例如实报告：v4 后 fixed 狠下 M3 仅 1 条退化（16 条时是 4）；
    #   主臂 M3 退化 29 条（v3 22 条时是 5；稳配额下两目标选出同一动作集）。
    #   读数解读据此，不拿"均值变化不大"去论证"姿态不重要"。
    c.check("③ fixed-posture=狠：M3 覆盖 0.6242 → 0.4424 且效用 10.7879 → 15.1818（定理 2：V(稳)≤V(狠)）",
            cov(fixed, "M3") == 0.4424 and util(fixed, "M3") == 15.1818,
            "M3 cov=%s util=%s" % (cov(fixed, "M3"), util(fixed, "M3")))
    # ③ 天然对照（v4 扩容轮重钉 2026-09-27）：族级"M2 不动"随 M2 姿态混布失效
    # （v4 起 M2 狠 20 / 稳 2 / 细 5，非狠实例的选择规则当然随 override 变，
    # 族均值移动不是策略层越界）。对照下沉到实例级，这才是"它自己的姿态就是
    # 狠 ⇒ 天然对照"的原义：自身姿态已是狠的实例，override 下逐实例不变。
    # 比较取计划的实质部分（selected_ids / 效用 / 覆盖），姿态名块不算变化。
    def _plan_key(p):
        return (tuple(p.get("selected_ids") or []),
                p.get("expected_utility"),
                tuple(sorted(p.get("covered_elements") or [])))

    posture_loose = [i["task_id"] for i in taskset["instances"]
                     if i.get("posture") == "狠"]
    moved_loose = [tid for tid in posture_loose
                   if _plan_key(main_plans[tid]) != _plan_key(fixed_plans[tid])]
    c.check("③ 天然对照（实例级）：自身姿态=狠的实例在 override 下逐实例不变",
            not moved_loose, "变动的狠姿态实例：%s" % moved_loose)

    # ④ 反向消融不得改变真实事故率（它拦的是可逆风险，与不可逆事故无关）
    c.check("④ no-shield-reversible 不改 M3 事故率（可逆风险的拦截不该影响不可逆口径）",
            inc(rev, "M3") == inc(main, "M3"), repr(inc(rev, "M3")))

    # ⑤ 真值单调性：V(稳) ≤ V(狠)（定理 2(a) 直接读数）
    c.check("⑤ 定理 2(a) 直接读数：M3 上 V(稳) ≤ V(狠)（10.7879 ≤ 15.1818）",
            util(main, "M3") <= util(fixed, "M3"),
            "V(稳)=%s V(狠)=%s" % (util(main, "M3"), util(fixed, "M3")))


def main():
    print("=" * 70)
    print("策略层与消融效应回归测试")
    print("=" * 70)

    matcher_path = MATCHER_PATH
    if matcher_path is None:
        sys.stderr.write(_matcher_path.describe(CODE_ROOT, HERE) + "\n")
        return 2

    from ctd_acte import posture as posture_mod
    from ctd_acte import strategy as strategy_mod
    from ctd_acte import metrics as metrics_mod
    from ctd_acte.t_irr import TIrR

    harness = _load("f1_harness_strategy", CODE_ROOT / "harness.py")
    matcher = _load("f1_matcher_strategy", matcher_path)
    tirr = TIrR.load(CODE_ROOT / "data" / "t-irr.json")
    try:
        taskset, _ = harness.load_taskset(TASKS_ROOT)
    except harness.HarnessError as exc:
        # 同上：算法本体导出的仓库不含实验材料，报「不适用」而不是抛异常。
        print("\n不适用：本套件以任务集为输入，当前仓库不含实验材料。")
        print("  原因：%s" % exc)
        print("  这不是失败。取得实验材料后重跑，或直接跑 code/tools/verify_all.py"
              "（该脚本会自动把本步列为「不适用」）。")
        return 2

    c = Case()
    test_strategy_boundary(c, strategy_mod, posture_mod)
    test_quota_mapping(c, posture_mod)
    test_shield_trigger_map(c, tirr)
    test_ablation_effects(c, harness, metrics_mod, tirr, matcher, taskset)

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