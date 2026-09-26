"""`T_irr` 与安全盾：定理 1 的操作实现。

## 本模块的作用

形式化规范（`method/formalization.md`）的定理 1 说：硬约束（`ε = 0`）下，盾

    Shield(s) = { a : (s,a) ∉ T_irr 且 ∃ 自 s 起的无穷安全续行 }

只依赖 `⟨S, A, P, T_irr⟩`，不依赖效用 `r` 或策略 `σ`，故可独立于策略求解。
本模块把 `T_irr` 从登记表快照读进来，并把它操作成"允许动作集"，供引擎在动作前调用。

## 两种结构对盾的要求

`formalization.md` §1.1.1 把 `T_irr` 分成两类，本模块的核心职责就是把二者区分对待：

| 类型 | 定义 | 盾的行为 |
|---|---|---|
| 动作型（`active`） | `(s,a) ∈ T_irr`，`a ≠ ⊥` | 限制：拿掉 `a`，其余照旧 |
| 不作为型（`passive`） | `(s,⊥) ∈ T_irr` | 强制：拿掉 `⊥` 之后必须还有别的动作可做 |

这是本模块最容易被做错的地方：若把闸门一律实现为"拦截器"（只做减法），
不作为型风险会全数漏防，因为"等待"这个动作在拦截器眼里从来不是"被拦下的动作"。
实测依据：对 `rules/gates.yaml` 全 18 条闸门逐条判定，
属本引擎范围且构成不可逆转移的 3 条里，2 条是不作为型
（`gate.suspension-of-execution`、`gate.statute-of-limitations`）。

故本模块的 `shield()` 对不作为型返回的不只是"排除 `⊥`"，而是把 `⊥` 移入
`obligations`（必须另作他动），由调用方保证动作集非空，若拿掉 `⊥` 后动作集为空，
说明状态刻画不全（缺可行动作），本模块报错而非静默放过。

## 时间增广要求（formalization.md §1.1.1）

不作为型风险要求状态携带剩余期限 `r`：同一状态在不同时刻的 `⊥` 转移不同。
故 `shield()` 接受可选的 `remaining` 参数；当某条不作为型 `T_irr` 声明了
`deadline_field` 而调用方没给剩余期限时，报错，静默按"还没到期限"处理
会让闸门时灵时不灵，正是"看着装上了其实哑了"那类失效。
"""

from __future__ import annotations

import json
from pathlib import Path

# 不作为动作。显式列入动作集是 formalization.md §1.1.1 的建模要求：
# 只有把 ⊥ 当作一个动作，"(s,⊥) ∈ T_irr" 才是良定义的状态函数。
WAIT = "⊥"


class ShieldError(RuntimeError):
    """盾无法计算（状态刻画不全、缺期限分量、登记表不可用等）。"""


class IrreversibleAction:
    """一条不可逆转移的登记项（从 T_irr 快照读出）。"""

    __slots__ = ("gate_id", "kind", "action", "level_change", "criterion",
                 "source_page", "rule_ref")

    def __init__(self, raw):
        self.gate_id = raw["gate_id"]
        self.kind = raw["kind"]                 # active / passive
        self.action = raw.get("action")
        self.level_change = raw.get("level_change")
        self.criterion = raw.get("criterion")
        self.source_page = raw.get("source_page")
        self.rule_ref = raw.get("rule_ref")

    def __repr__(self):
        return "IrreversibleAction(%s, %s)" % (self.gate_id, self.kind)


