"""姿态向量 `π = (Obj, ε)`：定理 2 与定理 3 的操作实现。

三姿态的取值（见 `method/formalization.md` §1.2）：

| 姿态 | 书中来源 | `Obj` | `ε` |
|---|---|---|---|
| 细（民事） | "慢工出细活"，重覆盖 | 覆盖最大化 | 宽松 |
| 狠（商事） | "狭路相逢勇者胜"，重效用 | 效用最大化 | 宽松 |
| 稳（行政） | "不要草率开打"，防升级 | 效用最大化 | 严格 |

细与狠在 `Obj` 上不同、在 `ε` 上相同；稳与狠在 `Obj` 上相同、在 `ε` 上不同。
三姿态因此不是三个正交参数的取值，而是 `Obj × ε` 上的两个切片。定理 3 的
无免费午餐性质只发生在目标切片上，不在预算切片上。

定理 2(c)：固定 `Obj`、取 `ε_稳 < ε_狠`，则 `Shield_稳 ⊆ Shield_狠`。
`compare_shields()` 直接检验这一条：两个姿态的盾若出现交叉（各有一个动作只有
自己有），说明 `ε` 没有单独起作用，或 `T_irr` 识别有误。

定理 3 证明不存在恒不劣的姿态（构造了 `M_1`/`M_2`/`M_3` 三个互不占优的实例族），
故参数化是必需的。本模块只做"参数 → 目标与预算"的映射；给定参数下选哪个动作
由策略层负责（`engine.py`）。
"""

from __future__ import annotations

# ── 目标类型 ──────────────────────────────────────────────────
OBJ_COVERAGE = "coverage"   # 覆盖最大化（要件事实与证据的覆盖，不是请求权数量）
OBJ_UTILITY = "utility"     # 效用最大化（期望回收）

# ── 风险预算（枚举而非连续值）─────────────────────────────────
#
# 刻意用离散档位般的枚举而不是浮点 ε：盾的形态由 `T_irr` 条目的触发集合决定，
# 而"允许触发哪些不可逆条目"本身就是离散选择。用浮点 ε 再折算成触发集合，
# 中间那一步折算会引入一个没有依据的阈值，不如直接声明。
# 若将来要连续化，应走 formalization.md 定理 1′(i) 的配额自动机
# （把 ε 折成可消耗配额 q），而不是在这里加一个魔法系数。
EPS_STRICT = "strict"       # 零容忍：任何不可逆转移均不接受（稳）
EPS_LOOSE = "loose"         # 接受"可接受度以内"的跨层级动作（细 / 狠）

_EPS_ORDER = {EPS_STRICT: 0, EPS_LOOSE: 1}

# ── ε → 配额（定理 1′(i) 的配额自动机）──────────────────────────
#
# `稳` 与 `狠` 的 `Obj` 相同（都是效用最大化），差别只在 `ε`；而盾按定理 1(a)
# 不依赖姿态。若 `ε` 不产生任何操作效果，稳与狠在实现里就是同一个姿态，
# "三姿态参数化"落空，`fixed-posture` 消融也测不出差异。
#
# 定理 1′(i) 给的出路：`ε > 0` 时的解耦要求把风险预算做成可消耗的配额，
# 盾作用在增广状态 `(s, 配额)` 上。于是：
#   * `ε = strict` → 配额 0：任何不可逆动作都不接受（含已确认的）；
#   * `ε = loose`  → 配额 1：接受一个已确认的不可逆动作（未确认的仍一律拦）。
#
# 配额是状态的增广分量（配额自动机的状态），不是策略，故不违反"盾不看策略"
# （定理 1(a)）。本函数是 ε 与配额之间唯一的映射点。
#
# `loose` 的配额取 1 是当前实现的选择，不是定理的结论：定理只说配额型可解耦，
# 没说什么配额最好。若日后要连续化 ε，应扩展本函数，而不是在判据里塞系数。
_QUOTA = {EPS_STRICT: 0, EPS_LOOSE: 1}


