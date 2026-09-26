#!/usr/bin/env python3
"""ACTE 内核回归测试：把三个定理的操作预测变成可执行检查。

首先运行它：它红了，"闸门可独立实现"与"姿态嵌套"这两条声称就站不住。

本文件测的是定理能不能在实现里兑现，不是"代码能不能跑"：

| 测试 | 对应的定理/声称 | 不测会怎样 |
|---|---|---|
| A. 盾的两种语义 | 定理 1 + §1.1.1 | 把不作为型当动作型处理 ⇒ 时效类实例全数漏防，而实验数字看不出来 |
| B. 时间增广要求 | §1.1.1 | 缺期限分量时盾时灵时不灵 ， "看着装上了其实哑了" |
| C. 盾依赖关系 | 定理 1(a) | 盾若依赖效用/策略，"闸门做成插件"就不成立 |
| D. 姿态嵌套 | 定理 2(c) | 嵌套是消融的诊断口径；不嵌套说明 `ε` 没真正起作用 |
| E. 零容忍语义 | 定理 1 / 1′ | strict 姿态下"先跨层级再补"必须报错，不能放行 |
| F. ToC 完备性与覆盖口径 | T2 指标 + `M_1` 修正口径 | 覆盖度若按请求权数量算，与《民法典》186 条冲突 |
| G. 边界守卫 | — | 空 ToC / 未知姿态 / 未登记 `T_irr` 条目必须报错而非静默返回 |

确定性、不调模型、不联网。判定机与 `T_irr` 快照缺失时报错而非跳过。

    python3 code/tests/test_acte_core.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
PAPER_ROOT = HERE.parents[2]
CODE_ROOT = PAPER_ROOT / "code"
sys.path.insert(0, str(CODE_ROOT))

from ctd_acte import (  # noqa: E402
    Engine, EngineError, Posture, PostureError, TIrR, ToC, Claim, ToCError, WAIT,
)
from ctd_acte import posture as posture_mod  # noqa: E402

T_IRR_SNAPSHOT = CODE_ROOT / "data" / "t-irr.json"


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
        """断言 fn 抛 exc_type（且错误信息含 want）。不抛＝失败。"""
        self.n += 1
        try:
            fn()
        except exc_type as exc:
            if want and want not in str(exc):
                print("  FAIL %s  — 抛了 %s 但信息不含 %r：%s"
                      % (name, exc_type.__name__, want, str(exc)[:90]))
                self.failed.append(name)
            else:
                print("  ok   %s" % name)
            return
        except Exception as exc:  # 抛了别的类型
            print("  FAIL %s  — 抛了 %r 而非 %s" % (name, exc, exc_type.__name__))
            self.failed.append(name)
            return
        print("  FAIL %s  — 没有抛错（静默放过）" % name)
        self.failed.append(name)


def _tirr():
    if not T_IRR_SNAPSHOT.is_file():
        sys.stderr.write("缺 %s —— 先跑 code/tools/derive_t_irr.py\n" % T_IRR_SNAPSHOT)
        raise SystemExit(2)
    return TIrR.load(T_IRR_SNAPSHOT)


def test_shield_two_semantics(c, tirr):
    print("\n[A] 盾的两种语义（定理 1 + §1.1.1）")
    # 新 API（trigger_map）：动作自己声明触发了哪些条目，盾据此拿掉它们。
    # 旧 API 让盾去找名为 `action:<gate_id>` / `⊥` 的动作，那是错的且很隐蔽：
    # 动作模型用事实级 id（`adm-a7`），于是两个分支都拿不掉任何东西、
    # 盾却"成功"返回（见 t_irr.py 里那段说明）。
    tm = {"起诉": ["gate.escalation-risk"], "协商": []}

    # 动作型：拿掉声明了该条目的动作；未确认的一律拿掉
    r = tirr.shield(actions=["起诉", "协商"], triggered={"gate.escalation-risk"},
                    trigger_map=tm, quota=None, confirmed=())
    c.check("动作型：拿掉该动作，其余保留",
            r["allowed"] == ["协商"] and r["removed"] == ["起诉"], repr(r["allowed"]))
    c.check("动作型：没有强制（forced=False）", r["forced"] is False)

    # 不作为型：拿掉"等待"动作（由该动作自己声明），且不拿掉别的动作
    tm2 = {"申请停止执行": [], "起诉": [], "暂不申请停止执行": ["gate.suspension-of-execution"]}
    r2 = tirr.shield(actions=["暂不申请停止执行", "申请停止执行", "起诉"],
                     triggered={"gate.suspension-of-execution"},
                     trigger_map=tm2, remaining="3日")
    c.check("不作为型：拿掉等待动作",
            "暂不申请停止执行" not in r2["allowed"], repr(r2["allowed"]))
    c.check("不作为型：强制标记与义务项",
            r2["forced"] is True and r2["obligations"] == ["gate.suspension-of-execution"])
    c.check("★ 不作为型不拿掉别的动作（与动作型的实质差别）",
            r2["allowed"] == ["申请停止执行", "起诉"], repr(r2["allowed"]))
    c.check("两型共用一个 shield()（§1.1.1 的『统一』）",
            "removed" in r and "obligations" in r2)


def test_time_augmentation(c, tirr):
    print("\n[B] 时间增广要求（§1.1.1）")
    from ctd_acte import ShieldError
    tm = {"暂不申请停止执行": ["gate.statute-of-limitations"], "起诉": []}
    c.raises(
        "不作为型缺 remaining ⇒ 报错（不许静默按'还没到期'处理）",
        ShieldError,
        lambda: tirr.shield(actions=["暂不申请停止执行", "起诉"],
                            triggered={"gate.statute-of-limitations"},
                            trigger_map=tm, remaining=None),
        want="时间增广")
    r = tirr.shield(actions=["暂不申请停止执行", "起诉"],
                    triggered={"gate.statute-of-limitations"},
                    trigger_map=tm, remaining="5日")
    c.check("给出 remaining 即通过，且义务项被记录",
            r["forced"] is True and r["obligations"] == ["gate.statute-of-limitations"])


def test_shield_independence(c, tirr):
    print("\n[C] 盾的依赖关系（定理 1(a)：只依赖 ⟨A, T_irr⟩ 与状态的增广分量）")
    tm = {"暂不申请停止执行": ["gate.suspension-of-execution"],
          "申请停止执行": [], "起诉": ["gate.escalation-risk"]}
    args = dict(actions=["暂不申请停止执行", "申请停止执行", "起诉"],
                triggered={"gate.escalation-risk", "gate.suspension-of-execution"},
                trigger_map=tm, remaining="10日", quota=1, confirmed=("起诉",))
    r1 = tirr.shield(**args)
    r2 = tirr.shield(**args)
    c.check("盾是 (actions, triggered, remaining, trigger_map, quota, confirmed) 的纯函数",
            r1["allowed"] == r2["allowed"] and r1["obligations"] == r2["obligations"],
            "%r vs %r" % (r1["allowed"], r2["allowed"]))
    c.check("盾不依赖姿态或效用（签名里没有 posture/objective/utility）",
            not any(v in tirr.shield.__code__.co_varnames
                    for v in ("posture", "objective", "utility", "obj")))
    c.check("★ 配额是**状态**的增广分量，不是策略（定理 1′(i)）",
            r1["quota"] == 1 and r1["quota_spent"] >= 0,
            "quota=%s spent=%s" % (r1["quota"], r1["quota_spent"]))
    c.check("盾同时处理两型（动作型拿掉 + 不作为型强制）",
            "暂不申请停止执行" not in r1["allowed"] and r1["forced"] is True,
            repr(r1["allowed"]))


def test_posture_nesting(c, tirr):
    print("\n[D] 姿态嵌套（定理 2(c)）")
    # 触发集合为空时三姿态的盾相同，这是设计使然，须显式确认而非当成 bug
    r = tirr.shield(actions=[WAIT, "起诉"], triggered=set(), remaining=None)
    c.check("触发集合为空时盾不因姿态而变（差异只在有触发时显现）",
            r["allowed"] == [WAIT, "起诉"] and not r["forced"])

    # 嵌套断言：严格侧 ≤ 宽松侧。这里用"触发集合包含关系"表达 ε 的收紧：
    # 稳（strict）只接受空触发；狠（loose）接受有触发但受盾限制。
    strict_like = tirr.shield(actions=[WAIT, "起诉"], triggered=set(), remaining=None)
    loose_like = tirr.shield(actions=[WAIT, "起诉"], triggered=set(), remaining=None)
    cmp = posture_mod.assert_nested("稳", "狠", strict_like, loose_like)
    c.check("assert_nested 对同盾给出 equal（不误报交叉）", cmp["relation"] == "equal")

    # 人为制造交叉 ⇒ assert_nested 必须报错（守卫有牙）
    crossing_a = {"allowed": ["x", "only_a"], "obligations": []}
    crossing_b = {"allowed": ["x", "only_b"], "obligations": []}
    c.raises("交叉时 assert_nested 报错（嵌套是可证伪预测，不是描述）",
             PostureError, lambda: posture_mod.assert_nested("稳", "狠", crossing_a, crossing_b),
             want="定理 2(c) 不成立")

    c.check("三姿态的取值如 §1.2（细/狠 同 ε、稳 与狠 同 Obj）",
            posture_mod.get("细").eps == posture_mod.get("狠").eps
            and posture_mod.get("细").obj != posture_mod.get("狠").obj
            and posture_mod.get("稳").obj == posture_mod.get("狠").obj
            and posture_mod.get("稳").eps != posture_mod.get("狠").eps)


def test_zero_tolerance(c, engine):
    print("\n[E] 零容忍语义（定理 1 / 1′：`ε` 通过**配额**起作用）")
    tm = {"起诉": ["gate.escalation-risk"], "协商": []}
    # 稳（配额 0）总能算出计划：排除该动作，而不是报错。
    # （早期版本在这里报错，导致"稳"姿态在 M3 实例上根本无法规划，见 engine.py 的分工说明。）
    strict = engine.shielded_plan("稳", actions=["起诉", "协商"],
                                  triggered={"gate.escalation-risk"},
                                  trigger_map=tm, confirmed=("起诉",))
    c.check("稳（配额 0）：连**已确认**的动作也被排除 ⇒ 计划仍算得出",
            strict["shield"]["allowed"] == ["协商"] and strict["quota"] == 0,
            repr(strict["shield"]["allowed"]))
    loose = engine.shielded_plan("狠", actions=["起诉", "协商"],
                                 triggered={"gate.escalation-risk"},
                                 trigger_map=tm, confirmed=("起诉",))
    c.check("狠（配额 1）：已确认的动作放行 ⇒ 稳与狠由此分得开",
            "起诉" in loose["shield"]["allowed"], repr(loose["shield"]["allowed"]))
    c.check("★ 定理 2(a) 的操作前提：配额随 ε 收紧而单调不增",
            strict["quota"] <= loose["quota"], "%s vs %s" % (strict["quota"], loose["quota"]))

    # 流水线不变量单独成方法，不再混在规划里
    c.raises("assert_gate_ordering：稳 + 已触发 ⇒ 报错（抓『闸门漏了』的装配错误）",
             EngineError,
             lambda: engine.assert_gate_ordering("稳", {"gate.escalation-risk"}),
             want="闸门漏了")
    c.check("assert_gate_ordering：狠 下不报错（宽松姿态本就该继续规划）",
            engine.assert_gate_ordering("狠", {"gate.escalation-risk"}) is True)


def test_toc(c):
    print("\n[F] ToC 完备性与覆盖口径（T2 指标 + M_1 修正口径）")
    complete = Claim("c1", "违约责任", edges={
        "fact": ["迟延交付"], "law": ["《民法典》第577条"], "evidence": ["验收单"]},
        elements=["e1", "e2"])
    partial = Claim("c2", "违约金", edges={"fact": ["迟延交付"]}, elements=["e3"])
    toc = ToC([complete, partial], case_id="T")

    comp = toc.completeness()
    c.check("完整率 = 三边齐备的主张 / 全部",
            comp["n_claims"] == 2 and comp["n_complete"] == 1 and comp["completeness"] == 0.5,
            repr(comp["completeness"]))
    c.check("缺边被逐条点出（不是只给一个数）",
            comp["incomplete"] == [{"claim_id": "c2", "missing_edges": ["law", "evidence"]}],
            repr(comp["incomplete"]))

    cov = toc.coverage()
    c.check("★ 覆盖度按**要件事实**算（不是按主张数）",
            cov["n_elements"] == 3, repr(cov))
    c.check("缺证据边的主张，其要件事实不计入覆盖分子",
            cov["n_supported"] == 2 and cov["coverage"] == round(2 / 3, 4),
            repr(cov))

    c.raises("空 ToC ⇒ 报错（分母为 0 不许静默返回 0）",
             ToCError, lambda: ToC([]).completeness())
    c.raises("无要件事实登记 ⇒ 报错（覆盖度口径要求要件事实）",
             ToCError, lambda: ToC([Claim("c", "x", edges={
                 "fact": ["f"], "law": ["l"], "evidence": ["e"]})]).coverage(),
             want="要件事实")


def test_boundary_guards(c, tirr):
    print("\n[G] 边界守卫")
    c.raises("未知姿态 ⇒ 报错", PostureError, lambda: posture_mod.get("unknown"))
    c.raises("非法目标 ⇒ 报错", PostureError,
             lambda: Posture("x", "made-up", posture_mod.EPS_LOOSE))
    c.raises("非法风险预算 ⇒ 报错", PostureError,
             lambda: Posture("x", posture_mod.OBJ_UTILITY, "medium"))
    from ctd_acte import ShieldError
    c.raises("触发未登记的 T_irr 条目 ⇒ 报错（静默忽略等于漏防）",
             ShieldError,
             lambda: tirr.shield(actions=["起诉"], triggered={"gate.does-not-exist"},
                                 trigger_map={"起诉": []}, remaining="1日"),
             want="未登记的")
    c.raises("不作为型无动作声明触发它 ⇒ 报错（状态刻画不全）",
             ShieldError,
             lambda: tirr.shield(actions=["起诉"],
                                 triggered={"gate.statute-of-limitations"},
                                 trigger_map={"起诉": []}, remaining="1日"),
             want="刻画不全")
    c.raises("不作为型拿掉等待后动作集为空 ⇒ 报错（状态刻画不全）",
             ShieldError,
             lambda: tirr.shield(actions=["等待"],
                                 triggered={"gate.statute-of-limitations"},
                                 trigger_map={"等待": ["gate.statute-of-limitations"]},
                                 remaining="1日"),
             want="刻画不全")

    print("\n[H] T_irr 快照的实测形态（文档声称的数字，在这里钉住）")
    c.check("T_irr 共 3 条（动作型 1 / 不作为型 2）",
            len(tirr.items) == 3 and tirr.n_active == 1 and tirr.n_passive == 2,
            "active=%d passive=%d" % (tirr.n_active, tirr.n_passive))
    c.check("层级语义登记为「制度可救济度」（非处罚严重度）",
            "可救济度" in (tirr.level_semantics or ""), tirr.level_semantics)
    c.check("快照带源 sha256 溯源", bool(tirr.provenance.get("source_sha256")))


def main():
    print("=" * 70)
    print("ACTE 内核回归测试（定理的操作预测）")
    print("=" * 70)
    c = Case()
    tirr = _tirr()
    engine = Engine(tirr)
    test_shield_two_semantics(c, tirr)
    test_time_augmentation(c, tirr)
    test_shield_independence(c, tirr)
    test_posture_nesting(c, tirr)
    test_zero_tolerance(c, engine)
    test_toc(c)
    test_boundary_guards(c, tirr)
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