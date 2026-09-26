/**
 * 盾插件的运行时回归测试（Node ESM，直接 `node code/tests/test_plugin_shield.mjs`）。
 *
 * ## 为什么必须有这个测试
 *
 * `check_plugin_data.py` 只能做静态核查（生成段是否一致、判据段是否引用了两型
 * 常量、时间增广守卫是否在场）。但静态核查证明不了行为：
 *
 * - 表里两型都有、判据里也引用了两个常量，但分支写反（把不作为型也当拦截处理）
 *   ， 静态看全对，运行时全数漏防；
 * - 缺 `deadlineDays` 时报了 finding，但 `severity` 写成放行档
 *   ， 静态看"报出来了"，实际等于放行。
 *
 * 故必须有一步真的跑 JS。本文件就是那一步。
 *
 * ## 测什么
 *
 * | 测试 | 对应 §1.1.1 的哪条要求 |
 * |---|---|
 * | 动作型：未确认 ⇒ 报 require_confirmation | 拦 |
 * | 动作型：已确认 ⇒ 不报 | 拦（确认后放行） |
 * | 不作为型：期限临近 ⇒ 报 block（催办，不是拦截） | 催 ， 与动作型的实质差别 |
 * | 不作为型：期限充裕 ⇒ 不报 | 催（未到阈值不催） |
 * | 不作为型：缺期限 ⇒ 报"无法判定"，且不静默放行 | 时间增广要求 |
 * | 未登记条目 ⇒ 报出（不静默忽略） | 漏防守卫 |
 * | 未触发时 ⇒ 不报任何东西 | 防"见谁都报" |
 * | `apply` 委托下游且保留下游 findings | waterfall 规则 |
 * | `apply(ctx, config)` 不读 `ctx.config`（喂会抛的假 ctx） | 真机加载期崩溃 |
 * | 裁决带 `layers` 且含本层名 | 消融可观察：分辨"盾在但本次无风险"与"盾没挂上" |
 * | 行配置（`urgent_days`）经 `apply` 第二参数生效 | 消融 = 一行配置 diff |
 * | 内联表与快照条数一致（与 Python 侧校验互为独立证据） | 防漂移 |
 *
 * 最后一条刻意与 `check_plugin_data.py` 重复：那一步走 Python 文本比对，
 * 这一步走 JS 实际加载。两条独立路径都说一致，才排除"Python 读的文件
 * 和 node 加载的文件不是同一份"这种低级错。
 *
 * ## 一个入口
 *
 * 本文件同时跑发射端套件（`plugin_plan_suite.mjs`）。原因不是省事：
 * `verify_all.py` 的 node 步骤只能有一个入口，本仓库把子进程面收敛到一处
 * （静态安全审查把新增的 `subprocess.run` 判为命令注入）。
 * 而盾与发射端必须一起验证（缺发射端，盾的裁决进不了会话，症状与"本案无风险"同形），
 * 故由本入口一并跑掉，不再新增第二个 subprocess 调用。
 */

import { check, apply, optionsFrom, T_IRR, REVERSIBLE_RISKS, LAYER,
         DEFAULT_URGENT_DAYS, LEVEL_SEMANTICS } from '../plugins/acte-shield.js'
// 本入口同时跑发射端套件（见文件顶部"一个入口"的说明）。
import { runPlanSuite } from './plugin_plan_suite.mjs'

let n = 0
const failed = []

function ok(name, cond, detail = '') {
  n += 1
  if (cond) {
    console.log(`  ok   ${name}`)
  } else {
    console.log(`  FAIL ${name}${detail ? '  — ' + detail : ''}`)
    failed.push(name)
  }
}

const ACTIVE_ID = 'gate.escalation-risk'
const PASSIVE_ID = 'gate.suspension-of-execution'

console.log('='.repeat(70))
console.log('盾插件运行时回归测试')
console.log('='.repeat(70))