class PostureError(RuntimeError):
    pass


class Posture:
    """姿态向量 `π = (Obj, ε)`。"""

    __slots__ = ("name", "obj", "eps", "yaojue", "source")

    def __init__(self, name, obj, eps, yaojue=None, source=None):
        if obj not in (OBJ_COVERAGE, OBJ_UTILITY):
            raise PostureError("未知目标：%r" % obj)
        if eps not in _EPS_ORDER:
            raise PostureError("未知风险预算：%r" % eps)
        self.name = name
        self.obj = obj
        self.eps = eps
        self.yaojue = yaojue or name
        self.source = source

    def __repr__(self):
        return "Posture(%s, obj=%s, eps=%s)" % (self.name, self.obj, self.eps)

    @property
    def eps_rank(self):
        return _EPS_ORDER[self.eps]

    @property
    def quota(self):
        """该姿态的不可逆风险配额（`ε` 的操作形态，定理 1′(i)）。"""
        return _QUOTA[self.eps]

    def to_dict(self):
        return {"name": self.name, "obj": self.obj, "eps": self.eps,
                "quota": self.quota,
                "yaojue": self.yaojue, "source": self.source,
                "eps_rank": self.eps_rank}


# ── 注册表：三姿态的定稿取值 ──────────────────────────────────
#
# `source` 记书中出处，使"姿态不是拍脑袋划分"可溯源（对应 summaries 里的书页）。
POSTURES = {
    "细": Posture("细", OBJ_COVERAGE, EPS_LOOSE, source="[书页137] 民事诉讼慢工出细活，重覆盖"),
    "狠": Posture("狠", OBJ_UTILITY, EPS_LOOSE, source="[书页90,92,104] 商事诉讼原告者依法要价，重效用"),
    "稳": Posture("稳", OBJ_UTILITY, EPS_STRICT, source="[书页15-16] 行政诉讼不要草率开打，防处罚升级"),
}


def get(name):
    if name not in POSTURES:
        raise PostureError("未登记的姿态：%r（现有 %s）"
                           % (name, "、".join(POSTURES)))
    return POSTURES[name]


def compare_shields(shield_a, shield_b):
    """比较两个姿态下的盾，判定是否嵌套（定理 2 的可证伪预测）。

    :returns: `{"relation": "equal"|"a_subset_b"|"b_subset_a"|"crossing"|"disjoint",
                "only_a": [...], "only_b": [...]}`
    """
    a, b = set(shield_a["allowed"]), set(shield_b["allowed"])
    only_a, only_b = sorted(a - b), sorted(b - a)
    if not only_a and not only_b:
        rel = "equal"
    elif not only_a:
        rel = "a_subset_b"
    elif not only_b:
        rel = "b_subset_a"
    elif a & b:
        rel = "crossing"
    else:
        rel = "disjoint"
    return {"relation": rel, "only_a": only_a, "only_b": only_b}


def assert_nested(strict_name, loose_name, strict_shield, loose_shield):
    """检验 `ε` 收紧时盾应嵌套（定理 2(c)）。不嵌套即实现有误，报错而非警告。

    为何要硬断言：语句上"稳的动作集是狠的子集"很容易被当成一句描述，
    于是实现里 `ε` 没真正单独起作用也不会有人发现。把它做成断言，
    就变成"要么成立、要么构建失败"。
    """
    cmp = compare_shields(strict_shield, loose_shield)
    if cmp["relation"] not in ("equal", "a_subset_b"):
        raise PostureError(
            "定理 2(c) 不成立：%s（ε=%s）的盾与 %s（ε=%s）的盾呈 %s 关系。"
            "预期严格侧是宽松侧的子集。反例动作落在：%s。"
            "可能原因：① ε 没有真正单独起作用；② T_irr 识别有误。"
            % (strict_name, get(strict_name).eps, loose_name, get(loose_name).eps,
               cmp["relation"],
               "、".join(cmp["only_a"]) or "（无）"))
    return cmp