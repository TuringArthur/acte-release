#!/usr/bin/env python3
"""校验插件内联的 `T_irr` 表与权威数据一致（防静默漂移）。

## 为什么必须有这个工具

DSH 的运行时插件从 profile 的 `node_modules/` 按包名加载。为保持零依赖，
插件必须把 `T_irr` 内联在 JS 里，于是同一份规则有了两处载体：

- 权威：`code/data/t-irr.json`（由 `derive_t_irr.py` 从闸门源生成，带 sha256）
- 运行时副本：`acte-shield.js` 的 `GENERATED:T_IRR` 段

两处不同步的后果在运行时完全看不出来，插件照常加载、照常挂载、
`announce` 照常打印"层已启用"，只是它拦的规则是旧的。
这正是"插件看着装上了、其实层是哑的"那一类失效。

## 本工具怎么查（以及为什么这么查）

副本是生成的（`gen_plugin_shield.py`），故一致性的定义很干脆：
用快照重新渲染一遍，看文本是否相同。于是：

- 不需要解析 JavaScript（不引入 JS 解析器依赖）；
- 不需要启动 node（不引入子进程与命令拼接面）；
- 不需要比对哈希（哈希只告诉你"不同"，文本比对能告诉你哪一行不同）。

错误信息因此能给到"首个差异在第 N 行：文件是什么、应为是什么"，
而"不一致"三个字会让人回去人工 diff，那正是这类守卫被绕过的原因。

## 除了"一致"还查什么

| 项 | 为什么 |
|---|---|
| 生成段存在且可定位 | 手改过的文件可能连标记都丢了，此时不许猜 |
| 层级语义含"可救济度" | 降级为"处罚严重度"会让时效届满、标的物灭失两类漏掉（§1.1） |
| 两型都在（`active` + `passive`） | 只实现一型就是漏防的根源：把闸门当"拦截器"做，不作为型全数漏防，而实验数字上看不出来 |
| 插件的判据段引用了两型常量 | 逻辑若只判一型，表里有两型也没用 |
| 时间增广的守卫存在 | 不作为型缺 `deadlineDays` 时必须报"无法判定"而非静默放行 |

## 用法

    python3 code/tools/check_plugin_data.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
CODE_ROOT = HERE.parents[1]
PLUGIN_JS = CODE_ROOT / "plugins" / "acte-shield.js"
PLUGIN_DIR = CODE_ROOT / "plugins"
PLAN_JS = PLUGIN_DIR / "acte-plan.js"
SNAPSHOT = CODE_ROOT / "data" / "t-irr.json"
GEN_TOOL = CODE_ROOT / "tools" / "gen_plugin_shield.py"

# ── 插件源码里禁止出现的写法（每条都对应一次真实事故，不是风格偏好）──
BANNED_PATTERNS = (
    ("ctx.config",
     "读 `ctx.config` 会让插件在**真机加载期崩溃**：Cordis 的 ctx 是带守卫的 Proxy，"
     "未 inject 的服务属性一读就抛（实测报错 `cannot get property \"config\" without inject`，"
     "2026-09-19 F1 首次跑会话时暴露）。**行配置的正路是 `apply(ctx, config)` 的第二个参数**——"
     "而静态测试喂的是没有守卫的假 ctx，所以这一类错误在测试里看不出来。"
     "⚠ 本检查是**文本级**的：要在注释里讲这条坑，请写成「从 ctx 上读 config 属性」，"
     "别把字面量写进去（写了会被这条守卫拦下——它拦过作者自己一次）。"),
)


def check_plugin_sources():
    """插件源码的静态核查（不跑 JS，只查"曾经真的出错"的那些写法）。

    与 `check_logic` 的分工：`check_logic` 查盾的判据（两型、时间增广、委托），
    本函数查所有插件共同的两条纪律：

    1. 不得出现 `BANNED_PATTERNS` 里的写法；
    2. 每个插件的 `apply` 必须声明两个参数（`ctx, config`）：行配置只能从
       第二个参数进来，签名退化成一个参数时，配置就再也读不到，而消融会
       表现为"跑了、效应没变"（原因看不出来）。
    """
    problems = []
    files = sorted(PLUGIN_DIR.glob("acte-*.js"))
    if not files:
        return ["插件目录下没有 acte-*.js —— 目录布局变了，静态核查会静默失效"]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for token, why in BANNED_PATTERNS:
            if token in text:
                problems.append("%s 出现禁用写法 `%s`：%s" % (path.name, token, why))
        if "export function apply" in text and "apply(ctx, config)" not in text:
            problems.append(
                "%s 的 apply 未声明两个参数（应为 `apply(ctx, config)`）——"
                "行配置（消融 overlay 的开关）只能从第二个参数进来，"
                "签名退化会让配置读不到而**消融看起来无效**" % path.name)
    return problems


def check_plan_plugin():
    """发射端的存在性与契约（它缺失时，会话照常跑完，所以必须静态钉住）。"""
    problems = []
    if not PLAN_JS.is_file():
        return ["发射端插件不存在：%s\n"
                "    没有它，盾收不到任何 submission，裁决恒为空，"
                "而症状与『本案无风险』完全同形（『层是哑的』同类失效）"
                % PLAN_JS]
    text = PLAN_JS.read_text(encoding="utf-8")
    if "acte/shield" not in text:
        problems.append("发射端未出现 `acte/shield` —— 它必须在**这个事件名**上发射，"
                        "与盾的监听名一致，否则盾永远收不到提交")
    if "inject: ['tools']" not in text:
        problems.append("发射端未声明 `inject: ['tools']` —— 注册工具需要 tools 服务，"
                        "缺声明则 `ctx.tools` 一读就抛（Cordis 守卫）")
    if "waterfall(" not in text:
        problems.append("发射端未调用 `waterfall(` —— 事件不是自带广播，必须显式发射")
    return problems


def _load(path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("f1_gen_plugin_shield_%d" % id(path),
                                                  str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_logic(text):
    """静态核查插件的判据段（不跑 JS，只查关键构造是否在场）。"""
    problems = []
    # 两型都必须在判据里被处理
    if "KIND_ACTIVE" not in text or "KIND_PASSIVE" not in text:
        problems.append(
            "判据段未同时引用 KIND_ACTIVE 与 KIND_PASSIVE——"
            "只处理一型会漏防（不作为型尤其：把闸门当拦截器做，它全数漏防）")
    # 时间增广的守卫
    if "irreversible_guard_no_clock" not in text:
        problems.append(
            "缺少 `irreversible_guard_no_clock`：不作为型拿到缺期限的状态时"
            "必须报『无法判定』，不得静默放行（§1.1.1 的时间增广要求）")
    if "deadlineDays" not in text:
        problems.append("判据段未读取 `deadlineDays`——时间增广分量没有落实")
    # 未登记条目不许静默忽略
    if "unregistered_irreversible" not in text:
        problems.append(
            "缺少 `unregistered_irreversible`：触发了未登记的 T_irr 条目时"
            "必须报出，静默忽略等于漏防")
    # 委托下游（waterfall 规则）
    if "next(" not in text and "next()" not in text:
        problems.append("未委托 `next()`——waterfall 规则要求只做标注的监听者必须委托")
    # 不得在闸门里读姿态（定理 1(a)：盾不依赖策略/效用）
    for banned in ("posture", "姿态"):
        if banned in text.split("GENERATED:T_IRR")[-1] and "不看姿态" not in text:
            problems.append(
                "判据段出现 %r —— 盾**不得**依赖姿态或效用（定理 1(a)："
                "盾只依赖 ⟨S,A,P,T_irr⟩）。姿态差异应由策略层承担。" % banned)
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plugin", default=str(PLUGIN_JS))
    ap.add_argument("--snapshot", default=str(SNAPSHOT))
    args = ap.parse_args(argv)

    plug_path, snap_path = Path(args.plugin), Path(args.snapshot)

    print("=" * 70)
    print("插件数据一致性校验：%s" % plug_path.name)
    print("=" * 70)

    if not snap_path.is_file():
        sys.stderr.write("快照不存在：%s\n  先跑 derive_t_irr.py\n" % snap_path)
        return 1
    if not plug_path.is_file():
        sys.stderr.write("插件不存在：%s\n" % plug_path)
        return 1

    snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
    plugin_text = plug_path.read_text(encoding="utf-8")

    problems = []

    # ── 一、生成段是否与快照一致（用生成器自己的渲染函数，保证定义唯一）──
    gen = _load(GEN_TOOL)
    try:
        desired = gen.render_block(snapshot)
        head, current, tail = gen.split_file(plugin_text)
    except gen.GenError as exc:
        sys.stderr.write("\n%s\n" % exc)
        return 1

    if current.strip() != desired.strip():
        cur_lines = current.strip().splitlines()
        des_lines = desired.strip().splitlines()
        detail = ""
        for idx in range(max(len(cur_lines), len(des_lines))):
            a = cur_lines[idx] if idx < len(cur_lines) else "<缺>"
            b = des_lines[idx] if idx < len(des_lines) else "<多>"
            if a != b:
                detail = ("\n    首个差异在第 %d 行：\n      文件：%s\n      应为：%s"
                          % (idx + 1, a, b))
                break
        problems.append(
            "GENERATED 段与快照不一致（快照 %d 行 / 文件 %d 行）%s\n"
            "    修法：python3 code/tools/gen_plugin_shield.py"
            % (len(des_lines), len(cur_lines), detail))
    else:
        print("生成段与快照一致（逐字）")

    # ── 二、层级语义 ──
    semantics = snapshot.get("level_semantics") or ""
    if "可救济度" not in semantics:
        problems.append("快照 level_semantics 未体现『可救济度』：%r" % semantics)
    if "可救济度" not in (desired.split("LEVEL_SEMANTICS = ")[-1]):
        problems.append("生成段里的 LEVEL_SEMANTICS 未体现『可救济度』")

    # ── 三、两型都在 ──
    items = snapshot.get("t_irr") or []
    kinds = {e.get("kind") for e in items}
    if "active" not in kinds:
        problems.append("T_irr 里没有动作型（active）条目")
    if "passive" not in kinds:
        problems.append(
            "T_irr 里没有不作为型（passive）条目——若闸门只实现为『拦截器』，"
            "不作为型风险会**全数漏防**，而实验数字上看不出来"
            "（漏防与『本就无需触发』不可区分）。见 formalization.md §1.1.1")

    # ── 四、判据段的静态核查 ──
    problems.extend(check_logic(plugin_text))

    # ── 五、插件源码的通用纪律（禁用写法 / apply 的两参签名）──
    problems.extend(check_plugin_sources())

    # ── 六、发射端的存在性与契约 ──
    problems.extend(check_plan_plugin())

    if problems:
        print("\n失败 %d 项：\n" % len(problems))
        for p in problems:
            print("  ✗ %s" % p)
        return 1

    n_active = sum(1 for e in items if e["kind"] == "active")
    print("判据段静态核查通过（两型均处理 / 时间增广守卫在场 / 未登记条目报出 / 委托下游）")
    print("  盾不看姿态（定理 1(a)）—— 判据段未出现姿态依赖")
    print("  插件源码纪律通过（无 `ctx.config` / apply 均为两参签名）")
    print("  发射端契约通过（发射 acte/shield / inject tools / 调 waterfall）")
    print("  T_irr %d 条（动作型 %d / 不作为型 %d）"
          % (len(items), n_active, len(items) - n_active))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())