// ── 内联表形态 ────────────────────────────────────────────────
console.log('\n[A] 内联表的形态')
ok('T_irr 非空（空表 = 盾不拦任何动作）', T_IRR.length > 0, `n=${T_IRR.length}`)
const actives = T_IRR.filter((e) => e.kind === 'active')
const passives = T_IRR.filter((e) => e.kind === 'passive')
ok('动作型与非动作型都在（只实现一型 = 漏防的根源）',
   actives.length > 0 && passives.length > 0,
   `active=${actives.length} passive=${passives.length}`)
ok('层级语义体现「可救济度」（非处罚严重度）', LEVEL_SEMANTICS.includes('可救济度'))
ok('含动作型的真实条目 gate.escalation-risk',
   actives.some((e) => e.gateId === ACTIVE_ID))

// ── 动作型：拦 ───────────────────────────────────────────────
console.log('\n[B] 动作型：拦（须当事人确认）')
{
  const f = check({ triggered: [ACTIVE_ID] })
  const hit = f.filter((x) => x.code === 'irreversible_action_guard')
  ok('未确认 ⇒ 报一条 irreversible_action_guard', hit.length === 1, JSON.stringify(f))
  ok('力度为 require_confirmation（拦，不是警告）',
     hit.length === 1 && hit[0].severity === 'require_confirmation',
     hit[0] ? hit[0].severity : '(无)')
  ok('说明里带上跨层级后果（供当事人确认时有据）',
     hit.length === 1 && hit[0].message.includes('不可回卷'))
  ok('finding 带**结构化** gateId（发射端按它机械映射回动作，不去解析 message 文本）',
     hit.length === 1 && hit[0].gateId === ACTIVE_ID,
     hit[0] ? String(hit[0].gateId) : '(无)')
}
{
  const f = check({ triggered: [ACTIVE_ID], confirmed: [ACTIVE_ID] })
  ok('已确认 ⇒ 不再报（确认后放行）',
     f.filter((x) => x.code === 'irreversible_action_guard').length === 0,
     JSON.stringify(f))
}

// ── 不作为型：催（ 与动作型的实质差别）──────────────────────
console.log('\n[C] 不作为型：催（ 与动作型的实质差别）')
{
  const f = check({ triggered: [PASSIVE_ID], deadlineDays: 3 })
  const hit = f.filter((x) => x.code === 'irreversible_omission_guard')
  ok('期限临近 ⇒ 报 irreversible_omission_guard', hit.length === 1, JSON.stringify(f))
  ok(`力度为 block（催办；阈值 ${DEFAULT_URGENT_DAYS} 天）`,
     hit.length === 1 && hit[0].severity === 'block',
     hit[0] ? hit[0].severity : '(无)')
  ok('说明写明本条是催办而不是拦截',
     hit.length === 1 && hit[0].message.includes('催办'))
}
{
  const f = check({ triggered: [PASSIVE_ID], deadlineDays: DEFAULT_URGENT_DAYS + 30 })
  ok('期限充裕 ⇒ 不催（未到阈值不打扰）',
     f.filter((x) => x.code === 'irreversible_omission_guard').length === 0,
     JSON.stringify(f))
}

// ── 时间增广（ 缺期限不许静默放行）──────────────────────────
console.log('\n[D] 时间增广（§1.1.1）')
{
  const f = check({ triggered: [PASSIVE_ID] })
  const hit = f.filter((x) => x.code === 'irreversible_guard_no_clock')
  ok('缺 deadlineDays ⇒ 报 irreversible_guard_no_clock', hit.length === 1,
     JSON.stringify(f))
  ok(' 且**不是**静默放行（findings 非空）', f.length > 0)
  ok('说明点出时间增广要求',
     hit.length === 1 && hit[0].message.includes('时间增广'))
  ok('缺期限的 finding 也带 gateId（发射端据此提示"请补剩余期限"）',
     hit.length === 1 && hit[0].gateId === PASSIVE_ID,
     hit[0] ? String(hit[0].gateId) : '(无)')
  ok('力度为 warn_and_log（无法判定，须补状态而非直接拦）',
     hit.length === 1 && hit[0].severity === 'warn_and_log',
     hit[0] ? hit[0].severity : '(无)')
}

