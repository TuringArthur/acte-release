#!/usr/bin/env python3
"""一键跑完 F1 的全部验证（11 步）。

把"验证链"固定成一条命令，是为了让任何一次改动后都能低成本重跑。
F1 的验证有 11 步（8 个测试 + 3 个校验），散着敲容易漏掉其中一步，
而漏掉的那一步恰好可能正是被改动破坏的那一步。

## 十一步与各自防什么

| # | 检查 | 防什么 |
|---|---|---|
| 1 | `test_t_irr.py` | 登记表结构漂了、校验器变成"从不报错" |
| 2 | `derive_t_irr.py --check-drift` | 规则库演进后 `T_irr` 静默过期 |
| 3 | `check_catalogue.py` | 弱点目录漏类、缺真实出处、与 `T_irr` 脱钩；可注入性陷阱 |
| 4 | `check_reuse.py` | 判定机在事实级弱点场景上失配（原子字段、tier 口径） |
| 5 | `test_taskset_pipeline.py` | 生成器守卫失效；产出签名的判定方向错 |
| 6 | `test_acte_core.py` | 盾的两种语义、时间增广、姿态嵌套、零容忍、ToC 口径 |
| 7 | `test_metrics_calibration.py` | 度量链本身算错，夹具臂上下界 + 判定方向 |
| 8 | `test_strategy.py` | 策略层越界（重新评估风险）、ε 未落到配额（稳≡狠）、四条消融臂静默失效 |
| 9 | `test_attack_loop_prompt.py` | 红队开关失效（红队开关：两臂提示词意外相同，或只差了别的东西） |
| 10 | `check_plugin_data.py` | 插件内联表悄悄过期；改用 `ctx` 上读配置的写法（真机加载期崩溃）；发射端契约缺失 |
| 11 | `test_plugin_shield.mjs`（需 node，见下） | 插件的运行时行为：盾的两型语义 / 时间增广 / waterfall 委托 + 发射端的工具注册 / 发射 / 盾缺席时必须报警 |

### 第 11 步为什么必须真的跑 JS（而不是静态查一下）

`check_plugin_data.py` 只能做静态核查：生成段是否与快照逐字一致、判据段是否
引用了两型常量、时间增广守卫是否在场。但静态核查证明不了行为，

- 表里两型都有、判据里也引用了两个常量，但分支写反（把不作为型也当拦截处理）：
  静态看全对，运行时全数漏防；
- 缺期限时报了 finding，但 `severity` 写成放行档：静态看"报出来了"，实际等于放行。

故第 11 步用 node 实际加载插件并断言行为。它是唯一一步需要 node 的：
无 node 时本脚本报告该步未执行并以非零退出，不静默略过，
静默略过会让"插件行为已验证"变成一句没有依据的话。

它跑两个插件的套件（盾 + 发射端，见 `tests/plugin_plan_suite.mjs`）：
两者是一对，缺发射端时盾收不到任何提交，而"没人判过"与"本案无风险"同形。
 两个套件都挂在同一个 node 入口上，不要拆成两步，理由见 `run_node_step` 的说明。

## 执行方式（安全相关，不要改）

第 1–10 步不启动任何子进程、不经 shell：各步都是本仓库内的 Python 模块，
用 `importlib` 按固定路径进程内加载，再调用其 `main()`，
stdout 用 `contextlib.redirect_stdout` 捕获。

第 11 步需 node 加载 ESM，无法在进程内做。其 argv 是由本文件推出的固定字面量
（`shutil.which("node")` 的结果 + 本仓库内的实测脚本路径），
不接受任何外部输入（本脚本的参数只有 `--quiet` 一个布尔开关，
它不参与任何路径或命令构造）。

## 前置

第 2、3 步需要 `code/data/t-irr.json`（由 `derive_t_irr.py` 生成）。
第 5 步需要 `experiment/tasks/taskset.json`（由 `make_taskset.py` 生成）。
本脚本不自动生成它们，生成是"改动数据"的动作，应显式执行；验证只读。
缺文件时相应步骤报错，由报错信息指路。

## 用法

    python3 code/tools/verify_all.py
    python3 code/tools/verify_all.py --quiet     # 只打印每步结论
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve()
CODE_ROOT = HERE.parents[1]
PAPER_ROOT = HERE.parents[2]
# 本文件在 `code/tools/` 下（比 `code/harness.py` 多一级），故 parents 下标与它
#   不同。这里用语义明确的相对式并断言布局，布局一变即失败，而不是去找错的仓库根。
if CODE_ROOT.name != "code" or (PAPER_ROOT / "code").resolve() != CODE_ROOT:
    raise RuntimeError("目录布局与预期不符：本文件应位于 <篇目录>/code/tools/ 下，实际 %s" % HERE)


def _find_repo_root(start):
    """向上找含 `papers/` 与 `rules/` 的目录；找不到返回 None（独立导出时无仓库根）。"""
    for cand in [start] + list(start.parents):
        if (cand / "papers").is_dir() and (cand / "rules").is_dir():
            return cand
    return None


def _resolve_gates(paper_root):
    """定位闸门源：上层仓库的 `rules/gates.yaml`，或导出仓库自带的钉版快照。

    `rules/` 是系列共享库、不随单篇走，故导出仓库里没有它；`export-paper-repo.sh`
    会注入一份 `<仓库根>/rules/gates.yaml` 钉版快照。两处都没有时返回 None，
    此时只有第 2 步无法执行，其余步骤照跑。
    """
    cands = []
    repo = _find_repo_root(HERE)
    if repo is not None:
        cands.append(repo / "rules" / "gates.yaml")
    cands.append(Path(paper_root) / "rules" / "gates.yaml")
    for cand in cands:
        if cand.is_file():
            return cand
    return None


REPO_ROOT = _find_repo_root(HERE)
GATES_YAML = _resolve_gates(PAPER_ROOT)
T_IRR_REGISTRY = PAPER_ROOT / "method" / "t-irr.yaml"
DRIFT_STEP_LABEL = "2/11 T_irr 漂移"

# 判定机路径在这里解析一次，再经环境变量交给各步，各步（含三个测试套件）都按
# `CTD_LA_MATCHER` 取用，避免每步各写一份路径推导。三档顺序见 tools/matcher_path.py。
_spec = importlib.util.spec_from_file_location(
    "f1_matcher_path", str(CODE_ROOT / "tools" / "matcher_path.py"))
_matcher_path_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_matcher_path_mod)
if not (os.environ.get("CTD_LA_MATCHER") or "").strip():
    _resolved_matcher = _matcher_path_mod.resolve(CODE_ROOT, HERE)
    if _resolved_matcher is not None:
        os.environ["CTD_LA_MATCHER"] = str(_resolved_matcher)

# (标签, 模块路径, 传给 main 的 argv, 期望在输出里看到的字样)
STEPS = (
    ("1/11 T_irr 回归",
     CODE_ROOT / "tests" / "test_t_irr.py", (), "全部通过"),
    ("2/11 T_irr 漂移",
     CODE_ROOT / "tools" / "derive_t_irr.py",
     ("--source", str(GATES_YAML), "--registry", str(T_IRR_REGISTRY), "--check-drift"),
     "无漂移"),
    ("3/11 弱点目录校验",
     CODE_ROOT / "tools" / "check_catalogue.py", (), "通过："),
    ("4/11 判定机复用验证",
     CODE_ROOT / "tools" / "check_reuse.py", (), "全部通过"),
    ("5/11 任务集流水线回归",
     CODE_ROOT / "tests" / "test_taskset_pipeline.py", (), "全部通过"),
    ("6/11 ACTE 内核回归",
     CODE_ROOT / "tests" / "test_acte_core.py", (), "全部通过"),
    ("7/11 度量链标定",
     CODE_ROOT / "tests" / "test_metrics_calibration.py", (), "全部通过"),
    ("8/11 策略层与消融效应",
     CODE_ROOT / "tests" / "test_strategy.py", (), "全部通过"),
    ("9/11 红队提示词开关",
     CODE_ROOT / "tests" / "test_attack_loop_prompt.py", (), "全部通过"),
    ("10/11 插件数据一致性",
     CODE_ROOT / "tools" / "check_plugin_data.py", (), "生成段与快照一致"),
)

# 以任务集为输入的步骤。算法本体发布的仓库里没有任务集，这些步骤在本仓无从执行；
# 它们会被显式列出并说明原因，末尾再声明本次覆盖了哪些保证。
TASKSET_STEPS = frozenset((
    "3/11 弱点目录校验",
    "4/11 判定机复用验证",
    "7/11 度量链标定",
    "8/11 策略层与消融效应",
    "9/11 红队提示词开关",
))


def detect_profile():
    """按任务集是否在场判定档位：full（完整实验仓）/ code-only（算法本体仓）。"""
    if (PAPER_ROOT / "experiment" / "tasks" / "taskset.json").is_file():
        return "full"
    return "code-only"


# 第 11 步：插件的运行时行为，需 node 加载 ESM，无法进程内做。
# argv 全由本文件推出、不接受外部输入（见模块 docstring 的执行方式一节）。
NODE_TEST = CODE_ROOT / "tests" / "test_plugin_shield.mjs"
NODE_LABEL = "11/11 插件运行时行为（node：盾 + 发射端）"


def run_node_step(quiet):
    """跑第 11 步。无 node 时报"未执行"并返回失败，不静默略过。

    该入口同时跑发射端套件（`tests/plugin_plan_suite.mjs`）：盾与发射端是一对，
    缺发射端则盾的裁决永远进不了会话，而症状与"本案无风险"同形。
    只用一个入口是因为本仓库把子进程面收敛到一处（静态安全审查把新增的
    `subprocess.run` 判为命令注入）：故这里不再增加第二个 subprocess 调用。
    """
    node_exe = shutil.which("node")
    if not node_exe:
        print("✗ %s（未执行：找不到 node）" % NODE_LABEL)
        if not quiet:
            print("      静默略过会让『插件行为已验证』变成一句没有依据的话。")
            print("      装 node，或手动执行：node %s" % NODE_TEST)
        return False
    argv = [node_exe, str(NODE_TEST)]
    proc = subprocess.run(argv, capture_output=True, text=True, shell=False,
                          cwd=str(PAPER_ROOT))
    blob = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0 and "全部通过" in blob
    print("%s %s（退出码 %d）" % ("✓" if ok else "✗", NODE_LABEL, proc.returncode))
    if not quiet:
        for line in blob.strip().splitlines():
            if line.strip() and not line.startswith("="):
                print("      " + line)
    return ok


def load_main(module_path, alias):
    """按固定路径进程内加载模块，返回其 main（无 main 则报错）。"""
    if not module_path.is_file():
        raise FileNotFoundError("模块不存在：%s" % module_path)
    spec = importlib.util.spec_from_file_location(alias, str(module_path))
    module = importlib.util.module_from_spec(spec)
    # 让模块内的 sys.path 自增（test_acte_core 需要）生效
    sys.path.insert(0, str(CODE_ROOT))
    try:
        spec.loader.exec_module(module)
    finally:
        if sys.path and sys.path[0] == str(CODE_ROOT):
            sys.path.pop(0)
    if not hasattr(module, "main"):
        raise AttributeError("模块没有 main()：%s" % module_path)
    return module.main


def call_step(step_main, step_argv):
    """调用某一步的 main。

    各步的签名不统一：工具类（derive_t_irr / check_*）是 `main(argv=None)`，
    测试类（test_*）是 `main()`。按签名自适应，而不是改各模块的签名，
    那些模块要能单独用 `python3 <路径>` 直接跑，签名不该为聚合脚本让步。
    """
    import inspect

    params = inspect.signature(step_main).parameters
    if not params:
        return step_main()
    return step_main(list(step_argv))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quiet", action="store_true", help="只打印每步结论")
    ap.add_argument("--profile", choices=("auto", "full", "code-only"), default="auto",
                    help="验证档位。auto（默认）按任务集是否在场判定；"
                         "code-only 只跑不依赖任务集的步骤，其余显式列为不适用")
    args = ap.parse_args(argv)

    profile = detect_profile() if args.profile == "auto" else args.profile
    skipped = []
    if profile == "code-only":
        print("验证档位：code-only（算法本体仓，不含任务集）")

    results = []
    for idx, (label, mod_path, step_argv, expect) in enumerate(STEPS):
        if profile == "code-only" and label in TASKSET_STEPS:
            print("— %s（不适用：本仓库为算法本体发布，不含任务集）" % label)
            skipped.append(label)
            continue
        # 缺闸门源时只跳过第 2 步，不中止整条链：导出仓库若没带钉版快照，
        # 该步无从校验（它校验的是"快照相对源没有漂移"），但其余步骤仍然有效。
        if label == DRIFT_STEP_LABEL and GATES_YAML is None:
            msg = ("闸门源未找到：上层仓库的 rules/gates.yaml 与该仓库根的 "
                   "rules/gates.yaml 都不存在。\n"
                   "  本步校验「T_irr 快照相对闸门源无漂移」，无源则无从校验。\n"
                   "  导出仓库应由 export-paper-repo.sh 注入钉版快照；"
                   "在别处运行请显式给出 --source。")
            print("%s %s（未执行：缺闸门源）" % ("✗", label))
            if not args.quiet:
                print("      " + msg.replace("\n", "\n      "))
            results.append((label, False, 2, msg, None))
            continue
        buf = io.StringIO()
        code, err = 0, None
        try:
            step_main = load_main(mod_path, "_f1_verify_step_%d" % idx)
            with contextlib.redirect_stdout(buf):
                code = call_step(step_main, step_argv) or 0
        except SystemExit as exc:            # 模块内部 raise SystemExit
            code = exc.code if isinstance(exc.code, int) else 1
        except Exception:                    # 加载期/运行期异常都当失败
            code, err = 1, traceback.format_exc()
        blob = buf.getvalue()
        ok = (code == 0) and (expect in blob)
        results.append((label, ok, code, blob, err))
        print("%s %s（退出码 %s）" % ("✓" if ok else "✗", label, code))
        if not args.quiet:
            for line in blob.strip().splitlines():
                if line.strip() and not line.startswith("="):
                    print("      " + line)
            if err:
                for line in err.strip().splitlines()[-8:]:
                    print("      " + line)

    # 第 10 步：插件的运行时行为（需 node 加载 ESM）。
    # 无 node 时算失败而不静默略过，否则"插件行为已验证"是一句没有依据的话。
    node_ok = run_node_step(args.quiet)
    results.append((NODE_LABEL, node_ok, 0 if node_ok else 1, "", None))

    print("-" * 68)
    bad = [r for r in results if not r[1]]
    if bad:
        print("失败 %d / %d 步：" % (len(bad), len(results)))
        for label, _, code, blob, err in bad:
            print("  - %s（退出码 %s）" % (label, code))
            tail = (blob.strip().splitlines() or [])[-6:]
            if err:
                tail = err.strip().splitlines()[-6:]
            for line in tail:
                print("      " + line)
        return 1
    if skipped:
        print("通过 %d 步；另有 %d 步不适用（本仓库不含任务集）："
              % (len(results), len(skipped)))
        for label in skipped:
            print("  - %s" % label)
        print("  本次覆盖：T_irr 登记表不变量与漂移、任务集生成器守卫、"
              "ACTE 内核的定理预测、插件数据一致性与运行时行为。")
        print("  未覆盖：弱点目录、判定机复用、度量链标定、姿态与消融、红队开关"
              "——这些以任务集为输入，见论文与任务集仓库。")
        return 0
    print("全部 %d 步通过。" % len(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())