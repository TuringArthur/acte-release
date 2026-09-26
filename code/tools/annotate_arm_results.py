#!/usr/bin/env python3
"""给 harness 落盘的结果 JSON 补记"profile 层"的溯源（不改任何读数）。

## 为什么需要这一步

`harness.py` 的 `settings` 字段只记录它自己的开关
（`posture_override` / `shield_enabled` / `include_reversible`）。
而本篇真实系统臂的消融是在 profile 层做的，消融配置
把插件行 `disabled: true`（这就是"消融 = 一行配置 diff"的落点）。

于是 `acte-model-noshield.json` 里会出现 `shield_enabled: true` 而实际上盾没挂，
只读结果文件的人会被误导。本工具把那层事实补记进去：

- `environment.profile_ablation`：本轮叠加的消融 overlay 名（没有则 `null`）；
- `environment.arm_run_dir`：会话产物目录（那才是"评的是哪一批会话"的答案）；
- `environment.provenance_note`：一句话说明 `settings` 字段的边界。

## 纪律

- 只增字段，不改任何读数：本工具拒绝修改 `systems` 段，读数只能由 harness 生成。
- 幂等：重复运行结果相同。

## 用法

    python3 code/tools/annotate_arm_results.py \\
        --results 实验结果目录 实验结果目录 experiment/results/（随实验材料发布）（随实验材料发布）acte-model-noshield.json \\
        --ablation no-shield --run-dir experiment/run/acte-sessions-noshield
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
PAPER_ROOT = HERE.parents[2]

NOTE = (
    "本字段由 code/tools/annotate_arm_results.py 补记：harness 的 settings 只反映"
    "**它自己的**开关；本篇真实系统臂的消融在 profile 层"
    "（experiment/configs/ablations/*.yml 把插件行 disabled）。"
    "判断本轮有没有挂盾，请看 profile_ablation，不要看 settings.shield_enabled。"
)


def annotate(results_path: Path, ablation, run_dir):
    data = json.loads(results_path.read_text(encoding="utf-8"))
    if "systems" not in data or "environment" not in data:
        raise SystemExit("不是 harness 的结果文件（缺 systems / environment 段）：%s" % results_path)
    env = data["environment"]
    before = json.dumps({k: env.get(k) for k in ("profile_ablation", "arm_run_dir")},
                        ensure_ascii=False)
    env["profile_ablation"] = ablation
    env["arm_run_dir"] = str(run_dir) if run_dir else None
    env["provenance_note"] = NOTE
    after = json.dumps({k: env.get(k) for k in ("profile_ablation", "arm_run_dir")},
                       ensure_ascii=False)
    results_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    print("补记完成：%s" % results_path)
    print("  profile_ablation / arm_run_dir：%s → %s" % (before, after))
    print("  读数未改动（systems 段逐字保留）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True, help="harness 落盘的结果 JSON")
    ap.add_argument("--ablation", default=None,
                    help="本轮叠加的消融 overlay 名（不带 .yml）；不给表示没有消融")
    ap.add_argument("--run-dir", default=None, help="会话产物目录（写成相对篇目录的路径更好读）")
    args = ap.parse_args(argv)
    return annotate(Path(args.results), args.ablation, args.run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
