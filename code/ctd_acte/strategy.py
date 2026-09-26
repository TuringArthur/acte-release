"""策略层：在盾已筛过的动作集内，按姿态目标选动作。

## 边界（本模块围绕这条约束组织）

定理 1(c)：`max_{Risk=0} E[U] = max_{Shield 内} E[U]`，分两步（先算盾、再在盾内解策略）
就能达到约束最优，联合优化不必要。

故本模块：

- 只接收 `allowed`（盾的输出），不接收 `T_irr`、不接收 `triggered`、不调用 `t_irr`；
- 不重新评估风险。风险已由盾处理完；若这里再"因为风险所以不选它"，
  就会出现两处风险判断不一致，且第二处没有定理支撑（它是启发式）。
  这条由测试钉住（`test_strategy.py` 断言本模块不导入 `t_irr`）。

## 选择规则（启发式，不声称最优性）

- `Obj = coverage`：贪心取边际新覆盖要件事实最多的动作（同名元素被多个动作覆盖时只计一次）；
- `Obj = utility`：贪心取期望效用 / 成本最大的动作。

为什么必须写清"不声称最优性"：本文件的形式化贡献是盾的解耦与姿态的
单调性/必要性（定理 1/1′/2/3），不包含选择器的最优性。覆盖最大化本身是子模的、
贪心有 `(1−1/e)` 界，但那条界对应的是无对手的顺序信息采集问题，
与本题的对抗结构不同，搬到这里会造成两处形式化重复。
故本模块只声明"这是一个确定性、可复现的启发式"，不声明任何近似比。

## 姿态的作用点（fixed-posture 消融的落点）

`Obj` 决定选择规则，故同一实例在不同 `Obj` 下会选出不同动作集，
这正是 `fixed-posture` 消融能产生效应的机制：把 M1（民事/细）族用 `Obj = utility`
去跑，它就会为了效用放弃覆盖，`M_1` 的主指标（要件覆盖度）随之下降。

若某实例上两种 `Obj` 选出同一动作集，本模块如实报告（`degenerate: true`），
不制造差异，否则"消融有效应"会变成靠夹具造出来的假象。
"""

from __future__ import annotations

from . import posture as posture_mod


class StrategyError(RuntimeError):
    pass


def _as_float(value, where):
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        raise StrategyError("%s 不是数值：%r" % (where, value))


class Action:
    """一个候选动作（来自案件事实，不是金标准）。

    `covers` 是它推进的要件事实 id 列表（覆盖度的计数对象）；
    `expected_utility` 是它的期望回收。两者都由案件事实声明，
    判定层不比它们，它们只参与策略层的选择。
    """

    __slots__ = ("action_id", "text", "covers", "expected_utility", "cost",
                 "irreversible", "triggers")

    def __init__(self, action_id, text, covers=None, expected_utility=0.0, cost=1.0,
                 irreversible=False, triggers=None):
        self.action_id = action_id
        self.text = text
        self.covers = list(covers or [])
        self.expected_utility = _as_float(expected_utility, "action %s 的 expected_utility"
                                          % action_id)
        self.cost = _as_float(cost, "action %s 的 cost" % action_id)
        if self.cost <= 0:
            raise StrategyError("action %s 的 cost 必须为正（现 %r）——"
                                "零/负成本会让'每单位成本效用'无定义"
                                % (action_id, cost))
        self.irreversible = bool(irreversible)
        # 该动作在案件事实层面会触发哪些 T_irr 条目（供盾读取；策略层不读它）。
        self.triggers = list(triggers or [])

    def __repr__(self):
        return "Action(%s, covers=%d, util=%s)" % (
            self.action_id, len(self.covers), self.expected_utility)

    def to_dict(self):
        return {"action_id": self.action_id, "text": self.text, "covers": self.covers,
                "expected_utility": self.expected_utility, "cost": self.cost,
                "irreversible": self.irreversible, "triggers": self.triggers}


def _score(action, objective, covered_so_far):
    """按目标给单个动作打分。`covered_so_far` 用于覆盖目标的边际计算。"""
    if objective == posture_mod.OBJ_COVERAGE:
        new_elements = [e for e in action.covers if e not in covered_so_far]
        return float(len(new_elements))
    if objective == posture_mod.OBJ_UTILITY:
        return action.expected_utility / action.cost
    raise StrategyError("未知目标：%r（可用 %s / %s）"
                        % (objective, posture_mod.OBJ_COVERAGE, posture_mod.OBJ_UTILITY))


