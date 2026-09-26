#!/usr/bin/env bash
#
# 真实系统臂的会话驱动：按 manifest 逐条跑 headless 会话。
#
# 为什么驱动放在 shell 而不是 Python：见 `code/tools/arm_acte_model.py` 的模块说明，
# 本仓库把子进程面收敛到单一入口（静态安全审查把"由参数或变量推出来的 argv 进
# subprocess"判为命令注入）。
#
# 它做三件事，每件都有对应的一处纪律：
#   1. 一次实验会话 = 一个 DSH_HOME（隔离）；
#   2. 按实例设置 ACTE_POSTURE（姿态由 overlay 参数决定，定理 1(a) 的落点）
#      与 ACTE_PLAN_JOURNAL（发射端把"模型的四项判断 + 最终动作集"写在这里）；
#   3. 逐条落 stdout / stderr：stdout 是模型的最终答复（findings 的取数口），
#      与基线同口径。
#
# 用法（在实验材料根目录下，任务集与 manifest 由 arm_acte_model.py --prepare 生成）：
#   code/tools/run-acte-sessions.sh experiment/run/acte-sessions [--jobs N]
#   code/tools/run-acte-sessions.sh experiment/run/acte-sessions --ablation no-shield
#   code/tools/run-acte-sessions.sh experiment/run/acte-sessions --only F1-INJ-CONC-001
#   code/tools/run-acte-sessions.sh experiment/run/acte-sessions --only A,B,C --kind weak
#   CTD_ENV_FILE=<凭据文件> code/tools/run-acte-sessions.sh <run_dir>
#       # 跨模型轮：换凭据/模型文件（默认 .env.local；同目录混模型会被守卫拦下）
#
# ## 断点续传
#
#   · 已有非空 `stdout/<id>.txt` 的会话一律跳过（`--force` 才全量重跑）：
#     中途断电后原命令再跑一遍即从断点继续；失败/半截的会话（没有正式
#     stdout）会自动重跑。
#   · 新会话的 stdout 先写 `<id>.txt.part`，成功后才改名成正式文件，
#     被杀的进程只会留下 `.part`，不会被下一轮误认成"已完成"（若直接重定向
#     到正式文件，跑到一半的 stdout 已非空 ⇒ 会被跳过 = 静默采用半截产物）。
#   · `--only` 接受逗号分隔的多个 task_id（pilot 只跑子集时用）。
#
# ## 并行
#
#   · `--jobs N`：清单行级并行。父进程先顺序筛清单（滤掉已完成/过滤行）
#     得到待跑集合，再经 `xargs -0 -n1 -P N` 逐行派发；每行回到本脚本的
#     `--run-one` 内部模式执行。用 xargs 而不是后台 job + wait：本机
#     /bin/bash 是 3.2（无 `wait -n`），macOS 自带 xargs 支持 -0/-P。
#     默认 `--jobs 1` = 与旧版逐行串行同形。
#   · 只在单个驱动内部并行，run 目录之间仍按序接力：每次驱动启动都要对
#     共享的 profile headless 做"删旧插件再 pnpm add"（见下），两个驱动
#     同时装会互相踩踏 ⇒ 目录间不并发；目录内每行的 stdout/stderr/journal
#     本就按 task_id 分文件，行间并行互不干扰（进度行可能交错，仅观感问题）。
#   · 并发数即节流：每条会话内部是串行多轮调用（单路约 2–6 RPM），N=8 ⇒ 约
#     16–48 RPM，留在 60 上限内；TPM 150 万远超用量。CLI 层未见 429 自动
#     重试 ⇒ 若 stderr 出现 429/limit，调低 --jobs（失败行下一轮照常重跑）。
#   · 每个 run 目录一把驱动锁（`.driver.lock/`，写 pid 做活性检测防陈锁）：
#     防止两条链同时跑同一目录把 `.part` 写花。
#   · 并行下自增计数器不可用（每个 worker 是独立进程）：worker 向
#     `.driver.lock/status` 追加 `ok/fail` 行，父进程据此出汇总。
#   · 单会话硬超时：
#     实测有会话流式输出中途卡死、进程永不退出（APPEAL-001 挂 40 分钟，
#     xargs 等它 ⇒ 整条链停摆）。macOS 无 GNU timeout ⇒ bash 看门狗：到点
#     TERM 掉 node（node 收 TERM 退 0 但 stdout 半截 ⇒ 照记失败，.part 下轮重跑）。
#     正常会话 1–6 分钟，900 秒是留足余量后的值，超时不是限速手段，
#     只为防"一个卡死会话废掉整轮并行"。
#
# 退非零表示有会话失败，失败不静默（空 stdout 会在取数阶段被当成"没跑成"，
# 那正是我们要能立刻看见的事）。
# ==== 用法结束 ====

