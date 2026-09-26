#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F1 统计升级工具：把论文里"收益是确定的 / 相当"这类无检验措辞换成可检验读数。

输入：若干 harness 结果 JSON（`experiment/results/*.json`，**只读**，不改写）。
输出：统计报告 JSON + Markdown（默认写到第一个输入同目录的
`stats-upgrade-report.{json,md}`，`--out-json` / `--out-md` 可改）。

五组读数（每条读数都带 provenance：来源文件 / 系统名 / 口径）：

1. **bootstrap 95% CI**（实例层重采样，默认 10000 次，种子固定并写入输出）：
   检出率（严格=tier2 主口径 / 宽松=tier1）、真实升级事故率（计划口径）、
   代理事故率（识别口径）、干净件报出率（FP(any) / FP(重大)）。
2. **McNemar 精确检验 + Holm 多重比较校正**：成对臂、实例按 task_id 对齐，
   报不一致对 (b, c)、双侧精确 p、Holm 校正 p（按指标族 + 全局两档）。
   主对比：本系统 vs B3、本系统 vs no-shield、no-shield vs main 行为侧
   （默认配对：--main 指定的臂 vs 其余同口径臂；--compare 可显式加对）。
3. **TOST 等效检验**：两臂检出率差在界值 ±margin（默认 0.10）内的等效性。
   双单侧精确检验：H0: Δ ≤ -δ 与 H0: Δ ≥ +δ，各取 H0 区域上精确尾概率的
   上确界（三格分布 T = n10 - n01，干扰参数二维网格）；两个单侧 p 均 < α
   才允许说"相当（等效）"，否则只能说"未发现差异"。
4. **功效分析**：配对二项结构（不一致对 b/c）下精确 McNemar 功效——
   当前 n 下的功效与达到 1-β≥0.8 所需 n；`--power-contrast x1/n:x2/n`
   可直接喂聚合对比（如 0/25 vs 25/25、1/8 vs 3/8 这类配对结构，
   分母相等时按可行重叠 a 给出配对功效区间，另报独立两比例读数）。
5. **多种子方差**：`repeats.run_dirs` 多 run 的 run 间 SD、二项参照 SD、
   过度离散比 F 与组内相关 ICC（矩估计；结果 JSON 只落 run 级聚合，
   逐实例 × 逐 run 矩阵未落盘时精确 ICC(1) 不可算，如实标注）。

脚本纪律：只读结果文件；随机种子写死可复现；纯 Python 标准库（不引新依赖）。
口径约定：严格 = tier2（只算有痕迹根据的子句，主口径）、宽松 = tier1
（含定义性子句）、合取 = `settings.conjunction_across_findings` 开
（跨 finding 合取，检出值已含救回实例）。

用法示例：

    python3 code/tools/stats_upgrade.py \\
        experiment/results/acte-model.json \\
        experiment/results/acte-model-noshield-rerun.json

    python3 code/tools/stats_upgrade.py \\
        experiment/results/acte-model.json \\
        experiment/results/baseline-b3.json \\
        experiment/results/acte-model-noshield-rerun.json \\
        --main acte-model --out-json /tmp/r.json --out-md /tmp/r.md

    python3 code/tools/stats_upgrade.py A.json B.json \\
        --power-contrast 0/25:25/25 --power-contrast 1/8:3/8
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.0"

# 随机种子写死：换机器/换时间跑出的 bootstrap 区间逐位相同
DEFAULT_SEED = 20260926
DEFAULT_N_BOOT = 10000
DEFAULT_ALPHA = 0.05
DEFAULT_MARGIN = 0.10           # TOST 等效界值 ±0.10（预设，不看数据后改）
DEFAULT_TOST_GRID = 61          # TOST 干扰参数网格（每维点数）
DEFAULT_POWER_N_MAX = 5000      # 样本量搜索上限
FIXTURE_ARM_NAMES = ("oracle", "null")

CAL_STRICT = "严格（tier2 痕迹口径，主口径）"
CAL_LOOSE = "宽松（tier1 含定义性子句）"
CAL_CONJ = " + 跨 finding 合取"
CAL_REAL_INC = "行为侧（计划口径：未经确认的跨层级动作 / 等待失权）"
CAL_PROXY_INC = "代理口径（是否识别到风险，非行为侧）"
CAL_FP_ANY = "干净件 FP(any)（findings/notes 任一非空）"
CAL_FP_MAJOR = "干净件 FP(重大)（仅 findings）"


# ─────────────────────────── 基础数学（纯标准库） ───────────────────────────

def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _sample_sd(xs):
    if not xs or len(xs) < 2:
        return None
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def _quantile(sorted_xs, q):
    """百分位（线性插值），输入须已升序。"""
    if not sorted_xs:
        return None
    idx = q * (len(sorted_xs) - 1)
    lo = int(math.floor(idx))
    hi = min(lo + 1, len(sorted_xs) - 1)
    frac = idx - lo
    return sorted_xs[lo] * (1 - frac) + sorted_xs[hi] * frac


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _norm_ppf(q):
    """正态分位数（Acklam 近似，够用；只用于 Wald CI / 独立两比例功效）。"""
    if not 0.0 < q < 1.0:
        raise ValueError("q 必须在 (0,1) 内")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1 - 0.02425
    if q < plow:
        u = math.sqrt(-2 * math.log(q))
        return ((((((c[0] * u + c[1]) * u + c[2]) * u + c[3]) * u + c[4]) * u + c[5])
                / ((((d[0] * u + d[1]) * u + d[2]) * u + d[3]) * u + 1))
    if q > phigh:
        u = math.sqrt(-2 * math.log(1 - q))
        return -(((((c[0] * u + c[1]) * u + c[2]) * u + c[3]) * u + c[4]) * u + c[5]) \
            / ((((d[0] * u + d[1]) * u + d[2]) * u + d[3]) * u + 1)
    u = q - 0.5
    t = u * u
    return ((((((a[0] * t + a[1]) * t + a[2]) * t + a[3]) * t + a[4]) * t + a[5]) * u
            / (((((b[0] * t + b[1]) * t + b[2]) * t + b[3]) * t + b[4]) * t + 1))


def _binom_pmf(k, n, p):
    if k < 0 or k > n:
        return 0.0
    if p <= 0.0:
        return 1.0 if k == 0 else 0.0
    if p >= 1.0:
        return 1.0 if k == n else 0.0
    logp = (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
            + k * math.log(p) + (n - k) * math.log(1 - p))
    return math.exp(logp)


def _binom_cdf(k, n, p):
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return sum(_binom_pmf(i, n, p) for i in range(0, k + 1))


def _multinomial_coef(i, j, k):
    n = i + j + k
    return (math.factorial(n) / (math.factorial(i) * math.factorial(j)
                                 * math.factorial(k)))


