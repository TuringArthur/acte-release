#!/usr/bin/env python3
"""定理 1′(ii) 反例与 `M_2` 算术的回归测试（P4 落点钉值）。

钉住 `code/tools/t1p2_counterexample.py` 的读数：

1. 失效方向：安全盾内最优恒 0，见证族值 `ε·γ·u` 严格更高（ε>0 时），
   标准参数（ε=1/2、u=10）γ→1 极限恰为 5，"全损 5→0"的机械落点；
2. 再跨越注记：ever-hit 语义下再跨越族不低于见证族（论文写"约束最优 ≥ 5"）；
3. `M_2` 算术：集中 5 / 分散 2.5（p=0.5、u=10），与 formalization.md §5 一致；
4. 参数校验：ε/γ/p 越界报错而非默默算出错值。

    python3 code/tests/test_t1p2_counterexample.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
TOOL = HERE.parents[1] / "tools" / "t1p2_counterexample.py"

spec = importlib.util.spec_from_file_location("t1p2_counterexample", str(TOOL))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAILS = []


def ok(cond, msg):
    if cond:
        print("  ✓ %s" % msg)
    else:
        print("  ✗ %s" % msg)
        FAILS.append(msg)


def raises(fn, msg):
    try:
        fn()
    except mod.CounterexampleError:
        print("  ✓ %s" % msg)
        return
    print("  ✗ %s" % msg)
    FAILS.append(msg)


def main():
    print("[A] 失效方向与幅度（定理 1′(ii)）")
    ok(mod.shield_value() == 0.0, "安全盾内最优恒 0")
    ok(abs(mod.witness_value(0.5, 0.99, 10.0) - 4.95) < 1e-12,
       "见证值 ε·γ·u：0.5·0.99·10 = 4.95")
    ok(abs(mod.witness_value(0.5, 1.0 - 1e-9, 10.0) - 5.0) < 1e-6,
       "γ→1 极限 = ε·u = 5（标准参数）")
    ok(mod.witness_value(0.5, 0.99, 10.0) > mod.shield_value(),
       "ε>0 时见证值严格大于盾内最优（失效方向）")
    ok(mod.witness_value(0.0, 0.99, 10.0) == 0.0,
       "ε=0 时见证值退化为 0（与定理 1 的硬约束一致）")

    print("[B] 再跨越族注记（论文口径：约束最优 ≥ 5）")
    r = mod.recross_value(0.5, 0.95, 10.0)
    ok(r > mod.witness_value(0.5, 0.95, 10.0),
       "再跨越族不低于见证族（%.4f > %.4f）" % (r, mod.witness_value(0.5, 0.95, 10.0)))
    ok(abs(r - 48.7179487179) < 1e-6,
       "γ=0.95、ε=1/2 时再跨越族 ≈ 48.718（幅度论述的另一头）")

    print("[C] M_2 时间约束算术（formalization.md §5）")
    m2 = mod.m2_expected_utilities(0.5, 10.0)
    ok(abs(m2["concentrated"] - 5.0) < 1e-12 and abs(m2["dispersed"] - 2.5) < 1e-12,
       "集中 5 / 分散 2.5（p=0.5、u=10）")
    ok(m2["concentrated"] > m2["dispersed"], "Cont(f) 成立时集中投入严格占优")

    print("[D] 参数校验（报错而非默默算出错值）")
    raises(lambda: mod.witness_value(1.5, 0.99, 10.0), "ε>1 ⇒ CounterexampleError")
    raises(lambda: mod.witness_value(0.5, 1.0, 10.0), "γ=1 ⇒ CounterexampleError")
    raises(lambda: mod.m2_expected_utilities(-0.1, 10.0), "p<0 ⇒ CounterexampleError")

    print("[E] 全量 check() 在多组参数下自洽")
    for eps, gamma in ((0.5, 0.99), (0.3, 0.9), (1.0, 0.95)):
        out = mod.check(eps, gamma, 10.0, 0.5, 10.0)
        ok(out["witness_value"] <= out["recross_value"] + 1e-9,
           "check(ε=%s, γ=%s) 通过且见证 ≤ 再跨越" % (eps, gamma))

    if FAILS:
        print("\n✗ %d 条断言失败" % len(FAILS))
        return 1
    print("\n✓ 全部断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