set -euo pipefail

# 中文标点陷阱（本仓库脚本通用）：变量名后若紧跟中文标点，必须写成 ${VAR}，
# 否则非 UTF-8 locale 下 bash 会把多字节字符并入变量名，配合 set -u 直接报错。

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAPER="$(cd "$HERE/../.." && pwd)"                    # 篇目录
REPO="$(cd "$PAPER/../.." && pwd)"                    # 上位仓库根（默认凭据文件所在处）
CLI="${DSH_CLI:-}"
PATCH_FILE="$PAPER/experiment/configs/profile.patch.yml"
SELF="$HERE/$(basename "${BASH_SOURCE[0]}")"

die() { printf '错误：%s\n' "$*" >&2; exit 1; }

# 基座（DSH）CLI 的位置由环境变量 `DSH_CLI` 给出。基座是第三方依赖（MIT），
# 位置随安装方式而变（本地开发仓、独立仓库的 node_modules、全局安装各不相同），
# 故这里不猜默认路径：猜错会在跑到一半时才发现，而那时已经起了会话。
# 钉死版本 0.1.5-rc.2 / c291e7961a515f6d7af9304e7fd1d257929aef26。
if [ -z "$CLI" ]; then
  die "未设置 DSH_CLI。请用环境变量指向 DSH 的命令行入口，例如：
  DSH_CLI=<安装位置>/apps/cli/lib/bin.js $0 <run_dir>"
fi
[ -f "$CLI" ] || die "找不到 DSH CLI：${CLI}
  用环境变量 DSH_CLI 指向其入口（基座以依赖形式安装，位置随安装方式而变）。"

run_dir=""
row=""
run_one=""
ablation=""
only=""
kind=""
force=""
jobs=1
while [ $# -gt 0 ]; do
  case "$1" in
    --run-one)  [ $# -ge 3 ] || die "--run-one 需要 <run_dir> <清单行>（内部模式，勿手敲）"
                run_one=1; run_dir="$2"; row="$3"; shift 3 ;;
    --ablation) [ $# -ge 2 ] || die "--ablation 需要一个名字（见 experiment/configs/ablations/）"
                ablation="$2"; shift 2 ;;
    --only)     [ $# -ge 2 ] || die "--only 需要一个或多个 task_id（逗号分隔）"
                only="$2"; shift 2 ;;
    --kind)     [ $# -ge 2 ] || die "--kind 需要 weak 或 clean"
                kind="$2"; shift 2 ;;
    --jobs)     [ $# -ge 2 ] || die "--jobs 需要一个 ≥1 的整数"
                jobs="$2"; shift 2 ;;
    --force)    force="1"; shift ;;
    -h|--help)  sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *)          [ -z "$run_dir" ] || die "多余的位置参数：$1"
                run_dir="$1"; shift ;;
  esac
done

case "$jobs" in
  ''|*[!0-9]*) die "--jobs 只接受整数，收到：$jobs" ;;
esac
[ "$jobs" -ge 1 ] || die "--jobs 必须 ≥1，收到：$jobs"

[ -n "$run_dir" ] || die "用法：run-acte-sessions.sh <run_dir> [--jobs N] [--ablation <名>] [--only <id,id,…>] [--kind weak|clean] [--force]"
[ -f "$CLI" ] || die "基座未构建（缺 ${CLI}）。取回与构建见 papers/_shared/base-dsh.md §0。"
[ -f "$PATCH_FILE" ] || die "缺 profile overlay：${PATCH_FILE}"