def _derive_seed(seed, key):
    """由主种子 + 读数键派生子种子：与遍历顺序无关，重排代码不改结果。"""
    digest = hashlib.sha256(("%d|%s" % (seed, key)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


# ─────────────────────────── 1. bootstrap 95% CI ───────────────────────────

def bootstrap_rate_ci(bits, n_boot, seed, key):
    """对 0/1 实例向量做实例层重采样，给比率的百分位 95% CI。

    边界诚实：全 0 / 全 1 的样本 bootstrap 分布退化在点上，CI 会显示为
    [x, x] 并附 `degenerate` 标注——不是 bug，是这类数据的真实不确定性形状。
    """
    bits = [1 if b else 0 for b in bits]
    n = len(bits)
    if n == 0:
        return None
    point = sum(bits) / n
    rng = random.Random(_derive_seed(seed, key))
    reps = []
    for _ in range(n_boot):
        s = 0
        for _ in range(n):
            s += bits[rng.randrange(n)]
        reps.append(s / n)
    reps.sort()
    degenerate = (point == 0.0 or point == 1.0)
    return {
        "n": n,
        "n_positive": sum(bits),
        "point": round(point, 4),
        "ci95_low": round(_quantile(reps, 0.025), 4),
        "ci95_high": round(_quantile(reps, 0.975), 4),
        "n_bootstrap": n_boot,
        "resampling_unit": "实例（instance / clean case）行",
        "degenerate": degenerate,
        "note": ("全 0/全 1 样本：bootstrap 分布退化在点上，CI=[x,x]；"
                 "此时区间宽度低估真实不确定性，勿读作『估计精确』"
                 if degenerate else None),
    }


# ──────────────── 2. McNemar 精确检验 + Holm 多重比较校正 ────────────────

def mcnemar_exact(b, c, alpha=DEFAULT_ALPHA):
    """双侧精确 McNemar（不一致对 ~ Binom(b+c, 0.5)，双倍尾概率）。"""
    d = b + c
    if d == 0:
        return {"p_exact": 1.0, "note": "无不一致对：无法拒绝同分布（也无功效）"}
    p = min(1.0, 2.0 * _binom_cdf(min(b, c), d, 0.5))
    return {"p_exact": round(p, 6), "n_discordant": d}


def holm_adjust(pvals):
    """Holm step-down：返回与输入同序的校正 p。"""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        val = (m - rank) * pvals[i]
        running = max(running, val)
        adj[i] = min(1.0, running)
    return adj


# ─────────────────────────── 3. TOST 等效检验 ───────────────────────────

def _trinomial_outcomes(n):
    """三格 (n10, n01, 其余) 的全部结果：(i, j, k, coef, T=i-j)。"""
    out = []
    for i in range(n + 1):
        for j in range(n + 1 - i):
            k = n - i - j
            out.append((i, j, k, _multinomial_coef(i, j, k), i - j))
    return out


def _adaptive_grid(n, grid):
    """按结果数把二维网格收细：预算 ~4e6 项，保下限 21、上限 grid。"""
    n_out = (n + 1) * (n + 2) // 2
    g = int(math.sqrt(4.0e6 / max(n_out, 1)))
    return max(21, min(grid, g))


def _exact_tail_supremum(n, t_obs, side, margin, grid=DEFAULT_TOST_GRID):
    """H0 区域上精确尾概率的上确界。

    :param side: "ge" 测 H0: Δ ≤ -δ（p 值 = sup P(T ≥ t_obs)）；
        "le" 测 H0: Δ ≥ +δ（p 值 = sup P(T ≤ t_obs)）。
    :param margin: δ > 0。Δ = p10 - p01（A 独有率 − B 独有率）。
    """
    grid = _adaptive_grid(n, grid)
    outcomes = _trinomial_outcomes(n)
    if side == "ge":
        sel = [o for o in outcomes if o[4] >= t_obs]
    else:
        sel = [o for o in outcomes if o[4] <= t_obs]
    # H0 区域参数化：p10 - p01 = ±margin 边界随外层网格铺开
    if side == "ge":
        # Δ ≤ -δ ⇒ p01 ≥ p10 + δ
        outer = [i * ((1.0 - margin) / 2.0) / (grid - 1) for i in range(grid)]
        def inner_lo(p10):
            lo = p10 + margin
            hi = 1.0 - p10
            return lo, hi
    else:
        outer = [i * ((1.0 - margin) / 2.0) / (grid - 1) for i in range(grid)]
        def inner_lo(p01):
            lo = p01 + margin
            hi = 1.0 - p01
            return lo, hi

    best = 0.0
    for a in outer:
        if side == "ge":
            p10 = a
            lo, hi = inner_lo(p10)
            if hi < lo:
                continue
            pts = [lo + (hi - lo) * i / (grid - 1) for i in range(grid)]
            for p01 in pts:
                pc = 1.0 - p10 - p01
                if pc < 0:
                    continue
                best = max(best, _trinomial_tail_sum(sel, p10, p01, pc, n))
        else:
            p01 = a
            lo, hi = inner_lo(p01)
            if hi < lo:
                continue
            pts = [lo + (hi - lo) * i / (grid - 1) for i in range(grid)]
            for p10 in pts:
                pc = 1.0 - p10 - p01
                if pc < 0:
                    continue
                best = max(best, _trinomial_tail_sum(sel, p10, p01, pc, n))
    return best, grid


def _trinomial_tail_sum(sel, p10, p01, pc, n):
    pow10 = [1.0] * (n + 1)
    pow01 = [1.0] * (n + 1)
    powc = [1.0] * (n + 1)
    for i in range(1, n + 1):
        pow10[i] = pow10[i - 1] * p10
        pow01[i] = pow01[i - 1] * p01
        powc[i] = powc[i - 1] * pc
    total = 0.0
    for i, j, k, coef, _t in sel:
        total += coef * pow10[i] * pow01[j] * powc[k]
    return total


def tost_paired_exact(n10, n01, n, margin=DEFAULT_MARGIN, alpha=DEFAULT_ALPHA,
                      grid=DEFAULT_TOST_GRID):
    """配对二元结局的 TOST（双单侧精确检验）。

    Δ = p_A - p_B = (n10 - n01)/n，n10 = 仅 A 检出、n01 = 仅 B 检出。
    两个单侧精确检验分别在 H0 区域上对干扰参数取尾概率上确界；
    只有两侧 p 都 < α 才判"等效（|Δ| < δ）"。附配对 Wald 90% CI 作对照读数。
    """
    if n <= 0:
        return None
    t_obs = n10 - n01
    delta = t_obs / n
    # 配对 Wald 标准误（精确法的对照读数；n>150 时的退化口径）
    pd_hat = (n10 + n01) / n
    var = (pd_hat - delta ** 2) / n
    se = math.sqrt(var) if var > 0 else 0.0
    z = _norm_ppf(1 - alpha)          # 单侧 0.95 ⇒ 90% 双侧
    if n > 150:
        # 精确网格 O(结果数 × 网格²)，n>150 时太慢 ⇒ 退化为配对正态近似 TOST
        p_lower = 1.0 - _norm_cdf((delta + margin) / se) if se > 0 else (
            0.0 if delta > -margin else 1.0)
        p_upper = _norm_cdf((delta - margin) / se) if se > 0 else (
            0.0 if delta < margin else 1.0)
        return {
            "n_pairs": n, "n10_A_only": n10, "n01_B_only": n01,
            "delta_hat": round(delta, 4), "margin": margin, "alpha": alpha,
            "p_lower": round(p_lower, 6), "p_upper": round(p_upper, 6),
            "equivalent": (p_lower < alpha and p_upper < alpha),
            "wald_ci90": [round(delta - z * se, 4), round(delta + z * se, 4)],
            "method": ("n>150：配对正态近似 TOST（双单侧 z 检验）；"
                       "精确网格口径只对 n≤150 计算"),
        }
    p_lower, g_lo = _exact_tail_supremum(n, t_obs, "ge", margin, grid)  # H0: Δ ≤ -δ
    p_upper, _g_hi = _exact_tail_supremum(n, t_obs, "le", margin, grid)  # H0: Δ ≥ +δ
    equivalent = (p_lower < alpha and p_upper < alpha)
    return {
        "n_pairs": n,
        "n10_A_only": n10,
        "n01_B_only": n01,
        "delta_hat": round(delta, 4),
        "margin": margin,
        "alpha": alpha,
        "p_lower": round(p_lower, 6),   # H0: Δ ≤ -margin
        "p_upper": round(p_upper, 6),   # H0: Δ ≥ +margin
        "equivalent": equivalent,
        "wald_ci90": [round(delta - z * se, 4), round(delta + z * se, 4)],
        "method": ("双单侧精确检验：三格分布 T=n10-n01，H0 区域"
                   "(%d×%d 网格)上尾概率上确界；Wald CI 为对照读数"
                   % (g_lo, g_lo)),
    }


# ─────────────────────────── 4. 功效分析 ───────────────────────────

class _RejectSetCache:
    """精确 McNemar 的拒绝域阈值缓存：d 个不一致对时拒绝 x ≤ lo 或 x ≥ hi。"""

    def __init__(self, alpha):
        self.alpha = alpha
        self._lo = {}
        self._hi = {}

    def thresholds(self, d):
        if d in self._lo:
            return self._lo[d], self._hi[d]
        lo, hi = -1, d + 1
        for x in range(0, d + 1):
            p = min(1.0, 2.0 * _binom_cdf(x, d, 0.5))
            if p <= self.alpha:
                lo = x
        for x in range(d, -1, -1):
            p = min(1.0, 2.0 * (1.0 - _binom_cdf(x - 1, d, 0.5)))
            if p <= self.alpha:
                hi = x
        self._lo[d] = lo
        self._hi[d] = hi
        return lo, hi


def _q_reject(d, theta, cache):
    """给定 d 个不一致对、方向概率 theta 时落入拒绝域的概率。"""
    lo, hi = cache.thresholds(d)
    if d == 0:
        return 0.0
    q = 0.0
    if lo >= 0:
        q += _binom_cdf(lo, d, theta)
    if hi <= d:
        q += 1.0 - _binom_cdf(hi - 1, d, theta)
    return min(1.0, q)


def mcnemar_power(p10, p01, n, alpha=DEFAULT_ALPHA, cache=None, qtab=None):
    """精确 McNemar 功效：D~Binom(n, p10+p01)，X|D~Binom(D, p10/(p10+p01))。"""
    pd = p10 + p01
    if pd <= 0:
        return alpha    # 无不一致 ⇒ 功效 = 显著性水平（无效应可检）
    theta = p10 / pd
    cache = cache or _RejectSetCache(alpha)
    power = 0.0
    for d in range(1, n + 1):
        w = _binom_pmf(d, n, pd)
        if w <= 0:
            continue
        if qtab is not None and d in qtab:
            q = qtab[d]
        else:
            q = _q_reject(d, theta, cache)
            if qtab is not None:
                qtab[d] = q
        power += w * q
    return power


def power_analysis_paired(b, c, n, alpha=DEFAULT_ALPHA, target=0.8,
                          n_max=DEFAULT_POWER_N_MAX):
    """配对结构（不一致对 b=n10, c=n01）的功效与所需 n。

    插件估计：p10=b/n、p01=c/n。点估计在边界（如 b=0）时功效读数会偏乐观/
    偏极端，`caveat` 里如实标注。当前 n 功效 + 达到 target 所需最小 n。
    """
    if n <= 0:
        return None
    p10, p01 = b / n, c / n
    degenerate_direction = (b == c)
    cache = _RejectSetCache(alpha)
    qtab = {}
    notes = []
    if degenerate_direction:
        if b == 0 and c == 0:
            notes.append("无不一致对（两臂逐实例完全一致）：无可检效应，"
                         "功效 = α；required_n 记 null")
        else:
            notes.append("两个方向的不一致对相等（b=c）⇒ 插件效应为 0，"
                         "按点估计无法规划样本量；required_n 记 null")
        return {
            "structure": "paired_mcnemar",
            "b_A_only": b, "c_B_only": c, "n_pairs": n,
            "p10_hat": round(p10, 4), "p01_hat": round(p01, 4),
            "power_at_current_n": round(alpha, 4),
            "power_note": "效应点估计为 0 ⇒ 观测功效 = α（无方向可检）",
            "required_n_for_target": None,
            "target_power": target,
            "notes": notes,
        }
    power_now = mcnemar_power(p10, p01, n, alpha, cache, qtab)
    required = None
    for n_try in range(2, n_max + 1):
        pw = mcnemar_power(p10, p01, n_try, alpha, cache, qtab)
        if pw >= target:
            required = n_try
            break
    if b == 0 or c == 0:
        notes.append("不一致对全部同向（b 或 c = 0）：插件估计在边界，"
                     "所需 n 偏乐观，应按最小关注效应复核")
    if (b + c) <= 4:
        notes.append("不一致对 ≤ 4：效应估计极不稳定，读数仅供规划参考")
    return {
        "structure": "paired_mcnemar",
        "b_A_only": b, "c_B_only": c, "n_pairs": n,
        "p10_hat": round(p10, 4), "p01_hat": round(p01, 4),
        "power_at_current_n": round(power_now, 4),
        "required_n_for_target": required,
        "target_power": target,
        "n_max_searched": n_max,
        "notes": notes,
    }


def power_two_proportion(x1, n1, x2, n2, alpha=DEFAULT_ALPHA, target=0.8,
                         n_max=200000):
    """独立两比例（正态近似）功效与所需 n（等比例扩容 n2/n1 保持）。"""
    if n1 <= 0 or n2 <= 0:
        return None
    p1, p2 = x1 / n1, x2 / n2
    z_a = _norm_ppf(1 - alpha / 2)
    notes = []
    # 边界比例（x=0 或 x=n）用 +0.5 连续性校正（Anscombe）估效应与方差，
    # 否则方差为 0、正态近似退化成"任意 n 功效=1"
    def _adj(x, n):
        return (x + 0.5) / (n + 1) if (x == 0 or x == n) else x / n
    q1, q2 = _adj(x1, n1), _adj(x2, n2)
    delta = abs(q1 - q2)
    pbar = (x1 + x2) / (n1 + n2)
    if min(x1, n1 - x1, x2, n2 - x2) == 0:
        notes.append("比例在边界（0 或 n）：效应/方差用 +0.5 连续性校正估计，"
                     "正态近似读数偏乐观，供规划参考")
    if delta == 0:
        return {"structure": "independent_two_proportion",
                "p1_hat": round(p1, 4), "p2_hat": round(p2, 4),
                "delta_hat": 0.0,
                "power_at_current_n": round(alpha, 4),
                "required_n_per_arm_for_target": None,
                "notes": ["效应点估计为 0 ⇒ 无法规划样本量"]}

    def _power(nn1, nn2):
        s0 = math.sqrt(pbar * (1 - pbar) * (1 / nn1 + 1 / nn2))
        s1 = math.sqrt(q1 * (1 - q1) / nn1 + q2 * (1 - q2) / nn2)
        if s1 <= 0:
            return 1.0
        return _norm_cdf((delta - z_a * s0) / s1)

    power_now = _power(n1, n2)
    required = None
    ratio = n2 / n1
    n_try = 2
    while n_try <= n_max:
        if _power(n_try, max(2, int(round(n_try * ratio)))) >= target:
            required = n_try
            break
        n_try += 1
    return {
        "structure": "independent_two_proportion",
        "p1_hat": round(p1, 4), "p2_hat": round(p2, 4),
        "delta_hat": round(p1 - p2, 4),
        "delta_adj_used": round(q1 - q2, 4),
        "power_at_current_n": round(power_now, 4),
        "required_n_per_arm_for_target": required,
        "target_power": target,
        "notes": notes,
        "method": "正态近似两比例检验（双侧 α），n2/n1 等比扩容",
    }


def power_aggregate_contrast(x1, n1, x2, n2, alpha, target, n_max, label):
    """聚合计数对比（如 0/25 vs 25/25、1/8 vs 3/8）的功效包。

    分母相等时按**可行重叠** a ∈ [max(0, x1+x2-n), min(x1, x2)] 枚举配对
    结构（b = x1-a, c = x2-a），给配对功效/所需 n 的区间；另报独立两比例读数。
    """
    out = {
        "contrast": label,
        "counts": {"arm1": "%d/%d" % (x1, n1), "arm2": "%d/%d" % (x2, n2)},
        "independent": power_two_proportion(x1, n1, x2, n2, alpha, target, n_max),
    }
    if n1 == n2:
        lo_a = max(0, x1 + x2 - n1)
        hi_a = min(x1, x2)
        rows = []
        for a in range(lo_a, hi_a + 1):
            b, c = x1 - a, x2 - a
            pw = power_analysis_paired(b, c, n1, alpha, target, n_max)
            rows.append({"overlap_a": a, "b_A_only": b, "c_B_only": c, **pw})
        reqs = [r["required_n_for_target"] for r in rows
                if r["required_n_for_target"] is not None]
        out["paired_by_overlap"] = rows
        out["paired_required_n_range"] = (
            [min(reqs), max(reqs)] if reqs else None)
        out["paired_note"] = (
            "配对结构未给重叠 a 时按可行区间枚举：required_n 区间 = "
            "[%s]" % (("%d..%d" % (min(reqs), max(reqs))) if reqs else "不可估"))
    return out


# ─────────────────────────── 5. 多种子方差 ───────────────────────────

def multi_seed_variance(run_rows, metric_key, n_units, is_rate=True):
    """run 间 SD + 组内相关（ICC 矩估计，beta-binomial 过度离散）。

    Var(rate) = p(1-p)·(1+(n-1)ρ)/n ⇒ ρ̂ = (F-1)/(n-1)，
    F = s²_run / (p̄(1-p̄)/n)。结果 JSON 只存 run 级聚合（harness._compact），
    逐实例 × 逐 run 矩阵未落盘时这是唯一可算的组内相关；k 小 ⇒ 估计很噪。
    """
    vals = [(i + 1, r.get(metric_key), r.get("run_dir"))
            for i, r in enumerate(run_rows)]
    nums = [v for _i, v, _d in vals if isinstance(v, (int, float))]
    if len(nums) < 2:
        return {"n_runs": len(nums), "sd_run": None,
                "note": "run 数 < 2：run 间 SD 不可算"}
    mean = _mean(nums)
    sd = _sample_sd(nums)
    out = {
        "n_runs": len(nums),
        "run_values": [{"run": i, "value": v, "run_dir": d} for i, v, d in vals],
        "mean": round(mean, 6),
        "sd_run": round(sd, 6),
        "min": round(min(nums), 6),
        "max": round(max(nums), 6),
    }
    if is_rate and n_units and 0 < mean < 1:
        var_bin = mean * (1 - mean) / n_units
        var_run = sd ** 2
        F = var_run / var_bin if var_bin > 0 else None
        icc = (F - 1) / (n_units - 1) if (F is not None and n_units > 1) else None
        out.update({
            "sd_binomial_reference": round(var_bin ** 0.5, 6),
            "overdispersion_F": round(F, 4) if F is not None else None,
            "icc_moment": round(icc, 4) if icc is not None else None,
            "icc_method": ("矩估计 ρ̂=(F-1)/(n-1)，F=s²_run/(p̄(1-p̄)/n)；"
                           "k 小（= %d）⇒ 极噪，仅作量级参考" % len(nums)),
            "icc_exact_note": ("精确 ICC(1) 需逐实例 × 逐 run 矩阵，"
                               "harness 只落 run 级聚合 ⇒ 不可算，如实标注"),
        })
    else:
        out["icc_moment"] = None
        if is_rate:
            out["icc_note"] = ("比率在边界（mean=0 或 1）：二项参照方差为 0，"
                               "过度离散 F / 组内相关不可算")
        else:
            out["icc_note"] = "计数类指标（非比率）：组内相关不适用"
    return out


# ─────────────────────── 结果 JSON 装载与臂抽取 ───────────────────────

def _sha256_file(path):
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()


def load_results(path):
    p = Path(path)
    if not p.is_file():
        raise SystemExit("结果文件不存在：%s" % path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit("结果文件不是合法 JSON：%s（%s）" % (path, e))
    for key in ("harness_version", "taskset", "systems"):
        if key not in doc:
            raise SystemExit("结果文件缺顶层键 %r：%s（不是 harness 产物？）"
                             % (key, path))
    return doc


def extract_arms(path, want_system=None):
    """一个结果文件 → 一个或多个臂（systems 每键一臂）。"""
    doc = load_results(path)
    p = Path(path)
    systems = doc.get("systems") or {}
    if not systems:
        raise SystemExit("结果文件没有 systems：%s" % path)
    names = list(systems.keys())
    if want_system is not None:
        if want_system not in systems:
            raise SystemExit("%s 里没有系统 %r（有：%s）"
                             % (path, want_system, "、".join(names)))
        names = [want_system]
    multi = len(systems) > 1
    arms = []
    for name in names:
        s = systems[name]
        settings = s.get("settings") or {}
        conj = bool(settings.get("conjunction_across_findings"))
        label = "%s:%s" % (p.stem, name) if multi else p.stem
        det_rows = []
        real_rows = {}
        proxy_rows = {}
        for r in (s.get("per_instance") or []):
            det_rows.append({
                "task_id": r.get("task_id"),
                "detected": bool(r.get("detected")),
                "detected_tier1": bool(r.get("detected_tier1")),
                "assertion_matched": bool(r.get("assertion_matched")),
            })
            if r.get("upgrade"):
                proxy_rows[r.get("task_id")] = bool(r["upgrade"].get("incident"))
        for r in ((s.get("plan") or {}).get("per_instance") or []):
            ur = r.get("upgrade_real")
            if ur is not None:
                real_rows[r.get("task_id")] = bool(ur.get("incident"))
        clean_rows = []
        for r in ((s.get("false_positive") or {}).get("per_case") or []):
            clean_rows.append({
                "base_case": r.get("base_case"),
                "false_flagged": bool(r.get("false_flagged")),
                "flagged_major": bool(r.get("flagged_major")),
            })
        det = s.get("detection") or {}
        n_weak = det.get("n_instances") or len(det_rows)
        arms.append({
            "label": label,
            "system": name,
            "file": str(Path(path).resolve()),
            "file_sha256": _sha256_file(path),
            "generated_at_utc": doc.get("generated_at_utc"),
            "harness_version": doc.get("harness_version"),
            "taskset": doc.get("taskset"),
            "environment": doc.get("environment"),
            "repeats": doc.get("repeats"),
            "settings": settings,
            "shield_enabled": settings.get("shield_enabled"),
            "conjunction_caliber": conj,
            "is_fixture_arm": bool(s.get("is_fixture_arm"))
                              or name in FIXTURE_ARM_NAMES,
            "n_weak_instances": n_weak,
            "det_rows": det_rows,
            "real_rows": real_rows,
            "proxy_rows": proxy_rows,
            "clean_rows": clean_rows,
            "runs": s.get("runs") or [],
            "aggregate": s.get("aggregate"),
            "agg_readings": {
                "detection_rate": det.get("detection_rate"),
                "detection_rate_tier1": det.get("detection_rate_tier1"),
                "incident_rate_proxy": (s.get("upgrade") or {}).get("incident_rate"),
                "incident_rate_real": ((s.get("plan") or {})
                                       .get("real_incident") or {})
                                      .get("incident_rate"),
                "fp_rate": ((s.get("false_positive") or {}).get("rate")),
                "fp_major_rate": ((s.get("false_positive") or {})
                                  .get("fp_major") or {}).get("rate"),
            },
        })
    return arms


def caliber_label(arm, kind):
    if kind == "runs":
        return "多种子（run 级聚合，逐指标口径见该行 caliber）"
    base = {"strict": CAL_STRICT, "loose": CAL_LOOSE,
            "incident_real": CAL_REAL_INC, "incident_proxy": CAL_PROXY_INC,
            "fp_any": CAL_FP_ANY, "fp_major": CAL_FP_MAJOR}[kind]
    if kind in ("strict", "loose") and arm["conjunction_caliber"]:
        base += CAL_CONJ
    return base


def field_path(arm, kind):
    sysname = arm["system"]
    return {
        "strict": "systems.%s.per_instance[].detected（≡ detection.tp/fn）" % sysname,
        "loose": "systems.%s.per_instance[].detected_tier1" % sysname,
        "incident_real": "systems.%s.plan.per_instance[].upgrade_real.incident"
                         "（≡ plan.real_incident）" % sysname,
        "incident_proxy": "systems.%s.per_instance[].upgrade.incident"
                          "（≡ upgrade 段）" % sysname,
        "fp_any": "systems.%s.false_positive.per_case[].false_flagged" % sysname,
        "fp_major": "systems.%s.false_positive.per_case[].flagged_major" % sysname,
        "runs": "systems.%s.runs[] / aggregate / repeats.run_dirs" % sysname,
    }[kind]


def source_ref(arm, kind):
    return {"file": arm["file"], "system": arm["system"],
            "field": field_path(arm, kind),
            "caliber": caliber_label(arm, kind),
            "generated_at_utc": arm["generated_at_utc"],
            "file_sha256": arm["file_sha256"],
            "top_level_run": 1}


# ─────────────────────────── 对比构建 ───────────────────────────

METRIC_KINDS = ("strict", "loose", "incident_real", "incident_proxy",
                "fp_any", "fp_major")
POWER_METRIC_KINDS = ("strict", "loose", "incident_real", "incident_proxy",
                      "fp_any", "fp_major")
TOST_METRIC_KINDS = ("strict", "loose")

METRIC_DISPLAY = {
    "strict": "检出率（严格 tier2）",
    "loose": "检出率（宽松 tier1）",
    "incident_real": "真实事故率（行为侧）",
    "incident_proxy": "事故率（代理口径）",
    "fp_any": "干净件报出率 FP(any)",
    "fp_major": "干净件报出率 FP(重大)",
}


def paired_bits(arm_a, arm_b, kind):
    """两臂按 task_id / base_case 对齐的 (bits_a, bits_b)。"""
    if kind == "strict":
        da = {r["task_id"]: r["detected"] for r in arm_a["det_rows"]}
        db = {r["task_id"]: r["detected"] for r in arm_b["det_rows"]}
    elif kind == "loose":
        da = {r["task_id"]: r["detected_tier1"] for r in arm_a["det_rows"]}
        db = {r["task_id"]: r["detected_tier1"] for r in arm_b["det_rows"]}
    elif kind == "incident_real":
        da, db = arm_a["real_rows"], arm_b["real_rows"]
    elif kind == "incident_proxy":
        da, db = arm_a["proxy_rows"], arm_b["proxy_rows"]
    elif kind == "fp_any":
        da = {r["base_case"]: r["false_flagged"] for r in arm_a["clean_rows"]}
        db = {r["base_case"]: r["false_flagged"] for r in arm_b["clean_rows"]}
    elif kind == "fp_major":
        da = {r["base_case"]: r["flagged_major"] for r in arm_a["clean_rows"]}
        db = {r["base_case"]: r["flagged_major"] for r in arm_b["clean_rows"]}
    else:
        raise ValueError(kind)
    common = sorted(set(da) & set(db))
    return ([1 if da[k] else 0 for k in common],
            [1 if db[k] else 0 for k in common], common)


def build_pairs(arms, main_label, explicit):
    by_label = {a["label"]: a for a in arms}
    pairs = []
    seen = set()

    def _add(a, b, why):
        key = tuple(sorted((a["label"], b["label"])))
        if key in seen or a["label"] == b["label"]:
            return
        seen.add(key)
        pairs.append({"arm_a": a, "arm_b": b, "why": why})

    if explicit:
        for spec in explicit:
            if "," not in spec:
                raise SystemExit("--compare 形如 LABEL_A,LABEL_B：%r" % spec)
            la, lb = [x.strip() for x in spec.split(",", 1)]
            for lab in (la, lb):
                if lab not in by_label:
                    raise SystemExit("--compare 引用了不存在的臂 %r（有：%s）"
                                     % (lab, "、".join(by_label)))
            _add(by_label[la], by_label[lb], "显式 --compare")
    else:
        main = by_label.get(main_label)
        if main is None:
            raise SystemExit("--main %r 不是任何臂（有：%s）"
                             % (main_label, "、".join(by_label)))
        for arm in arms:
            if arm["label"] == main["label"]:
                continue
            if arm["is_fixture_arm"]:
                continue   # 夹具臂只标定度量链，不进对比
            if arm["conjunction_caliber"] != main["conjunction_caliber"]:
                continue   # 不同口径不默认配对（须显式 --compare）
            _add(main, arm, "默认：主臂 vs 其余同口径臂")
    return pairs


# ─────────────────────────── 报告组装 ───────────────────────────

def build_report(files, arm_specs, main_label, explicit_pairs, seed, n_boot,
                 alpha, margin, tost_grid, power_specs, n_max, power_target):
    arms = []
    for path in files:
        arms.extend(extract_arms(path))
    for spec in arm_specs or []:
        if "=" not in spec:
            raise SystemExit("--arm 形如 LABEL=FILE[:SYSTEM]：%r" % spec)
        label, rest = spec.split("=", 1)
        path, _, sysname = rest.partition(":")
        got = extract_arms(path, sysname or None)
        if len(got) != 1:
            raise SystemExit("--arm %r：文件里有多个系统，请用 FILE:SYSTEM 指定"
                             % spec)
        got[0]["label"] = label.strip()
        arms.extend(got)
    labels = [a["label"] for a in arms]
    if len(set(labels)) != len(labels):
        dup = sorted({l for l in labels if labels.count(l) > 1})
        raise SystemExit("臂标签重复：%s（用 --arm LABEL=FILE[:SYSTEM] 显式命名）"
                         % "、".join(dup))
    if not main_label:
        main_label = arms[0]["label"]

    # ── 顶层 taskset 键（v4 扩容轮补上，2026-09-27）──
    # 结果目录的新鲜度核对（test_metrics_calibration 组 [E]）按
    # `taskset.n_instances` 机械比对每个 `experiment/results/*.json`，
    # 本报告落进同一目录，同样要能被同一把尺子量。
    # 语义 = **读数所依据的任务集**，取自各输入结果文件的 taskset 溯源块
    # （harness 落盘时写入），不是生成本报告时 taskset.json 的现值——
    # 本报告聚合的是输入文件的读数，钉现值会让陈旧读数冒充新鲜读数。
    # 逐臂一致时取单值；不一致/缺失时如实记 variants、n_instances 记 None
    # （[E] 会把它当陈旧处理，宁可红也不冒充新鲜）。一致性的判据是
    # **实质三元组**（version / n_instances / 弱点目录 sha256）——
    # 各结果文件落盘的 path 拼写可能一相对一绝对，不算任务集不同。
    def _ts_identity(t):
        return (t.get("taskset_version"), t.get("n_instances"),
                t.get("weaknesses_yaml_sha256"))

    ts_variants = []
    for a in arms:
        t = a.get("taskset")
        if not t:
            continue
        if not any(_ts_identity(v) == _ts_identity(t) for v in ts_variants):
            ts_variants.append(t)
    if len(ts_variants) == 1:
        taskset_block = dict(ts_variants[0])
        taskset_block["source"] = "各输入结果文件的 taskset 溯源块（逐臂一致）"
    else:
        taskset_block = {
            "path": None, "taskset_version": None, "n_instances": None,
            "source": "输入结果文件的任务集不一致或缺失（见 variants）",
            "variants": ts_variants,
        }

    report = {
        "tool": "code/tools/stats_upgrade.py",
        "tool_version": __version__,
        "generated_at_utc": datetime.now(timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "taskset": taskset_block,
        "config": {
            "seed": seed, "n_bootstrap": n_boot, "alpha": alpha,
            "tost_margin": margin, "tost_grid": tost_grid,
            "power_target": power_target, "power_n_max": n_max,
            "main_arm": main_label,
            "inputs": [str(Path(f).resolve()) for f in files],
        },
        "arms": {},
        "bootstrap_ci": {},
        "mcnemar": [],
        "tost": [],
        "power_contrasts": [],
        "multi_seed": {},
        "verdicts": [],
        "provenance_notes": [
            "所有读数只读结果 JSON 顶层（顶层读数 = run 1，harness 约定）；"
            "run 级聚合另见 multi_seed 段。",
            "口径：严格 = tier2（痕迹口径主口径）、宽松 = tier1；"
            "settings.conjunction_across_findings=true 的臂为合取口径"
            "（检出值已含跨 finding 合取救回）。",
            "夹具臂（oracle/null，is_fixture_arm）只标定度量链，不进成对对比。",
            "默认配对 = 主臂 vs 其余同口径臂（合取口径与非合取口径不默认混比）；"
            "跨口径 / 自定义对比用 --compare 显式给出。",
            "真实升级事故（行为侧）= plan.per_instance[].upgrade_real.incident，"
            "仅对产出计划的臂适用；代理口径（upgrade 段）测『是否识别到风险』，"
            "不得与行为侧混称。",
        ],
    }

    # ── 1. bootstrap CI（每臂） ──
    for arm in arms:
        key_prefix = "boot|%s" % arm["label"]
        entries = {}
        for kind in ("strict", "loose"):
            bits = [r[kind == "strict" and "detected" or "detected_tier1"]
                    for r in arm["det_rows"]]
            ci = bootstrap_rate_ci(bits, n_boot, seed, "%s|%s" % (key_prefix, kind))
            if ci:
                ci["metric"] = "检出率"
                ci["caliber"] = caliber_label(arm, kind)
                ci["source"] = source_ref(arm, kind)
                entries[kind] = ci
        if arm["real_rows"]:
            ci = bootstrap_rate_ci(list(arm["real_rows"].values()), n_boot, seed,
                                   "%s|incident_real" % key_prefix)
            ci["metric"] = "真实升级事故率（行为侧）"
            ci["caliber"] = caliber_label(arm, "incident_real")
            ci["source"] = source_ref(arm, "incident_real")
            entries["incident_real"] = ci
        if arm["proxy_rows"]:
            ci = bootstrap_rate_ci(list(arm["proxy_rows"].values()), n_boot, seed,
                                   "%s|incident_proxy" % key_prefix)
            ci["metric"] = "升级事故率（代理口径）"
            ci["caliber"] = caliber_label(arm, "incident_proxy")
            ci["source"] = source_ref(arm, "incident_proxy")
            entries["incident_proxy"] = ci
        for kind in ("fp_any", "fp_major"):
            field = "false_flagged" if kind == "fp_any" else "flagged_major"
            if arm["clean_rows"]:
                ci = bootstrap_rate_ci([r[field] for r in arm["clean_rows"]],
                                       n_boot, seed, "%s|%s" % (key_prefix, kind))
                ci["metric"] = "干净件报出率"
                ci["caliber"] = caliber_label(arm, kind)
                ci["source"] = source_ref(arm, kind)
                entries[kind] = ci
        report["bootstrap_ci"][arm["label"]] = entries
        report["arms"][arm["label"]] = {
            "provenance": {k: arm[k] for k in
                           ("system", "file", "file_sha256", "generated_at_utc",
                            "harness_version", "taskset", "environment",
                            "repeats", "settings", "is_fixture_arm")},
            "caliber_strict": caliber_label(arm, "strict"),
            "caliber_loose": caliber_label(arm, "loose"),
            "agg_readings": arm["agg_readings"],
            "n_weak_instances": arm["n_weak_instances"],
            "n_clean_cases": len(arm["clean_rows"]),
            "n_real_incident_instances": len(arm["real_rows"]),
        }

    # ── 2/3/4. 成对对比：McNemar + Holm、TOST、功效 ──
    pairs = build_pairs(arms, main_label, explicit_pairs)
    if not pairs:
        report["provenance_notes"].append(
            "本次没有可配对的臂 ⇒ McNemar/TOST/功效段为空。默认配对要求"
            "『主臂 vs 其余**同口径**非夹具臂』；跨口径对比（严格 vs 合取）"
            "须用 --compare 显式给出。")
    mcnemar_rows = []
    for pair in pairs:
        arm_a, arm_b = pair["arm_a"], pair["arm_b"]
        pair_name = "%s vs %s" % (arm_a["label"], arm_b["label"])
        for kind in METRIC_KINDS:
            bits_a, bits_b, common = paired_bits(arm_a, arm_b, kind)
            n = len(common)
            if n == 0:
                continue
            b = sum(1 for x, y in zip(bits_a, bits_b) if x and not y)
            c = sum(1 for x, y in zip(bits_a, bits_b) if y and not x)
            res = mcnemar_exact(b, c, alpha)
            delta = (b - c) / n
            mcnemar_rows.append({
                "pair": pair_name,
                "why": pair["why"],
                "metric": kind,
                "caliber_A": caliber_label(arm_a, kind),
                "caliber_B": caliber_label(arm_b, kind),
                "n_paired": n,
                "b_A_only": b,
                "c_B_only": c,
                "rate_A": round(sum(bits_a) / n, 4),
                "rate_B": round(sum(bits_b) / n, 4),
                "delta_paired": round(delta, 4),
                "p_exact": res["p_exact"],
                "n_discordant": res.get("n_discordant", 0),
                "source_A": source_ref(arm_a, kind),
                "source_B": source_ref(arm_b, kind),
                "_power": power_analysis_paired(b, c, n, alpha,
                                                power_target, n_max),
            })

    # Holm：按指标族（同 metric 跨配对）+ 全局两档
    by_metric = {}
    for i, row in enumerate(mcnemar_rows):
        by_metric.setdefault(row["metric"], []).append(i)
    for idxs in by_metric.values():
        adj = holm_adjust([mcnemar_rows[i]["p_exact"] for i in idxs])
        for i, val in zip(idxs, adj):
            mcnemar_rows[i]["p_holm_within_metric"] = round(val, 6)
            mcnemar_rows[i]["holm_family_within_metric"] = (
                "metric=%s，共 %d 个配对对比" % (mcnemar_rows[i]["metric"], len(idxs)))
    adj_all = holm_adjust([r["p_exact"] for r in mcnemar_rows])
    for r, val in zip(mcnemar_rows, adj_all):
        r["p_holm_global"] = round(val, 6)
        r["holm_family_global"] = "全部 %d 个 McNemar 检验（保守档）" % len(mcnemar_rows)
    report["mcnemar"] = mcnemar_rows

    tost_rows = []
    for pair in pairs:
        arm_a, arm_b = pair["arm_a"], pair["arm_b"]
        pair_name = "%s vs %s" % (arm_a["label"], arm_b["label"])
        for kind in TOST_METRIC_KINDS:
            bits_a, bits_b, common = paired_bits(arm_a, arm_b, kind)
            n = len(common)
            if n == 0:
                continue
            n10 = sum(1 for x, y in zip(bits_a, bits_b) if x and not y)
            n01 = sum(1 for x, y in zip(bits_a, bits_b) if y and not x)
            res = tost_paired_exact(n10, n01, n, margin, alpha, tost_grid)
            if res is None:
                continue
            res.update({
                "pair": pair_name,
                "metric": kind,
                "caliber": caliber_label(arm_a, kind),
                "source_A": source_ref(arm_a, kind),
                "source_B": source_ref(arm_b, kind),
            })
            tost_rows.append(res)
    report["tost"] = tost_rows

    # 功效读数并入 mcnemar 行 + 单列
    for r in mcnemar_rows:
        r["power"] = r.pop("_power")

    # 聚合对比功效（--power-contrast）
    for spec in power_specs or []:
        try:
            left, right = spec.split(":", 1)
            x1, n1 = (int(v) for v in left.split("/"))
            x2, n2 = (int(v) for v in right.split("/"))
        except ValueError:
            raise SystemExit("--power-contrast 形如 x1/n1:x2/n2（如 0/25:25/25）：%r"
                             % spec)
        report["power_contrasts"].append(
            power_aggregate_contrast(x1, n1, x2, n2, alpha, power_target, n_max,
                                     spec))

    # ── 5. 多种子方差 ──
    rate_units = {
        "detection_rate": "weak",
        "detection_rate_tier1": "weak",
        "assertion_rate": "weak",
        "fp_rate": "clean",
        "fp_major_rate": "clean",
        "incident_rate": "t_irr",
    }
    for arm in arms:
        if len(arm["runs"]) < 2:
            report["multi_seed"][arm["label"]] = {
                "n_runs": len(arm["runs"]),
                "note": ("run 数 < 2（repeats.k=%s）：run 间 SD / 组内相关不适用"
                         % ((arm.get("repeats") or {}).get("k"))),
                "repeats": arm.get("repeats"),
            }
            continue
        n_weak = arm["n_weak_instances"]
        n_clean = len(arm["clean_rows"]) or None
        n_tirr = len(arm["real_rows"]) or None
        units = {"weak": n_weak, "clean": n_clean, "t_irr": n_tirr}
        block = {"repeats": arm.get("repeats"), "metrics": {}}
        for key in ("detection_rate", "detection_rate_tier1", "assertion_rate",
                    "fp_rate", "fp_major_rate", "incident_rate",
                    "mean_reported_per_instance"):
            is_rate = key != "mean_reported_per_instance"
            n_units = units.get(rate_units.get(key, "")) if is_rate else None
            block["metrics"][key] = multi_seed_variance(
                arm["runs"], key, n_units, is_rate=is_rate)
            block["metrics"][key]["caliber"] = {
                "detection_rate": CAL_STRICT + (CAL_CONJ if arm["conjunction_caliber"] else ""),
                "detection_rate_tier1": CAL_LOOSE + (CAL_CONJ if arm["conjunction_caliber"] else ""),
                "assertion_rate": "后果断言率（with_assertion / tp）",
                "fp_rate": CAL_FP_ANY,
                "fp_major_rate": CAL_FP_MAJOR,
                "incident_rate": CAL_PROXY_INC,
                "mean_reported_per_instance": "每实例平均报出条数（防滥报抬 precision）",
            }[key]
            block["metrics"][key]["source"] = source_ref(arm, "runs")
        # 与 harness 自带 aggregate 交叉核对（sd 口径应一致）
        xcheck = {}
        for key in ("detection_rate", "detection_rate_tier1", "incident_rate"):
            agg = (arm.get("aggregate") or {}).get(key) or {}
            mine = block["metrics"][key]["sd_run"]
            xcheck[key] = {"aggregate_sd": agg.get("sd"),
                           "recomputed_sd": mine,
                           "match": (agg.get("sd") is not None and mine is not None
                                     and abs(agg["sd"] - mine) < 1e-6)}
        block["cross_check_vs_harness_aggregate"] = xcheck
        report["multi_seed"][arm["label"]] = block

    # ── 措辞裁决（按差异/等效检验的结论定可写与不可写） ──
    for r in mcnemar_rows:
        sig = r["p_holm_global"] < alpha
        direction = ("A 高于 B" if r["delta_paired"] > 0
                     else ("B 高于 A" if r["delta_paired"] < 0 else "持平"))
        if sig:
            text = ("可说『存在差异』：%s（%s，Holm 全局 p=%.4f）"
                    % (direction, r["pair"], r["p_holm_global"]))
        else:
            text = ("不可说『收益是确定的』：%s 差异未达显著"
                    "（精确 McNemar p=%.4f，Holm 全局 p=%.4f）；"
                    "也不可反过来说『无差异』" % (r["pair"], r["p_exact"],
                                             r["p_holm_global"]))
        report["verdicts"].append({
            "kind": "差异（McNemar + Holm）",
            "pair": r["pair"], "metric": r["metric"],
            "caliber": r["caliber_A"],
            "text": text,
            "source": r["source_A"],
        })
    for r in tost_rows:
        if r.get("not_evaluable"):
            continue
        if r["equivalent"]:
            text = ("可说『相当』：%s 检出率差在 ±%.2f 内等效"
                    "（TOST 双侧 p=%.4f / %.4f）"
                    % (r["pair"], r["margin"], r["p_lower"], r["p_upper"]))
        else:
            text = ("不可说『相当』：%s 等效未被证明"
                    "（TOST p_lower=%.4f，p_upper=%.4f，界值 ±%.2f）；"
                    "『未发现差异 ≠ 相当』" % (r["pair"], r["p_lower"],
                                            r["p_upper"], r["margin"]))
        report["verdicts"].append({
            "kind": "等效（TOST）",
            "pair": r["pair"], "metric": r["metric"],
            "caliber": r["caliber"],
            "text": text,
            "source": r["source_A"],
        })

    return report


# ─────────────────────────── Markdown 渲染 ───────────────────────────

def render_markdown(report):
    cfg = report["config"]
    out = []
    w = out.append

    def _f(v):
        return "—" if v is None else v

    def _m(kind):
        return METRIC_DISPLAY.get(kind, kind)
    w("# F1 统计升级报告（stats_upgrade.py v%s）" % report["tool_version"])
    w("")
    w("生成时间：%s ｜ 种子 `%d` ｜ bootstrap `%d` 次 ｜ α=`%s` ｜ "
      "TOST 界值 ±`%.2f` ｜ 功效目标 `1-β≥%.1f`"
      % (report["generated_at_utc"], cfg["seed"], cfg["n_bootstrap"],
         cfg["alpha"], cfg["tost_margin"], cfg["power_target"]))
    ts_blk = report.get("taskset") or {}
    w("任务集（读数所依据，取自输入结果文件溯源块）：`%s` ｜ "
      "taskset_version=`%s` ｜ n_instances=`%s`"
      % (ts_blk.get("path"), ts_blk.get("taskset_version"),
         ts_blk.get("n_instances")))
    w("")
    w("输入文件：")
    for f in cfg["inputs"]:
        w("- `%s`" % f)
    w("")
    w("主臂：`%s`。口径：严格 = tier2（主口径）、宽松 = tier1；"
      "合取口径见各臂标注。行为侧 = 计划口径真实升级事故；代理口径 = "
      "是否识别到风险。" % cfg["main_arm"])
    w("")

    w("## 1. bootstrap 95% CI（实例层重采样）")
    w("")
    w("| 臂 | 指标 | 口径 | n | 读数 | 95% CI | 来源 |")
    w("|---|---|---|---|---|---|---|")
    for label, entries in report["bootstrap_ci"].items():
        for kind in ("strict", "loose", "incident_real", "incident_proxy",
                     "fp_any", "fp_major"):
            ci = entries.get(kind)
            if not ci:
                continue
            w("| %s | %s | %s | %d/%d | %.4f | [%.4f, %.4f]%s | `%s` |"
              % (label, ci["metric"], ci["caliber"], ci["n_positive"], ci["n"],
                 ci["point"], ci["ci95_low"], ci["ci95_high"],
                 "（退化）" if ci["degenerate"] else "",
                 ci["source"]["field"]))
    w("")

    w("## 2. McNemar 精确检验 + Holm 校正")
    w("")
    w("| 对比 | 指标（口径） | n | b(仅 A) | c(仅 B) | Δ(A-B) | p 精确 | "
      "p Holm(指标族) | p Holm(全局) | 来源 |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for r in report["mcnemar"]:
        w("| %s | %s（%s） | %d | %d | %d | %+.4f | %.4f | %.4f | %.4f | `%s` |"
          % (r["pair"], _m(r["metric"]), r["caliber_A"], r["n_paired"],
             r["b_A_only"], r["c_B_only"], r["delta_paired"], r["p_exact"],
             r["p_holm_within_metric"], r["p_holm_global"],
             r["source_A"]["field"]))
    w("")

    w("## 3. TOST 等效检验（检出率差，界值 ±%.2f）" % cfg["tost_margin"])
    w("")
    w("| 对比 | 指标（口径） | n | Δ̂ | Wald 90% CI | p(Δ≤-δ) | p(Δ≥+δ) | "
      "等效结论 |")
    w("|---|---|---|---|---|---|---|---|")
    for r in report["tost"]:
        if r.get("not_evaluable"):
            w("| %s | %s | — | %s | — | — | — | 不可评 |"
              % (r["pair"], _m(r["metric"]), r.get("delta_hat")))
            continue
        w("| %s | %s（%s） | %d | %+.4f | [%+.4f, %+.4f] | %.4f | %.4f | %s |"
          % (r["pair"], _m(r["metric"]), r["caliber"], r["n_pairs"], r["delta_hat"],
             r["wald_ci90"][0], r["wald_ci90"][1], r["p_lower"], r["p_upper"],
             "等效（|Δ|<δ）" if r["equivalent"] else "未证明等效"))
    w("")

    w("## 4. 功效分析（配对二项结构）")
    w("")
    w("| 对比 | 指标 | 结构 (b,c)/n | 功效@当前 n | 达 %.1f 所需 n | 备注 |"
      % cfg["power_target"])
    w("|---|---|---|---|---|---|")
    for r in report["mcnemar"]:
        pw = r.get("power") or {}
        if not pw:
            continue
        req = pw.get("required_n_for_target")
        notes = "；".join(pw.get("notes") or []) or "—"
        w("| %s | %s | (%d,%d)/%d | %s | %s | %s |"
          % (r["pair"], _m(r["metric"]), pw.get("b_A_only", 0), pw.get("c_B_only", 0),
             pw.get("n_pairs", 0),
             ("%.4f" % pw["power_at_current_n"])
             if pw.get("power_at_current_n") is not None else "—",
             str(req) if req is not None else "不可估/未达", notes))
    for pc in report["power_contrasts"]:
        w("")
        w("### 聚合对比 `%s`（%s vs %s）"
          % (pc["contrast"], pc["counts"]["arm1"], pc["counts"]["arm2"]))
        w("")
        ind = pc.get("independent") or {}
        w("- 独立两比例（正态近似）：功效@当前 n = %s，达 %.1f 所需 n/臂 = %s"
          % (ind.get("power_at_current_n"), cfg["power_target"],
             ind.get("required_n_per_arm_for_target")))
        if "paired_by_overlap" in pc:
            w("- 配对结构（按可行重叠枚举）：")
            for row in pc["paired_by_overlap"]:
                w("  - 重叠 a=%d ⇒ (b,c)=(%d,%d)/%d：功效@当前 n=%s，"
                  "所需 n=%s" % (row["overlap_a"], row["b_A_only"],
                                row["c_B_only"], row["n_pairs"],
                                row.get("power_at_current_n"),
                                row.get("required_n_for_target")))
            w("- %s" % pc.get("paired_note"))
    w("")

    w("## 5. 多种子方差（repeats.run_dirs）")
    w("")
    for label, block in report["multi_seed"].items():
        w("### %s" % label)
        w("")
        if "metrics" not in block:
            w("- %s" % block.get("note"))
            continue
        w("| 指标 | 口径 | k | mean | run 间 SD | 二项参照 SD | 过度离散 F | "
          "ICC(矩估计) |")
        w("|---|---|---|---|---|---|---|---|")
        for key, m in block["metrics"].items():
            w("| %s | %s | %d | %s | %s | %s | %s | %s |"
              % (key, _f(m.get("caliber")), _f(m.get("n_runs")),
                 _f(m.get("mean")), _f(m.get("sd_run")),
                 _f(m.get("sd_binomial_reference")),
                 _f(m.get("overdispersion_F")),
                 _f(m.get("icc_moment"))))
        w("")
        seen_notes = []
        for key, m in block["metrics"].items():
            for nk in ("icc_exact_note", "icc_note", "note"):
                note = m.get(nk)
                if note and note not in seen_notes:
                    seen_notes.append(note)
                    w("- [%s] %s" % (key, note))
        x = block.get("cross_check_vs_harness_aggregate") or {}
        w("- 与 harness `aggregate.sd` 交叉核对：%s"
          % "，".join("%s=%s" % (k, "一致" if v["match"] else "不一致(%s≠%s)"
                                % (v["recomputed_sd"], v["aggregate_sd"]))
                      for k, v in x.items()))
    w("")

    w("## 6. 措辞裁决（替换『收益是确定的 / 相当』）")
    w("")
    for v in report["verdicts"]:
        w("- **[%s]** %s ｜ %s（%s）｜ 来源 `%s`"
          % (v["kind"], v["text"], v["pair"], _m(v["metric"]),
             v["source"]["field"]))
    w("")
    w("## Provenance")
    w("")
    for note in report["provenance_notes"]:
        w("- %s" % note)
    for label, arm in report["arms"].items():
        pv = arm["provenance"]
        w("- `%s`：system=`%s`，file=`%s`，sha256=`%s`，generated=`%s`，"
          "taskset_version=%s，n_instances=%s，repeats.k=%s"
          % (label, pv["system"], pv["file"], pv["file_sha256"][:16] + "…",
             pv["generated_at_utc"], (pv.get("taskset") or {}).get("taskset_version"),
             (pv.get("taskset") or {}).get("n_instances"),
             (pv.get("repeats") or {}).get("k")))
    w("")
    return "\n".join(out) + "\n"


# ─────────────────────────── CLI ───────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="F1 统计升级工具：bootstrap CI / McNemar+Holm / TOST / "
                    "功效 / 多种子方差（只读结果 JSON）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法示例：")[-1])
    ap.add_argument("results", nargs="+", help="harness 结果 JSON（若干）")
    ap.add_argument("--arm", action="append", default=[],
                    help="附加臂 LABEL=FILE[:SYSTEM]（可重复）")
    ap.add_argument("--compare", action="append", default=[],
                    help="显式配对 LABEL_A,LABEL_B（可重复；给了则不做默认配对）")
    ap.add_argument("--main", default=None,
                    help="主臂标签（默认第一个文件的第一个臂）")
    ap.add_argument("--out-json", default=None, help="统计报告 JSON 输出路径")
    ap.add_argument("--out-md", default=None, help="统计报告 Markdown 输出路径")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED,
                    help="bootstrap 种子（默认 %d，写入输出）" % DEFAULT_SEED)
    ap.add_argument("--boot", type=int, default=DEFAULT_N_BOOT,
                    help="bootstrap 重采样次数（默认 %d）" % DEFAULT_N_BOOT)
    ap.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN,
                    help="TOST 等效界值 ±margin（默认 %.2f）" % DEFAULT_MARGIN)
    ap.add_argument("--tost-grid", type=int, default=DEFAULT_TOST_GRID,
                    help="TOST 干扰参数网格分辨率（默认 %d）"
                         % DEFAULT_TOST_GRID)
    ap.add_argument("--power-contrast", action="append", default=[],
                    help="聚合功效对比 x1/n1:x2/n2（可重复，如 0/25:25/25、1/8:3/8）")
    ap.add_argument("--power-target", type=float, default=0.8)
    ap.add_argument("--power-n-max", type=int, default=DEFAULT_POWER_N_MAX,
                    help="所需样本量搜索上限（默认 %d）" % DEFAULT_POWER_N_MAX)
    args = ap.parse_args(argv)

    if args.boot < 100:
        raise SystemExit("--boot 至少 100（默认 10000）")
    if not 0 < args.alpha < 1:
        raise SystemExit("--alpha 必须在 (0,1) 内")
    if args.margin <= 0:
        raise SystemExit("--margin 必须 > 0")

    report = build_report(
        files=args.results, arm_specs=args.arm, main_label=args.main,
        explicit_pairs=args.compare, seed=args.seed, n_boot=args.boot,
        alpha=args.alpha, margin=args.margin, tost_grid=args.tost_grid,
        power_specs=args.power_contrast, n_max=args.power_n_max,
        power_target=args.power_target)

    first = Path(args.results[0])
    out_json = Path(args.out_json) if args.out_json else (
        first.parent / "stats-upgrade-report.json")
    out_md = Path(args.out_md) if args.out_md else (
        first.parent / "stats-upgrade-report.md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    out_md.write_text(render_markdown(report), encoding="utf-8")
    print("已写出 %s" % out_json)
    print("已写出 %s" % out_md)
    print("种子 %d / bootstrap %d 次 / α=%.2f / TOST 界值 ±%.2f"
          % (args.seed, args.boot, args.alpha, args.margin))
    return 0


if __name__ == "__main__":
    sys.exit(main())
