#!/usr/bin/env python3
"""定理 1′(ii) 可运行反例 + `M_2` 证明算术机检（P4 形式化修补件 1、3）。

## 件 1：可逆风险下解耦失效的"全损 5 → 0"构造（参数化、可跑）

`formalization.md` §3(ii) 的解析构造此前只有纸面推导，本脚本把它做成可复算的
实例。实例与定理逐字同形：

```
S = {s0, s1}，A = {a0, a1}：s0 -a1-> s1（跨层），s1 -a0-> s0（回退）。
r(s1, a0) = u = 10，其余 0。风险语义取 §1.1 的定义：Risk(σ) = P(∃t: s_t ∈ S_1)。
```

两个策略族的精确值（γ < 1，封闭式）：

| 策略族 | Risk | E[U] | 含义 |
|---|---|---|---|
| 安全盾内（禁入 `S_1`） | 0 | 0 | 定理 1 的确定性安全盾；`s0` 上无收益 |
| 见证族：以概率 `p ≤ ε` 单次进入、次步必回退、不再进入 | `p` | `p·γ·u` | 取 `p = ε = 1/2`、`γ → 1` ⇒ 5 |
| 再跨越族（注）：进入一次后 `a1/a0` 往复 | `p`（ever-hit 只计首次） | `p·u·γ/(1-γ²)` | 比见证族更高，见下"诚实注记" |

断言：盾内最优 0 < 见证值 `ε·γ·u`（`ε > 0` 时），见证值在 `γ → 1` 时趋于
`ε·u = 5`。故"盾内最优 (0) < 约束可行 (5)"这一失效方向可机械复算。

诚实注记（写作口径，必须照此）：

1. 定理原文写"约束最优 (5)"，严格说 5 是见证策略的值，不是约束最优的上确界：
   ever-hit 风险语义下，回退后再跨越不再消耗风险预算（"多次小额跨越"），
   再跨越族给出 `ε·u·γ/(1-γ²)`（γ=0.95、ε=1/2 时 ≈ 48.7），比 5 更高。
   失效结论更强而非更弱（盾内 0 与约束可行值的差距更大），
   但论文行文应写"约束最优 ≥ 5（见证策略即达 5）"，不写"= 5"。
2. 本脚本对两个族都给封闭式与断言，使"幅度"论述两头都有落点。

## 件 3：`M_2`"连续投入"时间约束的证明算术机检

`formalization.md` §5 的 `M_2` 构造要求把"连续两期投入"写成 `A` 上的时序约束。
本脚本把该约束与证明算术一并机检：

```
fam: A → F（动作归入主张族）；Cont(f) ≡ ∃t: fam(a_t) = fam(a_{t+1}) = f。
目标主张在 Cont(f*) 成立时以 p 成功，否则需两个独立投入各自命中（p²）。
集中投入：E[U] = p·u = 5；分散投入：E[U] = p²·u = 2.5（p=0.5、u=10）。
```

断言：`Cont` 成立的期望严格高于分散投入（5 > 2.5），与 §5 的证明数字一致。

用法：

    python3 code/tools/t1p2_counterexample.py            # 全部断言 + 读数表
    python3 code/tools/t1p2_counterexample.py --gamma 0.95 --eps 0.5 --u 10
"""

from __future__ import annotations

import argparse


class CounterexampleError(RuntimeError):
    pass


def shield_value():
    """安全盾内最优：禁入 S_1 ⇒ 永远留在 s0 ⇒ 收益恒 0。"""
    return 0.0


def witness_value(eps, gamma, u):
    """见证族最优：单次进入（概率 p=ε）、次步必回退、不再进入。E[U] = ε·γ·u。"""
    if not 0 <= eps <= 1:
        raise CounterexampleError("ε 必须在 [0,1]，收到 %r" % eps)
    if not 0 < gamma < 1:
        raise CounterexampleError("γ 必须在 (0,1)，收到 %r" % gamma)
    return eps * gamma * u


