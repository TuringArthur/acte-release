#!/usr/bin/env python3
"""`T_irr` 与姿态闸门的回归测试。

首先运行它：它红了，后面所有与"不可逆护栏"有关的数字都不可信。

覆盖两类不变量：

1. 登记表不变量，`T_irr` 的规模与结构（3 条：动作型 1 / 不作为型 2）、
   三种结构各自的组成、每条带判据、时间增广要求被登记；
2. 校验器有牙，§1.1.2 声称"漏分类会报错退出"，这里把该断言变成可执行检查：
   漏分类 / `kind` 非法 / 多余条目 / 缺判据 / 源条数变动 / 重复分类，
   六种情形必须全部退出码非零。

第 2 类是本文件的重点。一个"从不报错的校验器"等于没有校验器，
而"插件看着装上了其实层是哑的"这类静默失效在实际系统里出现过多次，所以此处不查"能不能跑通"，
查"该拦的时候拦没拦住"。

不需要规则源文件：测试用内联的最小闸门源驱动校验器，故可在 `code/` 独立导出后运行。
（这正是 `code/` 作为独立导出单元的纪律：不依赖仓库其他部分。）

    python3 code/tests/test_t_irr.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
PAPER_ROOT = HERE.parents[2]          # 本篇目录
CODE_ROOT = PAPER_ROOT / "code"
REGISTRY = PAPER_ROOT / "method" / "t-irr.yaml"
SNAPSHOT = CODE_ROOT / "data" / "t-irr.json"

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 PyYAML：python3 -m pip install pyyaml\n")
    raise SystemExit(2)


def _load_tool():
    """按路径加载 derive_t_irr 模块（不依赖 sys.path 设置）。"""
    spec = importlib.util.spec_from_file_location(
        "derive_t_irr", CODE_ROOT / "tools" / "derive_t_irr.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 内联最小闸门源：与真实源同构，但不依赖仓库其他文件 ────────────────
MINIMAL_GATES = {
    "version": "test",
    "gates": [
        {"id": "g.a", "name": "甲", "enforcement": "require_confirmation"},
        {"id": "g.b", "name": "乙", "enforcement": "block"},
        {"id": "g.c", "name": "丙", "enforcement": "warn_and_log"},
    ],
}
MINIMAL_REGISTRY = {
    "version": 1,
    "derived_from": {"source_basename": "gates.yaml", "expected_gate_count": 3},
    "t_irr": [
        {"gate_id": "g.a", "kind": "active", "criterion": "甲跨层级且不可回退",
         "in_scope": True},
    ],
    "not_t_irr": [
        {"gate_id": "g.b", "reason": "乙可补正重提，层级不变"},
    ],
    "out_of_scope": [
        {"gate_id": "g.c", "domain": "文书效力", "note": "丙超出本引擎范围"},
    ],
}


class Case:
    def __init__(self):
        self.n = 0
        self.failed = []

    def check(self, name, cond, detail=""):
        self.n += 1
        if cond:
            print("  ok   %s" % name)
        else:
            print("  FAIL %s%s" % (name, ("  — " + detail) if detail else ""))
            self.failed.append(name)


def _write(tmp, relname, obj):
    p = Path(tmp) / relname
    p.write_text(yaml.safe_dump(obj, allow_unicode=True), encoding="utf-8")
    return p


def test_registry_invariants(c):
    """登记表的不变量（读真实登记表）。"""
    print("\n[1] 登记表不变量（真实 method/t-irr.yaml）")
    reg = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))

    t_irr = reg["t_irr"]
    c.check("T_irr 恰为 3 条", len(t_irr) == 3, "实际 %d 条" % len(t_irr))

    kinds = {e["gate_id"]: e["kind"] for e in t_irr}
    c.check(
        "动作型恰为 1 条（gate.escalation-risk）",
        [k for k, v in kinds.items() if v == "active"] == ["gate.escalation-risk"],
        repr(sorted(k for k, v in kinds.items() if v == "active")),
    )
    passive = sorted(k for k, v in kinds.items() if v == "passive")
    c.check(
        "不作为型恰为 2 条（停止执行 + 时效）",
        passive == ["gate.statute-of-limitations", "gate.suspension-of-execution"],
        repr(passive),
    )

    # 每条都必须带判据，否则"可复核"是空话
    c.check("每条 T_irr 都带非空 criterion",
            all((e.get("criterion") or "").strip() for e in t_irr))
    c.check("每条 T_irr 都标 in_scope=true",
            all(e.get("in_scope") is True for e in t_irr))
    c.check("每条 T_irr 都记 level_change（层级如何变）",
            all((e.get("level_change") or "").strip() for e in t_irr))

    # 三个桶不重叠
    buckets = {b: {e["gate_id"] for e in reg.get(b) or []}
               for b in ("t_irr", "not_t_irr", "out_of_scope")}
    overlaps = [(a, b) for a in buckets for b in buckets if a < b
                and buckets[a] & buckets[b]]
    c.check("三个桶互不重叠", not overlaps, repr(overlaps))
    c.check("18 条闸门全部被分类",
            sum(len(v) for v in buckets.values()) == 18,
            "实际 %d" % sum(len(v) for v in buckets.values()))

    # 层级的语义必须是"可救济度"而非"严重度"（§1.1 的关键抽象）
    sem = (reg.get("derived_from") or {}).get("level_semantics") or ""
    c.check("层级语义登记为「制度可救济度」而非「严重度」",
            "可救济度" in sem, sem[:60])


def test_snapshot_matches_registry(c):
    """快照与登记表一致（防止手工编辑过快照）。"""
    print("\n[2] 快照一致性")
    if not SNAPSHOT.is_file():
        c.check("快照存在", False, "缺 %s（先跑 derive_t_irr.py）" % SNAPSHOT)
        return
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    reg = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    c.check("快照 T_irr 条数与登记表一致",
            len(snap["t_irr"]) == len(reg["t_irr"]))
    c.check("快照记录了 source_sha256",
            bool((snap.get("provenance") or {}).get("source_sha256")))
    c.check("快照记录了 level_semantics",
            bool(snap.get("level_semantics")))
    c.check("被排除的闸门连理由一并入库",
            all((e.get("reason") or "").strip() for e in snap["excluded"]),
            "有条目缺 reason")


def test_validator_has_teeth(c):
    """校验器在五种坏输入下必须失败（§1.1.2 的可执行化）。"""
    print("\n[3] 校验器有牙（六种坏输入必须退出码非零）")
    tool = _load_tool()

    with tempfile.TemporaryDirectory() as tmp:
        src = _write(tmp, "gates.yaml", MINIMAL_GATES)

        def run(registry_obj, name):
            """→ (RegistryError|None, 意外错误的描述|None)。

            成功时两者皆 None；被 RegistryError 拦下时只有前者非 None；
            抛了别的异常时只有后者非 None（那也算校验器没按约定失败）。
            """
            p = _write(tmp, name, registry_obj)
            try:
                tool.build_snapshot(src, p)
                return None, None
            except tool.RegistryError as exc:
                return exc, None
            except Exception as exc:  # 非预期异常
                return None, "抛了非 RegistryError：%r" % exc

        import copy

        # 基线：良构登记表必须通过（否则后面五条"拦截"没有意义）
        exc, err = run(MINIMAL_REGISTRY, "ok.yaml")
        c.check("基线：良构登记表通过", exc is None and err is None, err or "")

        # ① 漏分类
        bad = copy.deepcopy(MINIMAL_REGISTRY)
        bad["t_irr"] = []
        bad["not_t_irr"] = []
        bad["out_of_scope"] = [e for e in bad["out_of_scope"]]
        exc, err = run(bad, "missing.yaml")
        c.check("① 漏分类被拦", exc is not None, err or "")
        c.check("① 错误信息点名漏掉的闸门",
                exc is not None and "g.a" in str(exc), "")

        # ② kind 非法
        bad = copy.deepcopy(MINIMAL_REGISTRY)
        bad["t_irr"][0]["kind"] = "reversible"
        exc, err = run(bad, "badkind.yaml")
        c.check("② kind 非法被拦", exc is not None, err or "")

        # ③ 多余条目（源里没有的闸门）
        bad = copy.deepcopy(MINIMAL_REGISTRY)
        bad["t_irr"].append({"gate_id": "g.nope", "kind": "active",
                             "criterion": "伪造", "in_scope": True})
        exc, err = run(bad, "extra.yaml")
        c.check("③ 多余条目被拦", exc is not None, err or "")

        # ④ 缺判据
        bad = copy.deepcopy(MINIMAL_REGISTRY)
        bad["t_irr"][0].pop("criterion", None)
        exc, err = run(bad, "nocrit.yaml")
        c.check("④ 缺判据被拦", exc is not None, err or "")

        # ⑤ 源条数与声明的 expected_gate_count 不符
        bad = copy.deepcopy(MINIMAL_REGISTRY)
        bad["derived_from"]["expected_gate_count"] = 99
        exc, err = run(bad, "count.yaml")
        c.check("⑤ 源条数变动被拦", exc is not None, err or "")

        # ⑥ 重复分类同一闸门
        bad = copy.deepcopy(MINIMAL_REGISTRY)
        bad["not_t_irr"].append({"gate_id": "g.a", "reason": "重复"})
        exc, err = run(bad, "dupe.yaml")
        c.check("⑥ 同一闸门重复分类被拦", exc is not None, err or "")


def main():
    print("=" * 68)
    print("`T_irr` 登记表回归测试")
    print("=" * 68)
    c = Case()
    test_registry_invariants(c)
    test_snapshot_matches_registry(c)
    test_validator_has_teeth(c)

    print("\n" + "=" * 68)
    if c.failed:
        print("失败 %d / %d 项：" % (len(c.failed), c.n))
        for name in c.failed:
            print("  - %s" % name)
        return 1
    print("全部通过（%d 项断言）" % c.n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())