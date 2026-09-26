"""三层编排：一次送检 = 盾 → 策略 → 指标。

    T_irr 盾（只读 ⟨actions, T_irr⟩）  →  策略层（在盾内选动作）  →  指标

先算盾、再选动作，而不是把风险并进目标一起优化：定理 1(c) 给出的正是"分两步
即可达到约束最优，联合优化不必要"。反过来，若实现把风险写进效用函数一起打分，
就等于默认定理 1 不成立。

盾只回答"哪些动作可做"，不回答"做哪个"。`shielded_plan()` 在盾内按姿态的目标
挑动作，不重新评估风险，风险已由盾处理完。策略层若再以风险为由排除动作，
就会出现两处风险判断，而第二处只是启发式，没有定理支撑。
"""

from __future__ import annotations

from . import posture as posture_mod
from .t_irr import TIrR, WAIT, ShieldError
from .toc import ToC, ToCError

__all__ = ["Engine", "EngineError"]


class EngineError(RuntimeError):
    pass


class Engine:
    """ACTE 的机械内核：盾 + 姿态 + ToC。生成式部分（红队循环）由 harness 接。"""

    def __init__(self, t_irr):
        if not isinstance(t_irr, TIrR):
            raise EngineError("需要 TIrR 实例（用 TIrR.load(<path>) 构造）")
        self.t_irr = t_irr

    @classmethod
    def from_snapshot(cls, path):
        return cls(TIrR.load(path))

    def shielded_plan(self, posture_name, actions, triggered, remaining=None, toc=None,
                      trigger_map=None, confirmed=()):
        """给定姿态与状态，算出盾内可行的动作集。

        本方法只算计划，`ε` 通过配额起作用（`quota=0` 时连已确认的不可逆动作
        也不接受）。「闸门应在前置确认阶段拦下」这条流水线不变量由
        `assert_gate_ordering()` 单独承担，供关心流水线次序的调用方显式调用。

        两者必须分开：早先版本把"strict 姿态 + 已触发不可逆转移 ⇒ 报错"并入
        本方法，那在 `ε` 尚无操作形态时看似合理，但 `ε` 落到配额（定理 1′(i)）
        之后就错位了，配额 0 本身给出一个良定义的计划（排除该动作），
        报错却让"稳"姿态在 M3 实例上无法规划。把不变量与计算混在一个方法里，
        代价是一条审计流水线的断言顺手把计算也禁掉了。

        :param posture_name: 细 / 狠 / 稳
        :param actions: 候选动作 id 列表
        :param triggered: 本状态已触发的 `T_irr` 条目 id 集合
        :param remaining: 剩余期限（不作为型必需，见 t_irr.py 的时间增广要求）
        :param toc: 可选 ToC；给了就把覆盖度一并算出
        :param trigger_map: `{action_id: [gate_id, …]}`（盾拿掉动作的依据）
        :param confirmed: 已被当事人确认的动作 id（案件事实）
        :returns: dict
        """
        p = posture_mod.get(posture_name)
        shield = self.t_irr.shield(actions, triggered, remaining=remaining,
                                   trigger_map=trigger_map,
                                   quota=p.quota, confirmed=confirmed)
        out = {
            "posture": p.to_dict(),
            "shield": shield,
            "triggered": sorted(triggered),
            "remaining": remaining,
            "quota": p.quota,
        }
        if toc is not None:
            out["toc"] = {"completeness": toc.completeness(), "coverage": toc.coverage()}
        return out

    def assert_gate_ordering(self, posture_name, triggered):
        """流水线不变量：零容忍姿态下，已触发的不可逆转移不该走到规划阶段。

        这是关于流水线次序的断言，不是计算的一部分（见 `shielded_plan` 的说明）。
        由关心次序的调用方显式调用，例如验证"前置确认闸门先于规划被调用"的
        集成测试。

        它抓的是装配错误：插件未挂载、hook 时机写错。没有它，这类错误会表现为
        "零容忍姿态下照样规划出跨层级动作"，看起来像策略层的问题，实际是闸门
        没有接上。

        :raises EngineError: 零容忍姿态 + 非空 triggered。
        """
        p = posture_mod.get(posture_name)
        if p.eps == posture_mod.EPS_STRICT and triggered:
            raise EngineError(
                "姿态 %s（ε=strict）下检测到已触发的不可逆转移：%s。"
                "零容忍姿态不应走到规划阶段——此处应在前置确认阶段拦下，"
                "走到这里说明闸门漏了（插件未挂载 / hook 时机错），"
                "见 formalization.md 定理 1。" 
                % (posture_name, "、".join(sorted(triggered))))
        return True

    def diagnose_nesting(self, actions, triggered, remaining=None, trigger_map=None,
                         confirmed=()):
        """定理 2(c) 的诊断：固定目标、只变 `ε`，检验盾是否嵌套。

        比较 细/狠（同 ε=loose、不同目标）与 稳/狠（同目标、不同 ε）两组，
        把"目标的影响"与"预算的影响"分开看。
        """
        # 同目标（utility）、不同 ε：稳 vs 狠 ⇒ 应嵌套
        loose = self.t_irr.shield(actions, [], remaining=remaining)
        # strict 姿态下 triggered 必须为空（否则 shielded_plan 会报错），
        # 故此处直接比较 strict 与 loose 在同一触发集合为空下的盾：
        # 两者此时相同是正常的，差异只在有触发时才显现。
        # 触发集合为空时三姿态的盾相同，这是盾只依赖 T_irr 与触发集合的结果，
        # 不是缺陷；嵌套检验因此必须在有触发的实例上做，fixed-posture 消融
        # 需要 M_3 族实例也因于此。
        if not triggered:
            return {
                "note": ("触发集合为空时，三个姿态的盾相同——盾只依赖 T_irr 与"
                         "触发集合，差异只在有不可逆风险被触发时才显现。"
                         "嵌套检验须在有触发的实例上做，fixed-posture 消融"
                         "需要 M_3 族实例即因于此。"),
                "postures": {name: {"allowed": loose["allowed"],
                                    "obligations": loose["obligations"]}
                             for name in posture_mod.POSTURES},
            }
        # 有触发时的对比：loose 姿态放行（盾内仍受限制），strict 姿态报错
        loose_plan = None
        try:
            loose_plan = self.shielded_plan("狠", actions, triggered, remaining=remaining)
        except EngineError as exc:
            loose_plan = {"error": str(exc)}
        strict_plan = None
        try:
            strict_plan = self.shielded_plan("稳", actions, triggered, remaining=remaining)
        except EngineError as exc:
            strict_plan = {"error": str(exc)}
        return {
            "triggered": sorted(triggered),
            "狠": loose_plan,
            "稳": strict_plan,
            "reading": ("稳 报错而 狠 给出计划 —— 定理 2 与定理 1′ 在工程上的形态："
                        "零容忍姿态不接受『先跨层级再补』，可接受姿态则在盾内继续规划。"),
        }