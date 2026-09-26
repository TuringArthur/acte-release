#!/usr/bin/env python3
"""红队对抗循环的提示词层回归：有/无红队段必须可切换且只差一段。

## 为什么必须钉住提示词

`no-attack-loop` 消融不是插件 diff，而是备料时的提示词 diff。
若开关失效（两臂提示词意外相同，或红队段被误删），红队开关 的对照会变成
"同一臂 vs 同一臂"，读数相等看起来像"红队无效应"，与
"插件看着装上了其实哑了"同型：不报错，看起来一切正常。

故本文件测三件事：

1. 主臂提示词含红队段（`【红队对抗循环` 在场，且要求 Attacks[]）；
2. 消融提示词不含红队段，但去掉红队段后与主臂逐字相同（只差那一段）；
3. 干净件与弱点件都遵守同一开关（漏一边会让误报对照失真）。

    python3 code/tests/test_attack_loop_prompt.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
PAPER_ROOT = HERE.parents[2]
CODE_ROOT = PAPER_ROOT / "code"

ARM_PATH = CODE_ROOT / "tools" / "arm_acte_model.py"
MARKER = "【红队对抗循环"
ATTACKS = "Attacks[]"


class Case:
    def __init__(self):
        self.n, self.failed = 0, []

    def check(self, name, cond, detail=""):
        self.n += 1
        if cond:
            print("  ok   %s" % name)
        else:
            print("  FAIL %s%s" % (name, ("  — " + detail) if detail else ""))
            self.failed.append(name)


def _load_arm():
    spec = importlib.util.spec_from_file_location("f1_arm_attack_loop", str(ARM_PATH))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CODE_ROOT))
    try:
        spec.loader.exec_module(mod)
    finally:
        if sys.path and sys.path[0] == str(CODE_ROOT):
            sys.path.pop(0)
    return mod


def _strip_red_team(text, arm):
    """从提示词里去掉红队段（拼接时带前导 \\n）。"""
    step = arm.RED_TEAM_STEP
    if "\n" + step in text:
        return text.replace("\n" + step, "", 1)
    return text.replace(step, "", 1)


def main():
    print("=" * 70)
    print("红队对抗循环：提示词开关")
    print("=" * 70)
    c = Case()
    arm = _load_arm()

    weak_payload = {
        "arm": "acte-model", "task_id": "F1-INJ-CONC-001",
        "facts_text": "案件事实：……与被告丙连带赔偿……（夹具案情，仅供提示词结构）",
        "posture": "细",
        "actions": [{"action_id": "a1", "text": "起诉", "covers": ["e1"],
                     "expected_utility": 1, "cost": 1, "triggers": []}],
    }
    clean_payload = {
        "arm": "acte-model", "task_id": "F1-CIV-CONC-001",
        "facts_text": "案件事实：……（干净件夹具）",
        "is_clean": True,
    }

    print("\n[A] 开关：主臂含红队段、消融不含")
    p_on = arm.build_prompt(weak_payload, with_plan=True, attack_loop=True)
    p_off = arm.build_prompt(weak_payload, with_plan=True, attack_loop=False)
    c.check("attack_loop=True ⇒ 含红队标题", MARKER in p_on)
    c.check("attack_loop=True ⇒ 含 Attacks[] 要求", ATTACKS in p_on)
    c.check("attack_loop=False ⇒ 不含红队标题", MARKER not in p_off)

    print("\n[B] 只差红队段：其余逐字相同（防顺手改了别的问法）")
    stripped = _strip_red_team(p_on, arm)
    c.check("★ 从主臂提示词去掉红队段 ⇒ 与消融提示词逐字相同",
            stripped == p_off,
            "去段后仍有差异（长度 on=%d off=%d stripped=%d）"
            % (len(p_on), len(p_off), len(stripped)))

    print("\n[C] 干净件同样遵守开关（漏一边会让误报对照失真）")
    c_on = arm.build_prompt(clean_payload, with_plan=False, attack_loop=True)
    c_off = arm.build_prompt(clean_payload, with_plan=False, attack_loop=False)
    c.check("干净件 attack_loop=True ⇒ 含红队段", MARKER in c_on)
    c.check("干净件 attack_loop=False ⇒ 不含红队段", MARKER not in c_off)
    c_stripped = _strip_red_team(c_on, arm)
    c.check("干净件去红队段后与 off 逐字相同", c_stripped == c_off,
            "len on=%d off=%d stripped=%d" % (len(c_on), len(c_off), len(c_stripped)))

    print("\n[D] 计划段与红队段的先后：红队在计划之前（方法顺序）")
    c.check("弱点件：红队段出现在 acte_plan 之前",
            p_on.find(MARKER) < p_on.find("acte_plan"),
            "marker@%s plan@%s" % (p_on.find(MARKER), p_on.find("acte_plan")))

    print("\n[E] 决策级套件不出干净行（weak 行与 clean 行同 id ⇒ 并行会互相覆盖）")
    import tempfile
    dec_tasks = PAPER_ROOT / "experiment" / "tasks-decision"
    dec_ts = dec_tasks / "taskset.json"
    if dec_ts.is_file():
        with tempfile.TemporaryDirectory() as td:
            arm.prepare(Path(td), dec_ts, attack_loop=True)
            kinds = [line.split("\t")[0]
                     for line in (Path(td) / "manifest.tsv").read_text(
                         encoding="utf-8").splitlines() if line.strip()]
            c.check("决策任务集 manifest 只含 weak 行", kinds and set(kinds) == {"weak"},
                    "kinds=%s" % kinds)
    else:
        # [A]–[D] 四组只依赖适配器模块，不依赖实验材料，本仓库里照常执行。
        # [E] 组以决策任务集为输入，算法本体导出的仓库没有它，按「不适用」记，
        # 不计入失败（见 README 第 5、8 节）。
        print("  —— 不适用：缺决策任务集（%s），[E] 组跳过。" % dec_ts)
        print("     本组以实验材料为输入；取得材料后重跑即可。")

    print("\n" + "=" * 70)
    if c.failed:
        print("失败 %d / %d 项：" % (len(c.failed), c.n))
        for f in c.failed:
            print("  - %s" % f)
        return 1
    print("全部通过（%d 项断言）" % c.n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