def plan(posture_name, actions, allowed, max_actions=None):
    """在 `allowed` 内按姿态选动作。

    :param posture_name: 细 / 狠 / 稳
    :param actions: 全部候选 `Action`（含被盾拿掉的）
    :param allowed: 盾的输出，允许的动作 id 集合。本模块只在这个集合内选。
    :param max_actions: 至多选几个（诉讼里"同时只能推进有限事项"的约束）；
        缺省则全选（受 `allowed` 限制）。
    :returns: dict，含 `selected` / `excluded_by_shield` / `objective` / `degenerate` 等。
    """
    p = posture_mod.get(posture_name)
    allowed_set = set(allowed)
    by_id = {a.action_id: a for a in actions}
    dupes = sorted({a.action_id for a in actions if [x.action_id for x in actions].count(a.action_id) > 1})
    if dupes:
        raise StrategyError("候选动作 id 重复：%s" % "、".join(dupes))

    # 盾拿掉的动作：只如实记录，不在此处重新评估风险
    excluded = [{"action_id": a.action_id, "text": a.text,
                 "reason": "盾已排除（本层不重新评估风险）"}
                for a in actions if a.action_id not in allowed_set]

    pool = [by_id[i] for i in sorted(allowed_set) if i in by_id]
    unknown = sorted(allowed_set - set(by_id))
    if unknown:
        raise StrategyError(
            "盾允许了候选集里没有的动作：%s —— 说明传入的 actions 与盾的输入不一致，"
            "此时'在盾内选'无意义" % "、".join(unknown))

    limit = len(pool) if max_actions is None else int(max_actions)
    if limit < 0:
        raise StrategyError("max_actions 不能为负（现 %r）" % max_actions)

    selected, covered = [], set()
    remaining = list(pool)
    while remaining and len(selected) < limit:
        # 贪心：每轮取当前最优。平手时按 action_id 升序，保证确定性。
        scored = [(_score(a, p.obj, covered), a) for a in remaining]
        best_score = max(s for s, _ in scored)
        if best_score <= 0 and p.obj == posture_mod.OBJ_COVERAGE:
            break   # 覆盖目标下，边际为零的动作不再选（避免无意义地填满额度）
        best = min((a for s, a in scored if s == best_score),
                   key=lambda a: a.action_id)
        selected.append(best)
        covered.update(best.covers)
        remaining.remove(best)

    # 退化检测：同一实例上两种目标是否选出同一集合。如实报告，不制造差异。
    others = {}
    for other in posture_mod.POSTURES:
        if posture_mod.POSTURES[other].obj == p.obj:
            continue
        o = plan_raw(other, pool, limit)
        others[other] = [a.action_id for a in o]
    degenerate = all(sorted(v) == sorted(a.action_id for a in selected)
                     for v in others.values()) if others else True

    return {
        "posture": p.to_dict(),
        "objective": p.obj,
        "selected": [a.to_dict() for a in selected],
        "selected_ids": [a.action_id for a in selected],
        "excluded_by_shield": excluded,
        "n_candidates": len(actions),
        "n_allowed": len(pool),
        "max_actions": max_actions,
        "covered_elements": sorted(covered),
        "expected_utility": round(sum(a.expected_utility for a in selected), 4),
        "degenerate": degenerate,
        "degenerate_note": ("两种 Obj 在本实例上选出同一动作集——若这是消融臂的读数，"
                            "说明该实例对该消融不敏感，**不得**当作'消融无效应'，"
                            "应换实例或如实报告" if degenerate else None),
        "no_optimality_claim": ("本选择规则是确定性启发式，**不声称最优性**——"
                               "覆盖的子模/近似界不在本仓库的形式化范围内，不在此重复"),
    }


def plan_raw(posture_name, pool, limit):
    """纯选择（不生成报告），供退化检测与测试复用。"""
    p = posture_mod.get(posture_name)
    selected, covered, remaining = [], set(), list(pool)
    while remaining and len(selected) < limit:
        scored = [(_score(a, p.obj, covered), a) for a in remaining]
        if not scored:
            break
        best_score = max(s for s, _ in scored)
        if best_score <= 0 and p.obj == posture_mod.OBJ_COVERAGE:
            break
        best = min((a for s, a in scored if s == best_score), key=lambda a: a.action_id)
        selected.append(best)
        covered.update(best.covers)
        remaining.remove(best)
    return selected


def actions_from(raw_list):
    """由任务集/底本里的 `actions` 声明构造 `Action` 列表。"""
    if not raw_list:
        raise StrategyError("没有候选动作——策略层的分母为 0，无法规划。"
                            "这是实例缺 `actions` 声明，不是'无需动作'。")
    out = []
    for i, raw in enumerate(raw_list):
        if not isinstance(raw, dict) or not raw.get("action_id"):
            raise StrategyError("第 %d 个动作缺 action_id" % (i + 1))
        out.append(Action(
            action_id=raw["action_id"],
            text=raw.get("text") or raw["action_id"],
            covers=raw.get("covers"),
            expected_utility=raw.get("expected_utility", 0.0),
            cost=raw.get("cost", 1.0),
            irreversible=raw.get("irreversible", False),
            triggers=raw.get("triggers"),
        ))
    return out


def triggered_from(actions):
    """由案件事实里的动作推出本状态触发了哪些 `T_irr` 条目。

    为什么由事实推：若让实例直接声明 `triggered`，盾就变成"把金标准抄一遍"，
    检出率恒为 100% 且毫无意义。这里读的是动作自带的 `triggers`（属案件事实，
    像日期一样），盾据此判定；真实系统应由模型从自然语言里判断，
    那是夹具简化，须在文档里写明（见 `code/README.md` 的"夹具简化"一节）。
    """
    out = []
    for a in actions:
        for g in a.triggers:
            if g not in out:
                out.append(g)
    return out