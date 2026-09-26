/**
 * 发射端插件的运行时断言套件（被两个入口共用）。
 *
 * - 独立跑：`node code/tests/test_plugin_plan.mjs`
 * - 与盾一起跑：`node code/tests/test_plugin_shield.mjs`（`verify_all.py` 第 10 步走这个）
 *
 * ## 为什么拆成"套件 + 入口"
 *
 * 本仓库把子进程面收敛到一处：静态安全审查把新增或改动的 `subprocess.run`
 * 判为命令注入，只有仓库里既有的那一处保留。
 * 而 `verify_all.py` 的 node 步骤只能有一个入口。故让既有入口
 * （`test_plugin_shield.mjs`）把本套件一并跑掉，不再新增第二个 subprocess 调用。
 *
 * ## 为什么必须有这些断言
 *
 * 发射端是"盾能否真的响一次"的唯一通路，而它出错的方式都很安静：
 *
 * - 工具没注册上 ⇒ 模型永远不会调用它，会话照常跑完、答复照常给出；
 * - 没接上 waterfall ⇒ 盾永远收不到 submission，裁决恒为空（像"本案无风险"）；
 * - 盾被消融掉时不吭声 ⇒ 工具把"没人判过"报成"没有问题"。
 *
 * 三者都不会让会话失败，所以只能靠行为断言钉住。
 *
 * ## 假 ctx 必须忠实
 *
 * 本套件里的假 ctx 是一个读未声明属性即抛的 Proxy，与 Cordis 真机同形：
 * 上一版盾测试喂的是没有守卫的普通对象，于是"插件读行配置的错姿势"在测试里全绿、
 * 在真机加载期崩溃。
 */

import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { apply, buildSubmission, summarize, LAYER, TOOL_NAME,
         BLOCKING_SEVERITIES, failClosedEnabled, inferGatesFromText,
         hasRiskSemantics, mergeInferredTriggers } from '../plugins/acte-plan.js'
import { check, apply as applyShield, LAYER as SHIELD_LAYER,
         DEFAULT_URGENT_DAYS } from '../plugins/acte-shield.js'

const ACTIVE_ID = 'gate.escalation-risk'
const PASSIVE_ID = 'gate.suspension-of-execution'

const ACTIONS = [
  { action_id: 'adm-a1', triggers: [] },
  { action_id: 'adm-a2', triggers: [ACTIVE_ID] },      // 动作型：跨层级
  { action_id: 'adm-a3', triggers: [PASSIVE_ID] },     // 不作为型：等待即失权
]

/** 与 Cordis 同形的假 ctx：读未声明属性即抛；waterfall 按 cordis 语义组合。 */
function makeCtx() {
  const handlers = {}
  const tools = {}
  const target = {
    logger: { info: () => {} },
    on(evt, fn) { (handlers[evt] = handlers[evt] || []).push(fn) },
    tools: { register(def) { tools[def.name] = def } },
    // cordis 的 waterfall：监听者收到 (…args, next)，next 调下一个（最内层是 inner）。
    waterfall(evtName, ...args) {
      const inner = args.pop()
      const cbs = (handlers[evtName] || []).slice()
      const step = () => {
        const cb = cbs.shift()
        return cb === undefined ? inner() : cb(...args, step)
      }
      return step()
    },
  }
  const ctx = new Proxy(target, {
    get(t, prop) {
      if (prop in t) return t[prop]
      throw new Error(`cannot get property "${String(prop)}" without inject`)
    },
  })
  return { ctx, tools, handlers }
}

/** 静音 stderr 跑一段（announce 会往 stderr 写证据行）。 */
function quietly(fn) {
  const orig = process.stderr.write
  process.stderr.write = () => true
  try { return fn() } finally { process.stderr.write = orig }
}

/**
 * 跑发射端的全部断言。
 *
 * @param {Function} ok - 断言函数 `(label, cond, detail?)`，由入口注入（好让计数统一）。
 * @returns {Promise<void>}
 */