class TIrR:
    """不可逆转移登记表（读自 `code/data/t-irr.json` 快照）。"""

    def __init__(self, snapshot):
        self.level_semantics = snapshot.get("level_semantics")
        self.provenance = snapshot.get("provenance") or {}
        self.items = [IrreversibleAction(e) for e in snapshot.get("t_irr") or []]
        if not self.items:
            raise ShieldError(
                "T_irr 快照里没有任何条目——盾会是恒等的（不拦任何动作）。"
                "若确实无不可逆风险，应在 method/t-irr.yaml 里显式说明，"
                "而不是让本模块静默产出一个空盾。")

    @classmethod
    def load(cls, path):
        p = Path(path)
        if not p.is_file():
            raise ShieldError(
                "T_irr 快照不存在：%s\n"
                "  先跑：python3 code/tools/derive_t_irr.py --source <path>/rules/gates.yaml"
                % p)
        return cls(json.loads(p.read_text(encoding="utf-8")))

    def by_kind(self, kind):
        return [i for i in self.items if i.kind == kind]

    @property
    def n_active(self):
        return len(self.by_kind("active"))

    @property
    def n_passive(self):
        return len(self.by_kind("passive"))

    def shield(self, actions, triggered, remaining=None, trigger_map=None,
               quota=None, confirmed=()):
        """算出允许动作集（定理 1 的 `Shield(s)`）。

        ## 动作如何被拿掉：按动作自己声明的触发，不按魔法命名

        早期版本让盾去找名为 `action:<gate_id>` 的动作、以及名为 `⊥` 的等待动作，
        那是错的，而且错得很隐蔽：动作模型里 id 是 `adm-a7` 这样的事实级命名，
        于是两个分支都拿不掉任何东西，而盾仍然"成功返回"（消融臂上表现为
        "去掉盾之后事故率不变"，看起来像闸门无效，其实是闸门没接上）。

        现在统一为：谁声明触发了某条 `T_irr`，谁就被拿掉（`trigger_map` 给出
        这个声明）。两型的差别只在于"拿掉的含义"：
        动作型拿掉 = 限制（该动作不可做）；不作为型拿掉 = 强制（等待不可做，
        必须另作他动）。

        :param actions: 当前状态下可考虑的动作 id 列表
        :param triggered: 本状态已触发的 `T_irr` 条目 id 集合
        :param remaining: 剩余期限（不作为型必需，见 §1.1.1 的时间增广要求）
        :param trigger_map: `{action_id: [gate_id, …]}` ，每个动作在事实层面会触发
            哪些条目。触发了条目却不给 trigger_map 时报错：
            拿不准该拿掉哪个动作时静默不拿，就是漏防。
        :param quota: 不可逆风险的配额（`ε` 的操作形态，定理 1′(i)）。
            `None` = 不限；`0` = 零容忍（稳）。取自姿态的 `ε`，但它不是策略，
            它是状态的增广分量（配额自动机的状态），故不破坏定理 1(a) 的"盾不看策略"。
        :param confirmed: 已被当事人确认的动作 id 集合（案件事实）。
        :returns: `{"allowed", "removed", "obligations", "forced", "notes", "quota"}`
        """
        actions = list(actions)
        triggered_ids = set(triggered)
        confirmed = set(confirmed or ())
        known = {i.gate_id for i in self.items}
        unknown = triggered_ids - known
        if unknown:
            raise ShieldError(
                "触发了未登记的 `T_irr` 条目：%s（登记表里只有 %s）——"
                "未登记的条目不在盾的管辖内，静默忽略等于漏防"
                % ("、".join(sorted(unknown)), "、".join(sorted(known))))

        removed, obligations, notes = [], [], []
        spent = 0
        for item in self.items:
            if item.gate_id not in triggered_ids:
                continue
            if trigger_map is None:
                raise ShieldError(
                    "条目 %s 已触发，但未给 `trigger_map`——无法判断该拿掉哪个动作。"
                    "此时静默不拿等于漏防（本模块早期版本正是这样：它去找名为 "
                    "`action:<gate_id>`/`⊥` 的动作，而动作模型用的是事实级 id，"
                    "于是两个分支都拿不掉任何东西、盾却'成功'返回）。"
                    % item.gate_id)
            targets = [aid for aid, gates in trigger_map.items()
                       if item.gate_id in (gates or ())]
            targets = [t for t in targets if t in actions]

            if item.kind == "active":
                # 动作型：限制。两级判定，
                #   ① 未确认的：一律拿掉（须先取得当事人确认，这是闸门的前置语义）；
                #   ② 已确认但配额为 0 的：也拿掉（零容忍姿态不接受任何不可逆风险）。
                # ② 就是 `ε` 的操作效果：`quota` 来自姿态，但它是状态的增广分量
                # （定理 1′(i) 的配额自动机），故盾对同一 (状态, 配额) 仍是纯函数。
                if not targets:
                    notes.append("动作型 %s：本状态未提出会触发它的动作，无需拦截"
                                 % item.gate_id)
                    continue
                unconfirmed = [t for t in targets if t not in confirmed]
                blocked_by_quota = []
                if quota is not None:
                    for t in [x for x in targets if x in confirmed]:
                        if spent >= quota:
                            blocked_by_quota.append(t)
                        else:
                            spent += 1
                drop = unconfirmed + blocked_by_quota
                if drop:
                    for t in drop:
                        actions.remove(t)
                        removed.append(t)
                if unconfirmed:
                    notes.append("动作型 %s：拿掉未确认的动作 %s（跨层级：%s；"
                                 "须前置提示升级风险并取得书面确认）"
                                 % (item.gate_id, "、".join(unconfirmed), item.level_change))
                if blocked_by_quota:
                    notes.append("动作型 %s：拿掉已确认但**超出配额**的动作 %s"
                                 "（quota=%s —— 零容忍姿态不接受任何不可逆风险；"
                                 "这是 ε 的操作效果，定理 1′(i)）"
                                 % (item.gate_id, "、".join(blocked_by_quota), quota))
                if not drop:
                    notes.append("动作型 %s：动作 %s 已确认且在配额内，放行（消耗配额）"
                                 % (item.gate_id, "、".join(targets)))
            elif item.kind == "passive":
                # 不作为型：强制，拿掉"等待"，即必须另作他动
                if remaining is None:
                    raise ShieldError(
                        "不作为型 %s 已触发，但未给剩余期限（remaining=None）。"
                        "不作为型风险要求状态按 §1.1.1 做**时间增广**："
                        "同一状态在不同时刻的 ⊥ 转移不同，缺期限分量会让盾时灵时不灵。"
                        % item.gate_id)
                if not targets:
                    raise ShieldError(
                        "不作为型 %s 已触发，但没有任何动作声明触发它——"
                        "说明状态刻画不全：候选集里应当有那个**等待/不作为**动作。"
                        "缺了它，'必须作为'这条约束无处施加（§1.1.1）。"
                        % item.gate_id)
                for t in targets:
                    actions.remove(t)
                    removed.append(t)
                obligations.append(item.gate_id)
                notes.append("不作为型 %s：拿掉等待动作 %s，剩余期限 %s —— "
                             "须在期限内作为（%s）"
                             % (item.gate_id, "、".join(targets), remaining,
                                item.level_change))
            else:
                raise ShieldError("T_irr 条目 %s 的 kind=%r 非法"
                                  % (item.gate_id, item.kind))

        if obligations and not actions:
            raise ShieldError(
                "不作为型条目 %s 拿掉等待后动作集为空——说明状态里没有可行动作，"
                "刻画不全。须补上该状态下当事人能采取的动作（如『申请停止执行』"
                "『在期限内起诉』），否则盾无法履行『强制』语义。"
                % "、".join(obligations))

        return {
            "allowed": actions,
            "removed": removed,
            "obligations": obligations,
            "forced": bool(obligations),
            "notes": notes,
            "quota": quota,
            "quota_spent": spent,
        }

    def describe(self):
        """人类可读摘要（写进结果文件，便于审阅"盾由什么导出"）。"""
        return {
            "level_semantics": self.level_semantics,
            "n_items": len(self.items),
            "active": [{"gate_id": i.gate_id, "level_change": i.level_change,
                        "rule_ref": i.rule_ref} for i in self.by_kind("active")],
            "passive": [{"gate_id": i.gate_id, "level_change": i.level_change,
                         "rule_ref": i.rule_ref} for i in self.by_kind("passive")],
            "provenance": {"source_sha256": self.provenance.get("source_sha256"),
                           "n_gates_source": self.provenance.get("n_gates_source")},
        }