// ── 漏防守卫与"见谁都报"守卫 ─────────────────────────────────
console.log('\n[E] 守卫')
{
  const f = check({ triggered: ['gate.does-not-exist'] })
  ok('未登记条目 ⇒ 报 unregistered_irreversible（不静默忽略）',
     f.filter((x) => x.code === 'unregistered_irreversible').length === 1,
     JSON.stringify(f))
}
{
  const f = check({ triggered: [] })
  ok('未触发 ⇒ 不报任何东西（防「见谁都报」）', f.length === 0, JSON.stringify(f))
}
{
  const f = check({})
  ok('空 submission ⇒ 不抛错、不报（健壮性）', Array.isArray(f) && f.length === 0)
}

// ── apply：waterfall 委托 ────────────────────────────────────
console.log('\n[F] apply：waterfall 委托 + 行配置（消融 = 一行配置 diff 的前提）')
{
  // 假 ctx 必须忠实：Cordis 的上下文是带守卫的 Proxy，读未 inject 的服务属性即抛。
  //   旧测试喂的是 `{ config: {...} }` 这样一个没有守卫的普通对象，于是
  //   "插件读 ctx.config" 在测试里全绿、在真机加载期就崩（实测：
  //   `cannot get property "config" without inject`）。本组用会抛的假 ctx 复现那道守卫，
  //   让同一类错误在测试里就现形。
  function makeStubCtx() {
    const handlers = {}
    let sawLayer = false
    const target = {
      logger: { info: () => {} },
      on: (evt, fn) => { handlers[evt] = fn },
    }
    const ctx = new Proxy(target, {
      get(t, prop) {
        if (prop in t) return t[prop]
        throw new Error(`cannot get property "${String(prop)}" without inject`)
      },
    })
    return { ctx, handlers, markLayer: () => { sawLayer = true }, hadLayer: () => sawLayer }
  }

  const stub = makeStubCtx()
  const orig = process.stderr.write
  process.stderr.write = (s) => { if (String(s).includes(LAYER)) stub.markLayer(); return true }
  let threw = null
  try { apply(stub.ctx, {}) } catch (err) { threw = err }
  process.stderr.write = orig

  ok(' apply(ctx, config) 不读 ctx.config（读即抛，真机表现为加载期崩溃）',
     threw === null, threw ? String(threw.message) : '')
  ok('apply 在 acte/shield 事件上注册了监听器',
     typeof stub.handlers['acte/shield'] === 'function')
  ok('announce 打印了层名（消融生效的可观察证据）', stub.hadLayer())
  const downstream = { findings: [{ code: 'upstream_marker' }], layers: ['upstream-layer'] }
  // 顶层 await（ESM 支持）：否则本节的异步断言会排到 [G] 之后才执行，
  // 输出顺序会乱，而"输出顺序乱"本身就会让人怀疑断言到底跑没跑。
  const out = await stub.handlers['acte/shield']({ triggered: [ACTIVE_ID] },
                                                 async () => downstream)
  ok('委托下游且保留下游 findings（只叠加不覆盖）',
     out.findings.some((x) => x.code === 'upstream_marker'))
  ok('本层 findings 也并入', out.findings.length >= 2, `n=${out.findings.length}`)
  ok(' 裁决带 layers 且含本层名（发射端据此分辨"盾在"与"盾没挂上"）',
     Array.isArray(out.layers) && out.layers.includes(LAYER), JSON.stringify(out.layers))
  ok('下游的层名也保留（多层叠加时不丢层）',
     Array.isArray(out.layers) && out.layers.includes('upstream-layer'))
  const quiet = await stub.handlers['acte/shield']({ triggered: [] },
                                                   async () => ({ findings: [], layers: [] }))
  ok('未触发时 layers **仍含本层**（"在但无风险"必须可与"不在"区分）',
     Array.isArray(quiet.layers) && quiet.layers.includes(LAYER),
     JSON.stringify(quiet.layers))

  // 行配置必须从 apply 的第二个参数流到判据里（消融就是改这一行）
  const stub2 = makeStubCtx()
  const orig2 = process.stderr.write
  process.stderr.write = () => true
  apply(stub2.ctx, { urgent_days: 90 })
  process.stderr.write = orig2
  const late = await stub2.handlers['acte/shield']({ triggered: [PASSIVE_ID], deadlineDays: 60 },
                                                   async () => ({ findings: [], layers: [] }))
  ok(' 行配置生效：urgent_days=90 下 60 天算临近（默认 15 天不算）',
     late.findings.some((x) => x.code === 'irreversible_omission_guard'),
     JSON.stringify(late.findings.map((x) => x.code)))
  const deflt = await stub.handlers['acte/shield']({ triggered: [PASSIVE_ID], deadlineDays: 60 },
                                                   async () => ({ findings: [], layers: [] }))
  ok('同一提交在**默认配置**下不催（与上条对照，证明起作用的是配置）',
     !deflt.findings.some((x) => x.code === 'irreversible_omission_guard'))
}