# ── 凭据与隔离（与 tools/run-experiment.sh 同一套口径）──────────────
# CTD_ENV_FILE 可换凭据文件（跨模型稳健性轮专用：换模型/网关不动主配置
# .env.local）。默认仍是 .env.local，行为不变。export 是给 --run-one 的
# worker 进程继承用的（xargs 派发的 worker 会重新走这段）。
export CTD_ENV_FILE="${CTD_ENV_FILE:-$REPO/.env.local}"
if [ -f "$CTD_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$CTD_ENV_FILE"
  set +a
fi
export DSH_HOME="$PAPER/experiment/run"
[ -n "${DEEPSEEK_API_KEY:-}" ] || \
  printf ' DEEPSEEK_API_KEY 为空——会话会失败。填 %s 后重试。\n' "$CTD_ENV_FILE" >&2

# 消融 overlay 路径（两种模式都要先验，早失败早看见）。
abl_file=""
if [ -n "$ablation" ]; then
  abl_file="$PAPER/experiment/configs/ablations/$ablation.yml"
  [ -f "$abl_file" ] || die "找不到消融臂 '$ablation'：${abl_file}
  可用的臂见 experiment/configs/ablations/*.yml"
fi

# ══ worker 模式（--run-one）：父进程派发一行，这里执行一行 ═══════════
# 不装插件（父进程装过）、不读清单（行已随参数给全）、不碰目录锁（父进程持有）。
if [ -n "$run_one" ]; then
  status="$run_dir/.driver.lock/status"
  patches[0]="--patch"
  patches[1]="$PATCH_FILE"
  if [ -n "$abl_file" ]; then
    patches[${#patches[@]}]="--patch"
    patches[${#patches[@]}]="$abl_file"
  fi

  IFS=$'\t' read -r row_kind tid posture prompt_file out_file err_file journal_file row_deadline <<< "$row"
  if [ -z "${tid:-}" ] || [ -z "${prompt_file:-}" ] || [ -z "${journal_file:-}" ]; then
    printf '✗ 清单行残缺，不执行：%s\n' "$row" >&2
    printf 'fail %s rc=badrow\n' "${tid:-?}" >> "$status"
    exit 1
  fi
  # 清单里的 `-` 是"无该字段"的占位（空字段会被 IFS 折叠，见备料脚本的说明）。
  [ "$posture" = "-" ] && posture=""
  [ "${row_deadline:-}" = "-" ] && row_deadline=""

  # 双保险：父进程筛过已完成行，这里再查一次（防两条链撞目录，锁是第一道）。
  if [ "${force:-}" != "1" ] && [ -s "$out_file" ]; then
    exit 0
  fi

  export ACTE_POSTURE="$posture"
  export ACTE_PLAN_JOURNAL="$journal_file"
  export ACTE_STRUCTURED_DEADLINE_DAYS="${row_deadline:-}"
  : > "$journal_file"          # 每条会话一份新日志：混在一起就分不清是哪次提交

  printf '  → %s（%s%s）…\n' "$tid" "$row_kind" "${posture:+ / $posture}" >&2
  # `< /dev/null` 必须有：node 若继承 xargs 的 stdin（待跑集合文件），
  #   会把派发数据读走，与旧版"子进程吃掉 manifest"同族的坑。
  # stdout 先写 `.part`：被杀的进程只会留下 .part；成功才改名转正，
  #   断点续传的"已完成"判据必须只认完整产物（半截 stdout 非空会被误跳过）。
  # 看门狗硬超时（见头部「并行」节）：卡死的会话到点收 TERM，记失败下轮重跑；
  #   正常完成则先杀看门狗再 wait 回收，不残留 sleep 进程。
  # 超时用标记文件裁决，不信退出码：实测 node 收 TERM 后可能以 0 退出，
  #   而此时 .part 已有流式半截输出 ⇒ 只看 rc 会把它 mv 转正，断点续传随后
  #   永远跳过这条 = 静默采用坏数据。看门狗里 kill 成功才打标（node 已成
  #   僵尸/已退出则不打，ps 的 state=Z 排除僵尸让 kill 误成功）。
  session_timeout="${ACTE_SESSION_TIMEOUT:-900}"
  timeout_flag="$out_file.part.timeout"
  rm -f "$timeout_flag"
  node "$CLI" --profile headless ${patches[@]+"${patches[@]}"} \
       "$(cat "$prompt_file")" >"$out_file.part" 2>"$err_file" < /dev/null &
  node_pid=$!
  (
    sleep "$session_timeout"
    state="$(ps -o state= -p "$node_pid" 2>/dev/null | tr -d ' ')"
    if [ -n "$state" ] && [ "$state" != "Z" ]; then
      : > "$timeout_flag"
      kill -TERM "$node_pid" 2>/dev/null || true
    fi
  ) &
  watchdog_pid=$!
  rc=0
  wait "$node_pid" || rc=$?
  kill "$watchdog_pid" 2>/dev/null || true
  wait "$watchdog_pid" 2>/dev/null || true
  if [ -e "$timeout_flag" ]; then
    rm -f "$timeout_flag"
    printf '  ✗ %s：会话超时（%ss 看门狗）——记失败，.part 下轮重跑（绝不转正）\n' \
      "$tid" "$session_timeout" >&2
    printf 'fail %s rc=timeout\n' "$tid" >> "$status"
    exit 1
  fi
  if [ "$rc" -eq 0 ]; then
    # 退出码 0 不等于会话成功（给病态会话发 TERM 后
    #   node 以 0 退出，stdout 只有 1 字节，只看退出码会把它记成 ok，
    #   空产物要到取数阶段才炸，而那已经隔了一整轮）。空产物一律记失败。
    # 转正阈值 >200 字节而非"非空"：`-s` 只挡 0 字节，被看门狗截断的会话
    #   会留下 1 字节 .part（流式首字节后停滞）骗过转正（2026-09-27 实测 9 件）。
    #   正常产物 1.7KB 起。
    if [ "$(wc -c < "$out_file.part" | tr -d ' ')" -gt 200 ]; then
      mv -f "$out_file.part" "$out_file"      # 成功才转正
      printf '  ok %s：stdout %s 字节 / 提交日志 %s 行\n' \
        "$tid" \
        "$(wc -c < "$out_file" | tr -d ' ')" \
        "$(wc -l < "$journal_file" | tr -d ' ')" >&2
      printf 'ok %s\n' "$tid" >> "$status"
      exit 0
    fi
    printf '  ✗ %s：退出码 0 但 stdout 为空——会话被中断或静默失败（记失败，下轮重跑）\n' "$tid" >&2
    printf 'fail %s rc=0-empty\n' "$tid" >> "$status"
    exit 1
  else
    # rc 已在上面 wait node_pid 时取到，这里若再写 rc=$? 会把它覆盖成
    # 测试表达式的 1，日志与 status 就丢掉真实退出码了。
    printf '  ✗ %s：会话失败（退出码 %s）——stderr 尾部：\n' "$tid" "$rc" >&2
    tail -5 "$err_file" >&2 || true
    printf 'fail %s rc=%s\n' "$tid" "$rc" >> "$status"
    exit 1
  fi
fi

# ══ 父进程模式 ════════════════════════════════════════════════════════
[ -f "$run_dir/manifest.tsv" ] || die "找不到 $run_dir/manifest.tsv——先备料：
  python3 code/tools/arm_acte_model.py --prepare --run-dir $run_dir"

# ── 模型溯源快照 + 混模型守卫────────────
# 一个 run 目录只允许一个模型的产物：断点续传若跨模型复用目录，k 次重复
# 会悄悄变成两种模型的混合采样，且产物上完全看不出来。快照不含密钥。
model_now="${DSH_MODEL_ID:-}"
snapshot="$run_dir/run-env.model"
if [ -f "$snapshot" ]; then
  model_prev="$(sed -n 's/^model_id=//p' "$snapshot" | head -1)"
  [ "$model_prev" = "$model_now" ] || die "run 目录 $run_dir 的既有产物属于模型 '$model_prev'，当前是 '$model_now'——同一目录不得混模型，换模型请开新 run 目录"
else
  {
    printf 'model_id=%s\n' "$model_now"
    printf 'base_url_host=%s\n' "$(printf '%s' "${DEEPSEEK_BASE_URL:-}" | sed -E 's#^https?://([^/]+).*#\1#')"
    printf 'recorded_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$snapshot"
fi

# ── 目录锁：同一 run 目录只允许一个驱动（防两条链把 .part 写花）──────
lockdir="$run_dir/.driver.lock"
if ! mkdir "$lockdir" 2>/dev/null; then
  oldpid="$(cat "$lockdir/pid" 2>/dev/null || true)"
  if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
    die "目录 $run_dir 已有一个驱动在跑（pid $oldpid）——先停它再续跑"
  fi
  rm -rf "$lockdir"            # 陈锁（上轮被 kill -9 / 断电留下）
  mkdir "$lockdir" 2>/dev/null || die "抢锁失败：$lockdir"
fi
printf '%s' "$$" > "$lockdir/pid"
trap 'rm -rf "$lockdir"' EXIT
status="$lockdir/status"
todo="$lockdir/todo"
: > "$status"
: > "$todo"

# ── 刷新本篇插件包（ 每次都必须做，理由如下）────────────────────
# pnpm 对 `file:` 依赖是复制而非链接：源改动后不先删再装，profile 里留着的
# 还是上一版导出，且不报错。曾出现过（给发射端加了取数日志后
# 直接跑会话，盾与发射端都正常响应、会话正常结束，只有日志文件是空的，
# 看起来像"模型没提交计划"，其实是插件跑的是旧代码）。
# 包名从 package.json 读，不写死：各任务形态的包名不同。
# 这段是目录间不并发的根本原因：两个驱动同时对同一 profile 删装会互踩。
PLUGIN_DIR="$PAPER/code/plugins"
profile_dir="$DSH_HOME/profiles/headless"
if [ -f "$PLUGIN_DIR/package.json" ]; then
  [ -d "$profile_dir" ] || node "$CLI" --profile headless --help >/dev/null 2>&1 || true
  [ -d "$profile_dir" ] || die "未找到 profile 目录 ${profile_dir}（插件行会解析失败）"
  pkg_name="$(node -e "process.stdout.write(require(process.argv[1]).name)" \
                "$PLUGIN_DIR/package.json" 2>/dev/null || true)"
  (
    cd "$profile_dir"
    if [ -n "$pkg_name" ]; then
      rm -rf "node_modules/$pkg_name"
    else
      printf ' 读不到 %s 的包名，退化为清空整个 @ctd-la 作用域。\n' \
        "${PLUGIN_DIR}" >&2
      rm -rf node_modules/@ctd-la
    fi
    pnpm add --prefer-offline "file:$PLUGIN_DIR" >/dev/null 2>&1
  ) || die "插件包安装失败（${PLUGIN_DIR} → ${profile_dir}）。
  手动重试：cd $profile_dir && pnpm add file:$PLUGIN_DIR"
  printf 'plugin : %s → profile headless（已先删再装）\n' "${PLUGIN_DIR}" >&2
fi

[ -n "$ablation" ] && printf 'ablation: %s\n' "$ablation" >&2
export ACTE_ABLATION="$ablation" ACTE_FORCE="${force:-}"

# ── 阶段 1：顺序筛清单 → 待跑集合（NUL 分隔；完成行在此跳过）─────────
total=0            # 待派发
done_resume=0      # 断点续传跳过
skipped=0          # --only/--kind/残缺 跳过
while IFS= read -r line || [ -n "${line:-}" ]; do
  [ -n "$line" ] || continue
  IFS=$'\t' read -r row_kind tid posture prompt_file out_file err_file journal_file row_deadline <<< "$line"
  [ -n "${tid:-}" ] || continue
  # `--only` 接受逗号分隔的多个 id（pilot 只跑子集）。
  if [ -n "$only" ]; then
    case ",$only," in
      *,"$tid",*) ;;
      *) skipped=$((skipped + 1)); continue ;;
    esac
  fi
  if [ -n "$kind" ] && [ "$row_kind" != "$kind" ]; then
    skipped=$((skipped + 1))
    continue
  fi
  # 清单字段残缺就跳过并报出来（不带着空路径去跑，空路径会表现为"会话失败"，
  # 而真因是清单没读对）。曾出现过一次：见 worker 里 `< /dev/null` 的说明。
  if [ -z "${journal_file:-}" ] || [ -z "${prompt_file:-}" ]; then
    printf ' 清单行残缺，跳过：kind=%s tid=%s\n' "$row_kind" "$tid" >&2
    skipped=$((skipped + 1))
    continue
  fi
  # ── 断点续传：已有非空正式 stdout ⇒ 该会话已完成，跳过（--force 才重跑）──
  # 正式文件只在会话成功后由 .part 改名而来（见 worker），故它非空 = 完整产物。
  # 中途断电的会话只有 .part（或空正式文件），会落到待跑集合被重跑。
  if [ "$force" != "1" ] && [ -s "$out_file" ]; then
    done_resume=$((done_resume + 1))
    continue
  fi
  total=$((total + 1))
  printf '%s\0' "$line" >> "$todo"
done < "$run_dir/manifest.tsv"

# ── 阶段 2：xargs 并行派发（并发即节流，见头部「并行」一节）───────────
# ★ `--ablation` 必须传给 worker：worker 是重新解析 argv 的新进程，
#   只传 `--run-one` 时它拿不到消融 overlay ⇒ no-shield 臂会照常挂盾跑完
#   且不报错（2026-09-26 实测：v3/v4 的 noshield 会话因此全部无效，
#   后果与处置见 notes/F1-noshield消融失效发现-2026-09-26.md）。
xrc=0
if [ "$total" -gt 0 ]; then
  printf '派发 %d 行（--jobs %d）…\n' "$total" "$jobs" >&2
  # worker 退出码：0=ok，1=失败；xargs 收齐后若>0 说明有失败行（123/125 惯例）。
  if [ -n "$ablation" ]; then
    xargs -0 -n 1 -P "$jobs" "$SELF" --ablation "$ablation" --run-one "$run_dir" < "$todo" || xrc=$?
  else
    xargs -0 -n 1 -P "$jobs" "$SELF" --run-one "$run_dir" < "$todo" || xrc=$?
  fi
fi

n_ok="$(grep -c '^ok ' "$status" 2>/dev/null || true)"
n_fail="$(grep -c '^fail ' "$status" 2>/dev/null || true)"
n_ok="${n_ok:-0}"; n_fail="${n_fail:-0}"
# grep 对"0 匹配"退 1 且可能吞掉输出为空串，兜底成 0。
[ -n "$n_ok" ] || n_ok=0
[ -n "$n_fail" ] || n_fail=0

printf '\n会话完成：派发 %d 条（成功 %d，失败 %d），断点续传跳过 %d，按 --only/--kind 跳过 %d\n' \
  "$total" "$n_ok" "$n_fail" "$done_resume" "$skipped" >&2
if [ "$xrc" -ne 0 ]; then
  printf 'xargs 退出码 %d（>0 表示有 worker 失败或派发异常）\n' "$xrc" >&2
fi
printf '产物：%s\n' "$run_dir" >&2

# ── 消融生效自检（no-shield 特有）────────────────────────────────────
# 消融"看着关了、其实没关"与"本案确实无风险"在结果上同形，
# 唯一可靠的判据是 journal 的层清单：盾没挂上时发射端只记 plan-emitter。
# 不自检就取数，正是 v3/v4 noshield 臂带病跑完的路径。
if [ "$ablation" = "no-shield" ]; then
  if grep -ql 'irreversible-shield' "$run_dir"/journal/*.jsonl 2>/dev/null; then
    printf '✗ no-shield 消融自检失败：journal 里仍有 irreversible-shield 层裁决，\n' \
      '  说明盾没有被真正卸载（先查 worker 是否拿到了 --ablation）。本次读数作废。\n' >&2
    exit 1
  fi
  printf '消融自检：journal 无 irreversible-shield 层，no-shield 生效 ✓\n' >&2
fi

[ "$n_fail" -eq 0 ] && [ "$xrc" -eq 0 ] || exit 1
