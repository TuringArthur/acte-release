/**
 * 盾检查微基准：对 `code/plugins/acte-shield.js` 的 `check()` 跑 N 次（默认 10 万）计时。
 *
 * ## 为什么要有这个基准
 *
 * 护栏"判定近乎零负担/常数时间"这一主张需要实测数据。会话日志只记
 * 提交与裁决落在同一行（`at` 是裁决落盘时刻），提交→裁决的墙钟差在日志里
 * 结构上不可观测；盾的单次判定成本只能这样量：把 `check()` 从真实会话里
 * 拆出来，在 node 侧反复打点。会话侧的回合间隔另有指标（见
 * `code/tools/perf_report.py`），两者合起来才构成"零负担"主张的证据。
 *
 * ## 纪律
 *
 * - 只 `import` 插件，不改插件本体（`code/plugins/*` 是被测物）。
 * - 输出走 stdout 的单行 JSON，供 `perf_report.py --bench` 解析；
 *   人直接跑时也能看（JSON 自带可读字段）。
 * - 计时用 `process.hrtime.bigint()`；同时量"空打点"的计时器开销并一并报出，
 *   避免把 hrtime 的成本记到盾头上。净耗时只给估算值（逐次差分），不下结论。
 *
 * ## 用法
 *
 *     node code/tools/bench_shield.mjs            # 默认 100000 次/场景
 *     node code/tools/bench_shield.mjs 200000     # 指定次数
 *
 * 场景两个：
 *   - `hot`：触发 3 条 `T_IRR` 闸门（动作型 + 两条不作为型）+ 1 条未登记
 *     + 1 条可逆风险 + 时钟冲突 —— 覆盖所有 finding 分支的最重提交；
 *   - `cold`：零触发 —— 空跑下界。
 * 论文引用以 `hot` 为准（上界口径），`cold` 是对照。
 */

import { performance } from 'node:perf_hooks'
import { check } from '../plugins/acte-shield.js'

const N = (() => {
  const v = Number(process.argv[2] === undefined ? 100000 : process.argv[2])
  if (!Number.isFinite(v) || v < 1000) {
    process.stderr.write('用法：node bench_shield.mjs [次数≥1000]\n')
    process.exit(2)
  }
  return Math.floor(v)
})()

/** 覆盖全部 finding 分支的重提交：动作型未确认、不作为型到阈值、未登记、时钟冲突。 */
const HOT = {
  caseType: 'bench',
  triggered: [
    'gate.escalation-risk',          // 动作型 ⇒ require_confirmation
    'gate.suspension-of-execution',  // 不作为型，deadlineDays 10 ≤ 15 ⇒ 催办
    'gate.statute-of-limitations',   // 不作为型 ⇒ 催办
    'W-ESC-03',                      // 可逆风险（默认不拦，仅遍历）
    'bench.unregistered',            // 未登记 ⇒ warn_and_log
  ],
  confirmed: [],
  deadlineDays: 10,
  clockConflict: { structured: 10, declared: 180, used: 10 },
}

/** 零触发的空跑提交。 */
const COLD = { caseType: 'bench', triggered: [], confirmed: [], deadlineDays: 100 }

/** 空打点一次（只测 hrtime 对的开销）。 */
function tick() {
  return process.hrtime.bigint()
}

function quantile(sorted, p) {
  if (sorted.length === 0) return null
  const idx = Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1)
  return Number(sorted[Math.max(0, idx)])
}

/**
 * 跑一个场景：预热后逐次打点 N 次 `check()`，再整批计一次作交叉核对。
 *
 * @param {string} label - 场景名。
 * @param {object} submission - 喂给 check() 的提交。
 * @param {number} n - 迭代次数。
 * @returns {object} 结果（纳秒，附换算微秒字段）。
 */
function benchScenario(label, submission, n) {
  const warmup = 2000
  for (let i = 0; i < warmup; i += 1) check(submission)

  const perCallNs = new Array(n)
  const batchStart = process.hrtime.bigint()
  for (let i = 0; i < n; i += 1) {
    const t0 = process.hrtime.bigint()
    const findings = check(submission)
    const t1 = process.hrtime.bigint()
    // 消费掉返回值，防 JIT 把整段判成无副作用而优化掉。
    if (findings.length < 0) throw new Error('unreachable')
    perCallNs[i] = Number(t1 - t0)
  }
  const batchNs = Number(process.hrtime.bigint() - batchStart)

  const sorted = [...perCallNs].sort((a, b) => a - b)
  const meanNs = perCallNs.reduce((a, b) => a + b, 0) / n
  return {
    scenario: label,
    iterations: n,
    findings_per_call: check(submission).length,
    per_call_ns: {
      mean: meanNs,
      p50: quantile(sorted, 50),
      p95: quantile(sorted, 95),
      max: sorted[sorted.length - 1],
    },
    per_call_us: {
      mean: meanNs / 1000,
      p50: quantile(sorted, 50) / 1000,
      p95: quantile(sorted, 95) / 1000,
      max: sorted[sorted.length - 1] / 1000,
    },
    batch_total_ms: batchNs / 1e6,
    batch_throughput_per_s: n / (batchNs / 1e9),
  }
}

/** 计时器开销：同样次数的空打点差分（不调 check）。 */
function benchTimerOverhead(n) {
  const perPairNs = new Array(n)
  for (let i = 0; i < n; i += 1) {
    const t0 = tick()
    const t1 = tick()
    perPairNs[i] = Number(t1 - t0)
  }
  const sorted = [...perPairNs].sort((a, b) => a - b)
  return {
    iterations: n,
    pair_overhead_ns: {
      mean: perPairNs.reduce((a, b) => a + b, 0) / n,
      p50: quantile(sorted, 50),
      p95: quantile(sorted, 95),
    },
    note: '一次 hrtime 差分（两次调用）的开销；per_call_ns 未扣除该开销',
  }
}

const wallStart = performance.now()
const result = {
  bench: 'acte-shield.check()',
  plugin: 'code/plugins/acte-shield.js',
  node: process.version,
  arch: `${process.platform}-${process.arch}`,
  iterations_per_scenario: N,
  scenarios: [
    benchScenario('hot', HOT, N),
    benchScenario('cold', COLD, N),
  ],
  timer_overhead: benchTimerOverhead(Math.min(N, 20000)),
}
result.wall_total_ms = performance.now() - wallStart

process.stdout.write(JSON.stringify(result) + '\n')
