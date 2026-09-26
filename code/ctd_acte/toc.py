"""案件理论图（ToC-Graph）：主张 ↔ 事实/规范/证据的支撑边与完备性。

## T2 的机械指标

任务设计文档 §1：T2「主张栈」的判定依据是
ToC 支撑完备性，每条主张是否三条支撑边齐备（事实 / 规范 / 证据各 ≥1）。

这是四类任务里唯一完全机械的一类：不问"主张对不对"（那要实质判断），
只问"这条主张有没有把三条支撑边都接上"。故 T2 的指标（主张栈完整率）
不依赖任何专家标注。

## 覆盖度的口径：数要件事实，不数请求权

这是 `M_1` 族修正后的口径（见 案件底本候选表 §3.1）。

原设计把"细"读作"多平行请求权并列主张、比谁主张得多"，与《民法典》186 条
（请求权竞合只能择一，同时主张即被驳回）直接冲突。修正后：

| | 原设计（错） | 本模块（对） |
|---|---|---|
| 覆盖度的分母 | 全部可支撑的主张节点 | 该请求权的全部要件事实 |
| "细"的含义 | 请求权数量最大化 | 同一请求权下穷尽要件事实与证据 |

故本模块的 `coverage()` 统计的是要件事实的获支撑比例，
而主张节点由 `claims` 列出（它们决定栈的结构，不直接决定覆盖度）。
"""

from __future__ import annotations

# 支撑边的三种类型。三边齐备即"完备"。
EDGE_FACT = "fact"        # 事实边：该主张对应的要件事实在案
EDGE_LAW = "law"          # 规范边：该主张指向的法条/规范依据
EDGE_EVIDENCE = "evidence"  # 证据边：支撑该要件事实的材料

ALL_EDGE_KINDS = (EDGE_FACT, EDGE_LAW, EDGE_EVIDENCE)

# 边名 → 是否"必须有"。三者都必须有（这就是完备性的定义）。
REQUIRED_EDGES = ALL_EDGE_KINDS


class ToCError(RuntimeError):
    pass


class Claim:
    """一条主张及其支撑边。"""

    __slots__ = ("claim_id", "text", "edges", "elements")

    def __init__(self, claim_id, text, edges=None, elements=None):
        """
        :param edges: `{edge_kind: [支撑项…]}`；缺的种类视为无该边
        :param elements: 该主张项下的要件事实标识列表（覆盖度按它算）
        """
        self.claim_id = claim_id
        self.text = text
        self.edges = {}
        for kind, items in (edges or {}).items():
            if kind not in ALL_EDGE_KINDS:
                raise ToCError("未知的支撑边类型：%r（可用 %s）"
                               % (kind, "、".join(ALL_EDGE_KINDS)))
            self.edges[kind] = list(items or [])
        self.elements = list(elements or [])

    def missing_edges(self):
        """缺哪些必需的支撑边（空列表 = 完备）。"""
        return [k for k in REQUIRED_EDGES if not self.edges.get(k)]

    def is_complete(self):
        return not self.missing_edges()

    @property
    def supported_elements(self):
        """该主张下已获支撑的要件事实数。

        口径：只有"三边齐备"的主张，其要件事实才计入覆盖分子，
        因为缺证据边的要件事实上撑不住（这正是"细"要防的）。
        """
        return len(self.elements) if self.is_complete() else 0

    def to_dict(self):
        return {"claim_id": self.claim_id, "text": self.text,
                "edges": self.edges, "elements": self.elements,
                "missing_edges": self.missing_edges(),
                "is_complete": self.is_complete()}


class ToC:
    """案件理论图：一组主张 + 其支撑边。"""

    def __init__(self, claims, case_id=None):
        self.case_id = case_id
        self.claims = list(claims)
        seen = set()
        for c in self.claims:
            if c.claim_id in seen:
                raise ToCError("主张 id 重复：%s" % c.claim_id)
            seen.add(c.claim_id)

    def completeness(self):
        """主张栈完整率：三边齐备的主张 / 全部主张。"""
        n = len(self.claims)
        if n == 0:
            raise ToCError("ToC 里没有任何主张——完整率的分母为 0。"
                           "空 ToC 要么是输入问题，要么是抽取失败，不该静默返回 0。")
        ok = sum(1 for c in self.claims if c.is_complete())
        return {
            "n_claims": n,
            "n_complete": ok,
            "completeness": round(ok / n, 4),
            "per_claim": [c.to_dict() for c in self.claims],
            "incomplete": [{"claim_id": c.claim_id, "missing_edges": c.missing_edges()}
                           for c in self.claims if not c.is_complete()],
        }

    def coverage(self):
        """要件覆盖度：已获支撑的要件事实 / 全部要件事实（`M_1` 的主指标）。"""
        total = sum(len(c.elements) for c in self.claims)
        if total == 0:
            raise ToCError(
                "没有任何要件事实登记——覆盖度的分母为 0。"
                "注意口径（README §4.1）：覆盖度数的是**要件事实**，不是请求权数量；"
                "若本 ToC 只登记了主张而未登记要件事实，覆盖度无从计算。")
        covered = sum(c.supported_elements for c in self.claims)
        return {
            "n_elements": total,
            "n_supported": covered,
            "coverage": round(covered / total, 4),
            "by_claim": [{"claim_id": c.claim_id,
                          "elements": len(c.elements),
                          "supported": c.supported_elements}
                         for c in self.claims],
        }