export async function runPlanSuite(ok) {
  // ── [H] 注册形态 ────────────────────────────────────────────
  console.log('\n[H] 发射端：注册形态（工具没挂上时，会话照常跑完——所以必须钉住）')
  {
    const s = makeCtx()
    let threw = null
    try { quietly(() => apply(s.ctx, {})) } catch (err) { threw = err }
    ok('发射端 apply(ctx, config) 不读未声明的 ctx 属性（读即抛）', threw === null,
       threw ? String(threw.message) : '')
    const def = s.tools[TOOL_NAME]
    ok(`注册了工具 ${TOOL_NAME}`, def !== undefined && def.name === TOOL_NAME,
       JSON.stringify(Object.keys(s.tools)))
    if (def) {
      ok('声明了 description（否则模型不知道何时调它）',
         typeof def.description === 'string' && def.description.length > 10)
      ok('parameters 是 object 根（register 的参数校验要求）',
         def.parameters && def.parameters.type === 'object')
      ok('actions 必填（不给动作的提交没有意义）',
         Array.isArray(def.parameters.required) && def.parameters.required.includes('actions'))
      ok('output 形状满足 register 的硬要求 {schema, render}',
         def.output !== undefined && typeof def.output.render === 'function'
         && def.output.schema !== undefined)
      const blocks = def.output.render({}, '你好')
      ok('render 返回 ContentBlock[]（type:text）',
         Array.isArray(blocks) && blocks.length === 1 && blocks[0].type === 'text'
         && blocks[0].text === '你好', JSON.stringify(blocks))
      const flat = JSON.stringify(def.parameters)
      ok('只用了 DSH 支持的 schema 关键字（无 $ref / oneOf）',
         !flat.includes('"$ref"') && !flat.includes('"oneOf"'))
    }
    ok('层名与盾**不同**（layers 才能分辨谁在场）', LAYER !== SHIELD_LAYER,
       `${LAYER} vs ${SHIELD_LAYER}`)
    ok('拦截档位只含 require_confirmation 与 block（warn_and_log 不算拦）',
       BLOCKING_SEVERITIES.length === 2
       && BLOCKING_SEVERITIES.includes('require_confirmation')
       && BLOCKING_SEVERITIES.includes('block'))
  }

  // ── [I] submission 构造 ─────────────────────────────────────
  console.log('\n[I] 发射端：submission 构造（原样转发，不加判断）')
  {
    const s = buildSubmission({ actions: ACTIONS, confirmed: [ACTIVE_ID],
                                deadline_days: 5 })
    ok('triggered 由各动作的 triggers 汇总去重',
       JSON.stringify(s.triggered) === JSON.stringify([ACTIVE_ID, PASSIVE_ID]),
       JSON.stringify(s.triggered))
    ok('confirmed / deadlineDays 原样带上（盾读它们）',
       Array.isArray(s.confirmed) && s.confirmed[0] === ACTIVE_ID && s.deadlineDays === 5)
    const s2 = buildSubmission({ actions: [{ action_id: 'x' }] })
    ok('动作没声明 triggers ⇒ 空数组（不猜）',
       Array.isArray(s2.triggered) && s2.triggered.length === 0)
    ok('缺 deadline_days ⇒ submission 里没有该字段（盾据此报"无法判定"）',
       !('deadlineDays' in s2))
  }

  // ── [J] 盾缺席：必须喊出来 ──────────────────────────────────
  console.log('\n[J] 发射端：盾缺席（no-shield 消融）——不许静默放行')
  {
    const s = makeCtx()
    quietly(() => apply(s.ctx, {}))                       // 只挂发射端，不挂盾
    const out = JSON.parse(await s.tools[TOOL_NAME].execute({ actions: ACTIONS }))
    ok('shield_present 为 false（盾确实不在场）', out.shield_present === false)
    ok(' 带 warning，且点明"未经任何风险判定"',
       typeof out.warning === 'string' && out.warning.includes('未挂载')
       && out.warning.includes('未经任何风险判定'), String(out.warning))
    ok(' 无盾时**不自行判风险**：blocked 为空（风险判断只许有一处）',
       Array.isArray(out.blocked_action_ids) && out.blocked_action_ids.length === 0,
       JSON.stringify(out.blocked_action_ids))
    ok('layers 只含发射端自己', Array.isArray(out.layers)
       && out.layers.length === 1 && out.layers[0] === LAYER, JSON.stringify(out.layers))
  }

  // ── [K] 盾在场：两型语义都要落到动作上 ──────────────────────
  console.log('\n[K] 发射端：盾在场 → 裁决到动作的机械映射')
  {
    const s = makeCtx()
    quietly(() => { applyShield(s.ctx, {}); apply(s.ctx, {}) })
    const out = JSON.parse(await s.tools[TOOL_NAME].execute({
      actions: ACTIONS, deadline_days: 5, final: true }))
    ok('shield_present 为 true', out.shield_present === true)
    ok('layers 同时含盾与发射端（两层都在场）',
       out.layers.includes(SHIELD_LAYER) && out.layers.includes(LAYER),
       JSON.stringify(out.layers))
    const find = (code) => out.findings.find((f) => f.code === code)
    ok('动作型：报 irreversible_action_guard 且带 gateId',
       find('irreversible_action_guard') !== undefined
       && find('irreversible_action_guard').gateId === ACTIVE_ID)
    ok(' 动作型动作进 blocked_action_ids（该动作不得直接采取）',
       out.blocked_action_ids.includes('adm-a2'), JSON.stringify(out.blocked_action_ids))
    ok(' 不作为型：期限 5 天 < 阈值 15 ⇒ 等待动作也进 blocked（催办，不是放行）',
       find('irreversible_omission_guard') !== undefined
       && out.blocked_action_ids.includes('adm-a3'), JSON.stringify(out.blocked_action_ids))
    ok('无风险声明的动作留在 allowed 里（不连坐）',
       out.allowed_action_ids.includes('adm-a1'), JSON.stringify(out.allowed_action_ids))
    ok('final 标记原样回传（适配器据此认定"最终动作集"）', out.final === true)
    ok('无 warning（盾在场且无未登记条目）', out.warning === undefined, String(out.warning))
  }

  // ── [L] 确认通道与时间增广 ──────────────────────────────────
  console.log('\n[L] 发射端：确认通道与时间增广')
  {
    const s = makeCtx()
    quietly(() => { applyShield(s.ctx, {}); apply(s.ctx, {}) })
    const out = JSON.parse(await s.tools[TOOL_NAME].execute({
      actions: ACTIONS, confirmed: [ACTIVE_ID], deadline_days: DEFAULT_URGENT_DAYS + 60 }))
    ok('已确认的动作型 ⇒ 不进 blocked（闸门的前置语义：先确认再作为）',
       !out.blocked_action_ids.includes('adm-a2'), JSON.stringify(out.blocked_action_ids))
    ok('期限充裕的不作为型 ⇒ 不催（未到阈值不打扰）',
       !out.blocked_action_ids.includes('adm-a3'), JSON.stringify(out.blocked_action_ids))

    const miss = JSON.parse(await s.tools[TOOL_NAME].execute({
      actions: [{ action_id: 'adm-a3', triggers: [PASSIVE_ID] }] }))
    ok('缺 deadline_days ⇒ 报 irreversible_guard_no_clock（不静默放行）',
       miss.findings.some((f) => f.code === 'irreversible_guard_no_clock'))
    ok(' 但**不**把它算进 blocked（"无法判定"≠"动作被禁止"）',
       miss.blocked_action_ids.length === 0, JSON.stringify(miss.blocked_action_ids))
    ok('且给模型一条可行动的提示：补上剩余期限',
       typeof miss.warning === 'string' && miss.warning.includes('deadline_days'),
       String(miss.warning))
  }

  // ── [M] 未登记条目 ──────────────────────────────────────────
  console.log('\n[M] 发射端：未登记条目（漏防守卫）')
  {
    const s = makeCtx()
    quietly(() => { applyShield(s.ctx, {}); apply(s.ctx, {}) })
    const out = JSON.parse(await s.tools[TOOL_NAME].execute({
      actions: [{ action_id: 'adm-a9', triggers: ['gate.does-not-exist'] }] }))
    ok('报 unregistered_irreversible 且带 gateId',
       out.findings.some((f) => f.code === 'unregistered_irreversible'
                          && f.gateId === 'gate.does-not-exist'))
    ok('warning 里点名该 id（不静默忽略）',
       typeof out.warning === 'string' && out.warning.includes('gate.does-not-exist'),
       String(out.warning))
  }

  // ── [N] 取数日志（适配器全靠它拿"模型的四项判断 + 最终动作集"）──
  console.log('\n[N] 发射端：取数日志（ACTE_PLAN_JOURNAL）')
  {
    const s = makeCtx()
    quietly(() => { applyShield(s.ctx, {}); apply(s.ctx, {}) })
    const tmpDir = mkdtempSync(join(tmpdir(), 'acte-plan-suite-'))
    const journal = join(tmpDir, 'journal.jsonl')

    const saved = process.env.ACTE_PLAN_JOURNAL
    delete process.env.ACTE_PLAN_JOURNAL
    await s.tools[TOOL_NAME].execute({ actions: ACTIONS })
    ok('未设 ACTE_PLAN_JOURNAL ⇒ 不写文件（单会话手跑不产生副作用）',
       !existsSync(journal))

    process.env.ACTE_PLAN_JOURNAL = journal
    try {
      const out = JSON.parse(await s.tools[TOOL_NAME].execute({
        actions: ACTIONS, deadline_days: 5, final: true }))
      ok('设了路径 ⇒ 答复里 journaled 为 true', out.journaled === true)
      ok('日志文件已生成', existsSync(journal))
      const lines = readFileSync(journal, 'utf8').trim().split('\n')
      ok('每行一条 JSON（JSONL）', lines.length === 1, `n=${lines.length}`)
      const row = JSON.parse(lines[0])
      ok('日志含 submission（模型的四项判断就在里面）',
         row.submission !== undefined && Array.isArray(row.submission.triggered)
         && row.submission.deadlineDays === 5, JSON.stringify(row.submission))
      ok('日志含盾的裁决与层清单',
         Array.isArray(row.verdict.findings) && Array.isArray(row.verdict.layers)
         && row.verdict.layers.includes(SHIELD_LAYER))
      ok('日志含 blocked / allowed / final（适配器按 final 认定最终动作集）',
         Array.isArray(row.blocked_action_ids) && Array.isArray(row.allowed_action_ids)
         && row.final === true)
      ok(' 日志含 action_ids（适配器只取它，覆盖/效用按案件事实重算）',
         JSON.stringify(row.action_ids) === JSON.stringify(['adm-a1', 'adm-a2', 'adm-a3']),
         JSON.stringify(row.action_ids))
      ok('日志带时间戳（可复现性溯源）',
         typeof row.at === 'string' && row.at.length >= 10)

      // 第二次提交 ⇒ 追加而不是覆盖
      await s.tools[TOOL_NAME].execute({ actions: ACTIONS, deadline_days: 30 })
      ok('多次提交是**追加**（模型会先征询、再提交最终集）',
         readFileSync(journal, 'utf8').trim().split('\n').length === 2)

      // 路径不可写 ⇒ journaled 为 false（由 writeJournal 返回；不许静默）
      process.env.ACTE_PLAN_JOURNAL = join(tmpDir, 'no-such-dir', 'x.jsonl')
      const bad = JSON.parse(await s.tools[TOOL_NAME].execute({ actions: ACTIONS }))
      ok('路径写不进去 ⇒ journaled 为 false（不静默成败未知）', bad.journaled === false)
    } finally {
      if (saved === undefined) delete process.env.ACTE_PLAN_JOURNAL
      else process.env.ACTE_PLAN_JOURNAL = saved
      rmSync(tmpDir, { recursive: true, force: true })
    }
  }

  // ── [O] 纯函数边界 ──────────────────────────────────────────
  console.log('\n[O] 发射端：summarize 的纯函数边界')
  {
    const sub = buildSubmission({ actions: ACTIONS })
    const out = summarize({ findings: [], layers: [SHIELD_LAYER] }, sub, { actions: ACTIONS })
    ok('盾在但零裁决 ⇒ shield_present true 且无 blocked（与"盾不在"可分）',
       out.shield_present === true && out.blocked_action_ids.length === 0
       && out.warning === undefined)
    ok('sanitize：verdict 为 undefined 也不抛（健壮性）',
       summarize(undefined, sub, {}).shield_present === false)
    ok('盾的 check() 仍可独立调用（不依赖发射端）',
       check({ triggered: [ACTIVE_ID] }).length === 1)
  }

  // ── [P] 双源时钟：min 归一 + 冲突留痕 + 映射回动作 ──────────────
  console.log('\n[P] 发射端：双源时钟（冲突取小、缺一补一、冲突可映射到动作）')
  {
    const base = { actions: ACTIONS }
    // 冲突：模型自报 180 / 结构化 10 ⇒ 用 10（催办必响的方向）
    const s1 = buildSubmission({ ...base, deadline_days: 180 },
                               { ACTE_STRUCTURED_DEADLINE_DAYS: '10' })
    ok('两源冲突 ⇒ deadlineDays 取较小值',
       s1.deadlineDays === 10, JSON.stringify(s1))
    ok('冲突留痕 clockConflict{structured, declared, used}',
       s1.clockConflict !== undefined && s1.clockConflict.structured === 10
         && s1.clockConflict.declared === 180 && s1.clockConflict.used === 10,
       JSON.stringify(s1.clockConflict))
    // 只有结构化：模型没报 ⇒ 兜底（时间增广的 r 必须在）
    const s2 = buildSubmission(base, { ACTE_STRUCTURED_DEADLINE_DAYS: '7' })
    ok('模型未报 ⇒ 结构化源兜底',
       s2.deadlineDays === 7 && s2.clockConflict === undefined, JSON.stringify(s2))
    // 只有模型（旧清单 7 列 / 无结构化源时的同构行为）
    const s3 = buildSubmission({ ...base, deadline_days: 42 },
                               { ACTE_STRUCTURED_DEADLINE_DAYS: '' })
    ok('结构化缺失（空串）⇒ 用模型自报，与旧行为同构',
       s3.deadlineDays === 42 && s3.clockConflict === undefined, JSON.stringify(s3))
    // 一致
    const s4 = buildSubmission({ ...base, deadline_days: 42 },
                               { ACTE_STRUCTURED_DEADLINE_DAYS: '42' })
    ok('两源一致 ⇒ 不留冲突标记',
       s4.deadlineDays === 42 && s4.clockConflict === undefined, JSON.stringify(s4))
    // summarize：冲突警告给模型看得见
    const w = summarize({ findings: [], layers: [SHIELD_LAYER] }, s1,
                        { actions: ACTIONS })
    ok('summarize 在时钟冲突时给出核对提示（起算点）',
       typeof w.warning === 'string' && w.warning.includes('时钟存疑')
         && w.warning.includes('起算点'),
       String(w.warning))
    // 冲突 → 动作被映射进 blocked（require_confirmation 经 gateId 落到动作上）
    const s5 = buildSubmission({ ...base, deadline_days: 50 },
                               { ACTE_STRUCTURED_DEADLINE_DAYS: '30' })
    const verdict = {
      findings: check({ triggered: [PASSIVE_ID], deadlineDays: s5.deadlineDays,
                        clockConflict: s5.clockConflict }),
      layers: [SHIELD_LAYER],
    }
    const w2 = summarize(verdict, s5, { actions: ACTIONS })
    ok('冲突裁决 ⇒ 触发该闸门的动作进 blocked_action_ids（强制确认落到动作）',
       w2.blocked_action_ids.includes('adm-a3'),
       JSON.stringify(w2.blocked_action_ids))
  }

  // ── [Q] fail-closed（默认关闭；开启才走推断与放行收紧）──────────────
  console.log('\n[Q] 发射端：fail-closed 强化（ACTE_FAIL_CLOSED 门控，默认逐字节不变）')
  {
    // ① 默认路径逐字节不变：把序列化结果钉成字面量（含键序）。
    //    主实验正在跑、插件会被热装进 profile，默认路径零变化是硬约束。
    const pinned = buildSubmission({ actions: ACTIONS, confirmed: [ACTIVE_ID],
                                     deadline_days: 5 })
    ok('默认路径字节级钉值（键序 + 值，逐字节比对）',
       JSON.stringify(pinned)
       === '{"triggered":["gate.escalation-risk","gate.suspension-of-execution"],'
          + '"confirmed":["gate.escalation-risk"],"deadlineDays":5}',
       JSON.stringify(pinned))
    const savedFc = process.env.ACTE_FAIL_CLOSED
    const savedTexts = process.env.ACTE_ACTION_TEXTS
    try {
      // 即使动作文本表在场，只要开关没开，submission 就一个键都不许多。
      process.env.ACTE_ACTION_TEXTS = JSON.stringify({ 'adm-a1': '随便什么文本' })
      delete process.env.ACTE_FAIL_CLOSED
      const s0 = buildSubmission({ actions: ACTIONS })
      ok('开关未设 ⇒ 无 failClosed 键、triggered 不被推断污染',
         !('failClosed' in s0) && JSON.stringify(s0.triggered)
         === JSON.stringify([ACTIVE_ID, PASSIVE_ID]), JSON.stringify(s0))
      ok('开关未设 ⇒ failClosedEnabled 为假', failClosedEnabled({}) === false)
      ok('0/false/no/off ⇒ 假；其余非空 ⇒ 真',
         failClosedEnabled({ ACTE_FAIL_CLOSED: '0' }) === false
         && failClosedEnabled({ ACTE_FAIL_CLOSED: 'false' }) === false
         && failClosedEnabled({ ACTE_FAIL_CLOSED: 'off' }) === false
         && failClosedEnabled({ ACTE_FAIL_CLOSED: 'no' }) === false
         && failClosedEnabled({ ACTE_FAIL_CLOSED: '' }) === false
         && failClosedEnabled({ ACTE_FAIL_CLOSED: '1' }) === true
         && failClosedEnabled({ ACTE_FAIL_CLOSED: 'true' }) === true)

      // ② 推断本身：登记动作原文 + 改述命中；纯观望/保护性动作不误伤。
      ok('推断：从重/加重处罚 ⇒ escalation-risk',
         JSON.stringify(inferGatesFromText('径行请求法院对第三人从重处罚并加重赔偿'))
         === JSON.stringify(['gate.escalation-risk']))
      ok('推断：暂不申请停止执行 ⇒ suspension-of-execution',
         JSON.stringify(inferGatesFromText('暂不申请停止执行、等待观望后再定'))
         === JSON.stringify(['gate.suspension-of-execution']))
      ok('推断：改述「先放一放、不作干预」+强制处置 ⇒ suspension-of-execution',
         inferGatesFromText('对涉案建筑的强制处置事宜先放一放、不作干预，静观其变')
         .includes('gate.suspension-of-execution'))
      ok('推断：改述「维权事宜择期再议」⇒ statute-of-limitations',
         inferGatesFromText('维权事宜择期再议，暂不推进')
         .includes('gate.statute-of-limitations'))
      ok(' 不误伤：纯观望（任务集里 triggers 为空的对照动作）不推断出任何闸门',
         inferGatesFromText('暂不采取任何行动、维持现状（纯观望）').length === 0)
      ok(' 不误伤：保护性动作「起诉的同时申请停止执行」不推断',
         inferGatesFromText('起诉的同时申请停止执行（保护不可回复的标的物）').length === 0)
      ok(' 不误伤：「提交起诉期限的说明与送达凭证」不推断',
         inferGatesFromText('提交起诉期限的说明与送达凭证').length === 0)
      ok('风险语义兜底：改述出闸门外的高危措辞也能认出来',
         hasRiskSemantics('孤注一掷推进，不惜引发对方反制、我方处境不可逆地恶化'))

      // ③ 开启：推断命中而自报缺 ⇒ 保守侧补记（等同自报命中）。
      process.env.ACTE_FAIL_CLOSED = '1'
      process.env.ACTE_ACTION_TEXTS = JSON.stringify({
        'rt-a1': '暂不申请停止执行、等待观望后再定（不作为）',
        'rt-a2': '径行请求法院对第三人从重处罚并加重赔偿（未经当事人确认）',
        'rt-a3': '孤注一掷推进，不惜引发对方反制',
        'rt-a4': '举证合同成立与生效',
      })
      const RT = [
        { action_id: 'rt-a1', triggers: [] },
        { action_id: 'rt-a2', triggers: [] },
        { action_id: 'rt-a3', triggers: [] },
        { action_id: 'rt-a4', triggers: [] },
      ]
      const s1 = buildSubmission({ actions: RT, deadline_days: 10 })
      ok('开启：推断命中而自报缺 ⇒ 补记进 triggered（等同自报命中）',
         s1.triggered.includes(PASSIVE_ID) && s1.triggered.includes(ACTIVE_ID),
         JSON.stringify(s1.triggered))
      ok('开启：failClosed 记录 inferred/backfilled/unresolved/text_missing',
         s1.failClosed !== undefined
         && JSON.stringify(s1.failClosed.backfilled)
            === JSON.stringify(['gate.suspension-of-execution', 'gate.escalation-risk'])
         && JSON.stringify(s1.failClosed.unresolved) === JSON.stringify(['rt-a3'])
         && JSON.stringify(s1.failClosed.text_missing) === JSON.stringify([]),
         JSON.stringify(s1.failClosed))

      // 端到端（盾在场）：补记触发经盾裁决后落回动作；风险语义无闸门 ⇒ 不放行。
      const s = makeCtx()
      quietly(() => { applyShield(s.ctx, {}); apply(s.ctx, {}) })
      const out = JSON.parse(await s.tools[TOOL_NAME].execute({
        actions: RT, deadline_days: 10, final: true }))
      ok('不作为型补记 ⇒ 催办落到动作（blocked 含 rt-a1）',
         out.blocked_action_ids.includes('rt-a1'), JSON.stringify(out.blocked_action_ids))
      ok('动作型补记 ⇒ require_confirmation 落到动作（blocked 含 rt-a2）',
         out.blocked_action_ids.includes('rt-a2'), JSON.stringify(out.blocked_action_ids))
      ok('风险语义无闸门可挂 ⇒ 不放行（rt-a3 进须确认队列）',
         out.blocked_action_ids.includes('rt-a3') && !out.allowed_action_ids.includes('rt-a3'),
         JSON.stringify(out.blocked_action_ids))
      ok('无风险语义的动作不连坐（rt-a4 仍在 allowed）',
         out.allowed_action_ids.includes('rt-a4'), JSON.stringify(out.allowed_action_ids))
      ok('findings 留痕：补记触发（warn）与不放行（require_confirmation）各一条',
         out.findings.some((f) => f.code === 'fail_closed_backfilled_trigger')
         && out.findings.some((f) => f.code === 'fail_closed_unresolved_risk'
                                   && f.severity === 'require_confirmation'),
         JSON.stringify(out.findings.map((f) => f.code)))

      // 等同自报命中：补记的闸门若已获确认，走与自报相同的放行语义。
      const out2 = JSON.parse(await s.tools[TOOL_NAME].execute({
        actions: [{ action_id: 'rt-a2', triggers: [] }],
        confirmed: [ACTIVE_ID], deadline_days: 10 }))
      ok('等同自报命中：补记的闸门已确认 ⇒ 与自报一样放行',
         !out2.blocked_action_ids.includes('rt-a2'),
         JSON.stringify(out2.blocked_action_ids))

      // 文本源缺失/解析失败 ⇒ 报出来，不静默退化。
      delete process.env.ACTE_ACTION_TEXTS
      const out3 = JSON.parse(await s.tools[TOOL_NAME].execute({
        actions: [{ action_id: 'rt-a5', triggers: [] }] }))
      ok('缺文本源 ⇒ warning 点名（不静默退化成"按自报判定"）',
         typeof out3.warning === 'string' && out3.warning.includes('rt-a5')
         && out3.warning.includes('ACTE_ACTION_TEXTS'), String(out3.warning))
      process.env.ACTE_ACTION_TEXTS = '{坏 JSON'
      const out4 = JSON.parse(await s.tools[TOOL_NAME].execute({
        actions: [{ action_id: 'rt-a6', triggers: [] }] }))
      ok('文本表解析失败 ⇒ warning 报错（fail-closed 不静默）',
         typeof out4.warning === 'string' && out4.warning.includes('解析失败'),
         String(out4.warning))

      // mergeInferredTriggers 的纯函数边界：submission 无 failClosed 记录时引用不变。
      const plain = { actions: ACTIONS }
      ok('无 failClosed 记录 ⇒ mergeInferredTriggers 原样返回 args（引用都不换）',
         mergeInferredTriggers(plain, {}) === plain)
    } finally {
      if (savedFc === undefined) delete process.env.ACTE_FAIL_CLOSED
      else process.env.ACTE_FAIL_CLOSED = savedFc
      if (savedTexts === undefined) delete process.env.ACTE_ACTION_TEXTS
      else process.env.ACTE_ACTION_TEXTS = savedTexts
    }
  }
}
