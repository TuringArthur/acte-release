/**
 * 发射端插件的独立入口（`node code/tests/test_plugin_plan.mjs`）。
 *
 * 断言本体在 `plugin_plan_suite.mjs`，因为 `verify_all.py` 的 node 步骤只能有一个
 * 入口（本仓库把子进程面收敛到一处，静态安全审查把新增的 `subprocess.run`
 * 判为命令注入），所以那条"一个入口"由 `test_plugin_shield.mjs` 承担并顺带跑本套件；
 * 本文件是手动跑发射端专用的入口。
 */

import { runPlanSuite } from './plugin_plan_suite.mjs'

let n = 0
const failed = []

function ok(label, cond, detail = '') {
  n += 1
  if (cond) {
    console.log(`  ok   ${label}`)
  } else {
    console.log(`  FAIL ${label}${detail ? '  — ' + detail : ''}`)
    failed.push(label)
  }
}

console.log('='.repeat(70))
console.log('发射端插件运行时回归测试（单独入口）')
console.log('='.repeat(70))

await runPlanSuite(ok)

console.log('\n' + '='.repeat(70))
if (failed.length) {
  console.log(`失败 ${failed.length} / ${n} 项：`)
  for (const f of failed) console.log('  - ' + f)
  process.exit(1)
}
console.log(`全部通过（${n} 项断言）`)
process.exit(0)
