#!/usr/bin/env python3
"""判定机路径的唯一解析点（供 `harness.py` 与三个测试套件共用）。

注入签名判定机（`signature.py`）是只读的共享冻结件，物理位置随运行场景而变，故按
以下顺序解析，取第一个真实存在的：

1. 环境变量 `CTD_LA_MATCHER`，操作者显式声明，优先级最高；
2. `<code_root>/vendor/signature.py`，本仓钉版副本，独立导出仓库里的权威；
3. 上层仓库里各任务形态自带的冻结副本（按 `papers/*/code/ctd_ccv/signature.py`
   通配查找，`repo_root` 按 `papers/` 与 `rules/` 两个标记向上定位）。

三档都没有时返回 `None`。本模块不抛异常：独立仓库里没有上层仓库是正常状态，
不是错误，怎么报错由调用方决定。

## 两处设计取舍

钉版副本保持文件名 `signature.py`，不改叫 `matcher.py`。
`experiment/tasks/taskset.json` 的 `provenance.matcher_basename` 记录的是
`"signature.py"`；改名会让任务集溯源字段与既存产物对不上。

副本放在 `vendor/` 而不是直接混进 `code/`。它是只读的第三方冻结件，
与自有源码分开，读者一眼就能看出哪些代码不出自本项目。

本模块零依赖，可被 `importlib.util.spec_from_file_location` 按路径加载，
`harness.py` 与三个测试套件都是以这种方式取用它的，故不要在这里引入任何 import。
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "CTD_LA_MATCHER"
VENDOR_RELPATH = Path("vendor") / "signature.py"


def find_repo_root(start):
    """向上找同时含 `papers/` 与 `rules/` 的目录；找不到返回 None。

    按标记找而不是数 `parents[N]`：`code/harness.py` 比 `code/tools/x.py` 少一级，
    同一个 N 在两类文件里指向不同目录，这个 off-by-one 曾导致定位到错误的仓库根。
    """
    for cand in [start] + list(start.parents):
        if (cand / "papers").is_dir() and (cand / "rules").is_dir():
            return cand
    return None


def candidates(code_root, start=None):
    """按优先级返回 [(来源说明, 路径), ...]；无论是否存在都列出，便于报错时展示。"""
    out = []
    env = (os.environ.get(ENV_VAR) or "").strip()
    if env:
        out.append(("环境变量 %s" % ENV_VAR, Path(env)))
    out.append(("本仓钉版副本", Path(code_root) / VENDOR_RELPATH))
    repo = find_repo_root(Path(start) if start is not None else Path(code_root))
    if repo is not None:
        # 上层仓库里若另有该冻结件的副本（同一套判定在多处共用的情形），一并纳入。
        # 按目录通配查找而不是写死某一处的路径：写死会让仓库布局一变就静默失效，
        # 而"路径不存在"要到运行那一刻才暴露。
        for cand in sorted((repo / "papers").glob("*/code/ctd_ccv/signature.py")):
            out.append(("上层仓库内的冻结副本", cand))
    return out


def resolve(code_root, start=None):
    """返回第一个存在的候选路径；都没有则 None。"""
    for _, path in candidates(code_root, start):
        if path.is_file():
            return path
    return None


def describe(code_root, start=None):
    """把所有候选连同存在与否渲染成一段可直接打印的说明。"""
    lines = ["判定机未找到。按优先级尝试过："]
    for label, path in candidates(code_root, start):
        lines.append("  [%s] %s" % ("命中" if path.is_file() else "缺失", path))
        lines.append("        （%s）" % label)
    lines.append("用 --matcher-module 指定路径，或设 %s 环境变量。" % ENV_VAR)
    return "\n".join(lines)