// ── 配置驱动：反向消融与阈值覆盖（ 声明必须跑在实现之后）──────
console.log('\n[G] 配置驱动（消融 overlay 声明的开关必须真的生效）')
{
  const rev = REVERSIBLE_RISKS[0]
  ok('可逆风险表非空且**不在** T_IRR 里（否则定理 1′ 的判据落空）',
     REVERSIBLE_RISKS.length > 0 &&
       !T_IRR.some((e) => e.gateId === rev.riskId),
     `riskId=${rev.riskId}`)

  // 默认：不拦可逆风险（定理 1′ 的判据）
  const def = check({ triggered: [rev.riskId] })
  ok('默认（includeReversible 未设）⇒ 不拦可逆风险',
     def.filter((x) => x.code === 'reversible_risk_overblocked').length === 0,
     JSON.stringify(def))

  // 消融臂：打开开关 ⇒ 拦（该臂用于观测次优）
  const abl = check({ triggered: [rev.riskId] }, { includeReversible: true })
  ok('no-shield-reversible 臂（includeReversible=true）⇒ 拦并说明次优',
     abl.filter((x) => x.code === 'reversible_risk_overblocked').length === 1 &&
       abl[0].message.includes('定理 1′'),
     JSON.stringify(abl))
  ok(' 两个开关状态给出不同结果（否则消融跑了、效应不变而原因看不出来）',
     def.length !== abl.length, `default=${def.length} ablation=${abl.length}`)

  // 阈值覆盖
  const late = check({ triggered: [PASSIVE_ID], deadlineDays: 60 }, { urgentDays: 90 })
  ok('urgent_days 可被配置覆盖（60 天在阈值 90 下 ⇒ 催）',
     late.filter((x) => x.code === 'irreversible_omission_guard').length === 1,
     JSON.stringify(late))
  const notLate = check({ triggered: [PASSIVE_ID], deadlineDays: 60 })
  ok('默认阈值 15 天下 60 天不催（与上条对照，证明覆盖确实起作用）',
     notLate.filter((x) => x.code === 'irreversible_omission_guard').length === 0)

  // optionsFrom：参数是行配置（apply 的第二个参数），不是 ctx
  ok('optionsFrom 读行配置的 urgent_days',
     optionsFrom({ urgent_days: 90 }).urgentDays === 90)
  ok('optionsFrom 读行配置的 include_reversible',
     optionsFrom({ include_reversible: true }).includeReversible === true)
  ok('optionsFrom 对空配置返回空对象（不抛错）',
     Object.keys(optionsFrom(undefined)).length === 0 &&
       Object.keys(optionsFrom({})).length === 0)
  ok(' 传 ctx 形状（{config:{…}}）**读不出**配置——签名本身就是那道防线',
     optionsFrom({ config: { urgent_days: 90 } }).urgentDays === undefined)
}

