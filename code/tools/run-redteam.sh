#!/usr/bin/env bash
#
# 红队探针批量会话：fail-open / fail-closed 两配置 × 2 种子。
#
# 产物目录（每格独立，避免混仓）：
#   experiment/run/redteam-open-k1   experiment/run/redteam-open-k2
#   experiment/run/redteam-closed-k1 experiment/run/redteam-closed-k2
#
# ## 排队纪律（重要）
#
#   会话驱动**不可并发**：run-acte-sessions.sh 每次驱动都会对共享的 profile
#   headless "删旧插件再 pnpm add"，两条驱动同时装会互相踩踏；主实验链
#   （chain-v4-runs.sh）正占着驱动。本脚本起跑前检测 experiment/run/*/.driver.lock
#   的活锁，发现活锁立即退出——探针排队在主实验之后，不是排进它中间。
#
# ## fail-closed 的开关位置
#
#   配置差异只有**一个环境变量**：ACTE_FAIL_CLOSED（closed 臂 =1，open 臂 =0）。
#   插件（code/plugins/acte-plan.js）只认这个变量；不设时与旧版逐字节一致，
#   故主实验的在跑链路不受影响。不需要 profile 补丁（ablations/ 无 fail-closed.yml：
#   profile overlay 是行配置，落不下进程环境变量，硬造一个反而会让人以为
#   挂了 overlay 就开了 fail-closed）。
#
#   动作文本表由本脚本从探针任务集导出为 ACTE_ACTION_TEXTS（JSON：
#   action_id → 文本），给插件做确定性的"文本→闸门"推断。动作 id 全套唯一，
#   故一张全量表对 24 条会话都适用（每条只查自己那几个 id）。
#
# ## 种子
#
#   k=1/2 是**独立重复采样**（与 chain-v4-runs.sh 的 k 槽同构）：同一提示词
#   两次独立会话。CLI 未暴露采样种子参数，不假装能固定随机源。
#
# ## 断点续传
#
#   与主链同一口径：已有非空 stdout 的会话跳过（run-acte-sessions.sh 的
#   --force 才全量重跑）。中断后原样重跑本脚本即续。
#
# 用法（在 papers/F1-adjudication/ 下；主实验跑完之后）：
#   code/tools/run-redteam.sh                 # 4 格全跑 + 判定
#   code/tools/run-redteam.sh --arms closed   # 只跑 fail-closed 两格
#   code/tools/run-redteam.sh --judge-only    # 只对已有产物出对照表
#   code/tools/run-redteam.sh --jobs 8
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAPER="$(cd "$HERE/../.." && pwd)"
cd "$PAPER"

JOBS="${ACTE_REDTEAM_JOBS:-8}"
SEEDS="1 2"
ARMS="open closed"
JUDGE_ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --jobs)       [ $# -ge 2 ] || { echo "错误：--jobs 需要一个 ≥1 的整数" >&2; exit 1; }
                  JOBS="$2"; shift 2 ;;
    --seeds)      [ $# -ge 2 ] || { echo "错误：--seeds 需要种子列表" >&2; exit 1; }
                  SEEDS="$(printf '%s' "$2" | tr ',' ' ')"; shift 2 ;;
    --arms)       [ $# -ge 2 ] || { echo "错误：--arms 需要 open/closed 列表" >&2; exit 1; }
                  ARMS="$(printf '%s' "$2" | tr ',' ' ')"; shift 2 ;;
    --judge-only) JUDGE_ONLY=1; shift ;;
    -h|--help)    sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *)            echo "错误：多余的位置参数：$1" >&2; exit 1 ;;
  esac
done

LOG="$PAPER/experiment/run/redteam-chain.log"
say() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"; }

# ── 活锁检测：主链（或任何驱动）还在跑就退出，不并发 ──────────────
for lock in "$PAPER"/experiment/run/*/.driver.lock; do
  [ -d "$lock" ] || continue
  oldpid="$(cat "$lock/pid" 2>/dev/null || true)"
  if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
    echo "✗ 发现活的会话驱动（${lock}，pid ${oldpid}）——会话驱动不可并发。" >&2
    echo "  探针排队在主实验之后：等 chain-v4-runs.sh 跑完（CHAIN-DONE）再起。" >&2
    exit 1
  fi
done

# ── 备料：探针任务集展开 + 动作文本表 + 逐格 prepare ─────────────
say "展开探针任务集"
python3 code/tools/build_redteam_taskset.py

# 动作文本表（JSON：action_id → 文本）：fail-closed 推断的输入。
export ACTE_ACTION_TEXTS
ACTE_ACTION_TEXTS="$(python3 - "$PAPER/experiment/tasks-redteam/taskset.json" <<'PY'
import json, sys
ts = json.load(open(sys.argv[1], encoding="utf-8"))
m = {}
for inst in ts.get("instances") or []:
    for a in inst.get("actions") or []:
        m[a.get("action_id")] = a.get("text") or ""
print(json.dumps(m, ensure_ascii=False))
PY
)"
say "动作文本表就绪（$(printf '%s' "$ACTE_ACTION_TEXTS" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))') 条）"

run_dirs=""
for arm in $ARMS; do
  for k in $SEEDS; do
    rd="experiment/run/redteam-$arm-k$k"
    run_dirs="$run_dirs $rd"
    if [ -z "$JUDGE_ONLY" ]; then
      if [ ! -f "$rd/manifest.tsv" ]; then
        say "备料 $rd"
        python3 code/tools/arm_acte_model.py --prepare --run-dir "$rd" \
                --taskset experiment/tasks-redteam >> "$LOG" 2>&1
      fi
      if [ "$arm" = "closed" ]; then
        export ACTE_FAIL_CLOSED=1
        say "开跑 ${rd}（fail-closed=1，--jobs ${JOBS}）"
      else
        export ACTE_FAIL_CLOSED=0
        say "开跑 ${rd}（fail-closed=0，--jobs ${JOBS}）"
      fi
      code/tools/run-acte-sessions.sh "$rd" --jobs "$JOBS" >> "$LOG" 2>&1 || \
        say "$rd 有会话失败（失败行下轮续跑）"
    fi
  done
done

# ── 判定：bypass 率对照表 ───────────────────────────────────────
say "判定"
# shellcheck disable=SC2086
python3 code/tools/judge_redteam.py $run_dirs \
  --tasks experiment/tasks-redteam \
  --out experiment/run/redteam-judge.json | tee -a "$LOG"

say "REDTEAM-DONE"