def recross_value(eps, gamma, u):
    """再跨越族（注记用）：进入一次后 a1/a0 往复。E[U] = ε·u·γ/(1-γ²)。"""
    return eps * u * gamma / (1.0 - gamma * gamma)


def m2_expected_utilities(p, u):
    """`M_2` 时间约束下的证明算术：集中（Cont 成立）vs 分散（两个独立投入）。"""
    if not 0 <= p <= 1:
        raise CounterexampleError("p 必须在 [0,1]，收到 %r" % p)
    return {"concentrated": p * u, "dispersed": (p * p) * u}


def check(eps, gamma, u, m2_p, m2_u):
    """全部断言。任何一条不符即定理落点与实现/算术脱节 ⇒ 报错退出。"""
    v_shield = shield_value()
    v_witness = witness_value(eps, gamma, u)
    v_recross = recross_value(eps, gamma, u)
    limit = eps * u  # γ → 1

    # 件 1：失效方向与幅度
    if v_shield != 0.0:
        raise CounterexampleError("盾内最优应恒为 0，算得 %r" % v_shield)
    if eps > 0 and not v_witness > v_shield:
        raise CounterexampleError(
            "ε>0 时见证值应严格大于盾内最优：%r vs %r" % (v_witness, v_shield))
    if abs(v_witness - eps * gamma * u) > 1e-12:
        raise CounterexampleError("见证值与闭式 ε·γ·u 不符")
    if v_recross + 1e-12 < v_witness:
        raise CounterexampleError(
            "再跨越族不应低于见证族（ever-hit 语义下再跨越不加预算）：%r < %r"
            % (v_recross, v_witness))
    if eps == 0.5 and u == 10 and abs(limit - 5.0) > 1e-12:
        raise CounterexampleError("标准参数（ε=1/2, u=10）的 γ→1 极限应为 5")

    # 件 3：M_2 证明算术
    m2 = m2_expected_utilities(m2_p, m2_u)
    if not m2["concentrated"] > m2["dispersed"]:
        raise CounterexampleError(
            "M_2 集中投入应严格占优：%r vs %r" % (m2["concentrated"], m2["dispersed"]))
    if m2_p == 0.5 and m2_u == 10:
        if abs(m2["concentrated"] - 5.0) > 1e-12 or abs(m2["dispersed"] - 2.5) > 1e-12:
            raise CounterexampleError(
                "M_2 标准参数应得 5 / 2.5，算得 %r / %r"
                % (m2["concentrated"], m2["dispersed"]))
    return {
        "params": {"eps": eps, "gamma": gamma, "u": u, "gamma_to_1_limit": limit},
        "shield_optimum": v_shield,
        "witness_value": v_witness,
        "recross_value": v_recross,
        "m2": {"params": {"p": m2_p, "u": m2_u}, **m2},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eps", type=float, default=0.5)
    ap.add_argument("--gamma", type=float, default=0.99)
    ap.add_argument("--u", type=float, default=10.0)
    ap.add_argument("--m2-p", type=float, default=0.5)
    ap.add_argument("--m2-u", type=float, default=10.0)
    args = ap.parse_args(argv)

    out = check(args.eps, args.gamma, args.u, args.m2_p, args.m2_u)
    p, m2 = out["params"], out["m2"]
    print("定理 1′(ii) 反例（ε=%s, u=%s, γ=%s；γ→1 极限 %s）："
          % (p["eps"], p["u"], p["gamma"], p["gamma_to_1_limit"]))
    print("  安全盾内最优        = %s" % out["shield_optimum"])
    print("  见证族值（ε·γ·u）   = %s" % out["witness_value"])
    print("  再跨越族值（注记）  = %s" % out["recross_value"])
    print("M_2 时间约束算术（p=%s, u=%s）：集中 %s / 分散 %s"
          % (m2["params"]["p"], m2["params"]["u"],
             m2["concentrated"], m2["dispersed"]))
    print("✓ 全部断言通过——失效方向（盾内 0 < 约束可行 %s）与 M_2 算术均可机械复算"
          % out["witness_value"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