// ── [P] 双源时钟冲突 ────────────────────────────────────
//
// 实测事故（BASELINES §4.3）：模型把 10 天算成 180 天 ⇒ 盾按错误时钟判
// "不催办" ⇒ 失权。补丁两半：发射端把 deadlineDays 归一为 min（催办必响），
// 盾把冲突本身报成 require_confirmation（正交于催办阈值）。
console.log('\n[P] 双源时钟冲突（min 判催办 + 时钟存疑强制确认）')
{
  const conflicted = check({
    triggered: [PASSIVE_ID], deadlineDays: 10,
    clockConflict: { structured: 10, declared: 180, used: 10 },
  })
  const cf = conflicted.filter((x) => x.code === 'irreversible_guard_clock_conflict')
  ok('冲突 ⇒ 报 irreversible_guard_clock_conflict 且挂到消费时钟的被动闸门',
     cf.length === 1 && cf[0].gateId === PASSIVE_ID,
     JSON.stringify(cf))
  ok('冲突的力度是 require_confirmation（强制核实，不是放行档）',
     cf.length === 1 && cf[0].severity === 'require_confirmation',
     cf[0] ? cf[0].severity : '(无)')
  ok('消息里点名两个源与"按较小值判定"',
     cf.length === 1 && cf[0].message.includes('180') && cf[0].message.includes('较小'),
     cf[0] ? cf[0].message : '(无)')
  // 冲突与"是否已到催办阈值"正交：阈值之上也必须强制确认
  const calm = check({
    triggered: [PASSIVE_ID], deadlineDays: 30,
    clockConflict: { structured: 30, declared: 50, used: 30 },
  })
  ok('期限尚充裕（30>15）但时钟冲突 ⇒ 仍强制确认、且不误触催办',
     calm.filter((x) => x.code === 'irreversible_guard_clock_conflict').length === 1
       && calm.filter((x) => x.code === 'irreversible_omission_guard').length === 0,
     JSON.stringify(calm))
  const clean = check({ triggered: [PASSIVE_ID], deadlineDays: 60 })
  ok('无冲突 ⇒ 不报 clock_conflict（不"见谁都报"）',
     clean.filter((x) => x.code === 'irreversible_guard_clock_conflict').length === 0)
  const passiveless = check({
    triggered: [ACTIVE_ID], deadlineDays: 60,
    clockConflict: { structured: 60, declared: 40, used: 40 },
  })
  const noGate = passiveless.filter((x) => x.code === 'irreversible_guard_clock_conflict')
  ok('无被动闸门触发时退化为无 gateId 提示（不误拦别的动作）',
     noGate.length === 1 && noGate.every((x) => x.gateId === undefined),
     JSON.stringify(passiveless))
}

function finish() {
  console.log('\n' + '='.repeat(70))
  if (failed.length) {
    console.log(`失败 ${failed.length} / ${n} 项：`)
    for (const f of failed) console.log('  - ' + f)
    process.exit(1)
  }
  console.log(`全部通过（${n} 项断言）`)
  process.exit(0)
}

// ── 发射端套件（同一个入口跑，理由见文件顶部的"一个入口"说明）──────
// 盾与发射端是一对：盾判、发射端发。缺发射端，盾的裁决永远进不了会话，
// 而症状与"本案无风险"同形，故两个套件必须一起跑。
console.log('\n' + '─'.repeat(70))
console.log('发射端插件的运行时断言（套件本体在 plugin_plan_suite.mjs）')
console.log('─'.repeat(70))
await runPlanSuite(ok)

// 显式收尾。不要把它挪进某个 async 回调里，那样很容易在改动中丢掉，
// 而丢掉的表现是"进程静默退出 0、没有汇总行"，看起来像通过。
// 这正是 verify_all 第 10 步检查"输出里含『全部通过』"的原因：
// 只看退出码会把"忘了收尾"当成成功。本文件就踩过一次（改 await 时丢了调用）。
finish()