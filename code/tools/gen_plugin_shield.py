#!/usr/bin/env python3
"""生成插件内联的 `T_irr` 表（把权威数据 → 运行时副本变成生成而非手抄）。

## 为什么是"生成"而不是"内联 + 定期比对"

DSH 的运行时插件从 profile 的 `node_modules/` 按包名加载。为保持零依赖，
插件必须把 `T_irr` 内联在 JS 里。这就产生"同一份规则两处载体"的问题。

内联 + 定期比对的方案有个致命弱点：副本过期在运行时完全看不出来，
插件照常加载、照常挂载、`announce` 照常打印"层已启用"，只是它拦的规则是旧的。
"插件看着装上了、其实层是哑的"这一类失效在本项目里出现过，故改用生成：

    code/data/t-irr.json  ──[gen_plugin_shield.py]──▶  acte-shield.js 的 GENERATED 段

生成让漂移在结构上不可能发生，且校验退化成一次纯文本比对
（`check_plugin_data.py`，不需要 node、不需要子进程）。

## 生成段的位置

`acte-shield.js` 里以

    // >>> GENERATED:T_IRR
    …
    // <<< GENERATED:T_IRR

标出。本工具只替换这两行之间的内容，其余部分（判定逻辑、注释）一律不动，
故插件的手写部分可以正常维护，不会被生成覆盖。

## 用法

    python3 code/tools/gen_plugin_shield.py              # 就地更新
    python3 code/tools/gen_plugin_shield.py --check      # 只校验，不写盘（返回码非零即过期）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
CODE_ROOT = HERE.parents[1]
PLUGIN_JS = CODE_ROOT / "plugins" / "acte-shield.js"
SNAPSHOT = CODE_ROOT / "data" / "t-irr.json"

BEGIN_MARK = "// >>> GENERATED:T_IRR"
BEGIN_LINE = BEGIN_MARK + "，由 code/tools/gen_plugin_shield.py 生成，勿手改"
END = "// <<< GENERATED:T_IRR"


class GenError(RuntimeError):
    pass


def js_string(value):
    """把 Python 字符串渲染成单引号 JS 字符串字面量。

    只做必要的转义（反斜杠、单引号、换行）。刻意不用 `json.dumps`
    再换引号，那会产出双引号字面量，与本文件其余部分的风格不一致，
    也会让同一份数据的两次渲染不可逐字比较。
    """
    if value is None:
        return "null"
    s = str(value)
    out = s.replace("\\", "\\\\").replace("'", "\\'")
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return "'%s'" % out


def render_block(snapshot):
    """由快照渲染 GENERATED 段（确定性输出：同输入必同文本）。"""
    items = snapshot.get("t_irr") or []
    if not items:
        raise GenError("快照里没有 T_irr 条目——不生成空表（空表等于盾不拦任何动作）")
    semantics = snapshot.get("level_semantics") or ""
    if "可救济度" not in semantics:
        raise GenError(
            "快照的 level_semantics 未体现『可救济度』：%r\n"
            "  层级若降级为『处罚严重度』，时效届满与标的物灭失两类会被漏掉"
            % semantics)

    lines = [BEGIN_LINE, "export const T_IRR = ["]
    for e in items:
        lines.append("  {")
        lines.append("    gateId: %s," % js_string(e["gate_id"]))
        lines.append("    kind: %s," % js_string(e["kind"]))
        lines.append("    action: %s," % js_string(e.get("action")))
        lines.append("    levelChange: %s," % js_string(e.get("level_change")))
        lines.append("    ruleRef: %s," % js_string(e.get("rule_ref")))
        lines.append("    sourcePage: %s," % js_string(e.get("source_page")))
        lines.append("  },")
    lines.append("]")
    lines.append("")
    lines.append("export const LEVEL_SEMANTICS = %s" % js_string(semantics))
    lines.append(END)
    return "\n".join(lines)


def split_file(text):
    """把文件切成 (前, 生成段, 后)。缺标记即报错，不猜测插入位置。

    定位用 `BEGIN_MARK`（标记前缀）而不是整行 `BEGIN_LINE`：
    整行含说明文字，若哪天说明被改（或本工具的说明文字变了），
    按整行找就会找不到 ， 而"找不到"会被误读成"没标记过、该手工插"。
    按前缀找则对说明文字免疫。
    """
    i = text.find(BEGIN_MARK)
    j = text.find(END)
    if i < 0 or j < 0 or j < i:
        raise GenError(
            "在 %s 里找不到 GENERATED 标记（%r / %r）。\n"
            "  不猜测插入位置：手工插入的生成段无法保证可重复，"
            "下次生成会与手写部分错位。" % (PLUGIN_JS.name, BEGIN_MARK, END))
    head = text[:i]
    tail = text[j + len(END):]
    return head, text[i:j + len(END)], tail


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", default=str(SNAPSHOT))
    ap.add_argument("--plugin", default=str(PLUGIN_JS))
    ap.add_argument("--check", action="store_true", help="只校验，不写盘")
    args = ap.parse_args(argv)

    snap_path = Path(args.snapshot)
    plug_path = Path(args.plugin)
    if not snap_path.is_file():
        sys.stderr.write("快照不存在：%s\n  先跑 derive_t_irr.py\n" % snap_path)
        return 1
    if not plug_path.is_file():
        sys.stderr.write("插件不存在：%s\n" % plug_path)
        return 1

    snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
    try:
        desired = render_block(snapshot)
        head, current, tail = split_file(plug_path.read_text(encoding="utf-8"))
    except GenError as exc:
        sys.stderr.write("\n%s\n" % exc)
        return 1

    # 逐行比对，便于报出"哪一行过期"而不是只说"不一致"
    if current.strip() == desired.strip():
        n_active = sum(1 for e in snapshot["t_irr"] if e["kind"] == "active")
        n_passive = len(snapshot["t_irr"]) - n_active
        print("无漂移：%s 的 GENERATED 段与快照一致" % plug_path.name)
        print("  T_irr %d 条（动作型 %d / 不作为型 %d）"
              % (len(snapshot["t_irr"]), n_active, n_passive))
        return 0

    if args.check:
        cur_lines = current.strip().splitlines()
        des_lines = desired.strip().splitlines()
        print("过期：%s 的 GENERATED 段与快照不一致（快照 %d 行 / 文件 %d 行）"
              % (plug_path.name, len(des_lines), len(cur_lines)))
        for idx in range(max(len(cur_lines), len(des_lines))):
            a = cur_lines[idx] if idx < len(cur_lines) else "<缺>"
            b = des_lines[idx] if idx < len(des_lines) else "<多>"
            if a != b:
                print("  首个差异在第 %d 行：" % (idx + 1))
                print("    文件：%s" % a)
                print("    应为：%s" % b)
                break
        print("\n  修法：python3 code/tools/gen_plugin_shield.py")
        return 1

    plug_path.write_text(head + desired + tail, encoding="utf-8")
    n_active = sum(1 for e in snapshot["t_irr"] if e["kind"] == "active")
    n_passive = len(snapshot["t_irr"]) - n_active
    print("已更新 %s 的 GENERATED 段" % plug_path.name)
    print("  T_irr %d 条（动作型 %d / 不作为型 %d）"
          % (len(snapshot["t_irr"]), n_active, n_passive))
    print("  层级语义：%s" % snapshot["level_semantics"][:40])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())