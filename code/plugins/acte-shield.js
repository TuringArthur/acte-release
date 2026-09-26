/**
 * ACTE 第一层（运行时侧）：不可逆动作护栏（安全盾）。
 *
 * 理论依据：`method/formalization.md` 定理 1，硬约束下盾
 * `Shield(s) = { a : (s,a) ∉ T_irr 且 ∃ 安全续行 }` 只依赖 ⟨S,A,P,T_irr⟩，
 * 不依赖效用或策略，故可独立于策略求解。本插件就是那个"独立出来的盾"：
 * 它不看律师的目标函数（细/狠/稳在它眼里没区别），只看案件处于哪个状态、
 * 该状态触发了哪些不可逆转移。
 *
 * 定理 1 在基座上的落点正是这里：理论与"插件可插拔"是同一件事。
 *
 * ## 本插件与"拦截器"式闸门的根本差异：两种语义
 *
 * `method/formalization.md` §1.1.1 把 `T_irr` 分成两类，本插件分别处理：
 *
 * | 类型 | 例 | 本插件的行为 |
 * |---|---|---|
 * | 动作型（`active`） | `gate.escalation-risk`（起诉引致处罚加重/转刑事） | 拦：`require_confirmation`，须当事人书面确认 |
 * | 不作为型（`passive`） | `gate.suspension-of-execution`（未申请停止执行） | 催：`block`，以"必须在期限内作为"的形式报出 |
 *
 * 实测依据（`method/formalization.md` §1.1.2）：对 `rules/gates.yaml` 全 18 条闸门
 * 逐条判定，属本篇范围且构成不可逆转移的 3 条里，2 条是不作为型。
 * 故若把闸门一律实现为"拦截器"（只做减法），它们会全数漏防，
 * 因为"等待"这个动作在拦截器眼里从来不是"被拦下的动作"，
 * 而实验数字上看不出这一点（漏防表现为"该实例从未触发闸门"，与"本就无需触发"不可区分）。
 *
 * ## 时间增广要求（§1.1.1）
 *
 * 不作为型风险要求状态携带剩余期限：同一状态在不同时刻的 ⊥ 转移不同。
 * 故本插件在判定不作为型时必须拿到 `deadlineDays`；缺失时报"无法判定期限"
 * 而不是静默放行，静默放行会让闸门时灵时不灵。
 *
 * ## `T_IRR` 是生成的，不要手改
 *
 * `T_IRR` 与 `LEVEL_SEMANTICS` 由 `code/tools/gen_plugin_shield.py` 从权威数据
 * `code/data/t-irr.json` 生成（下方 `GENERATED` 标记之间）。
 * 手改会在下次生成时被覆盖，且由 `code/tools/check_plugin_data.py` 判为过期。
 *
 * 为什么用生成而不是"内联一份 + 定期比对"：内联副本过期这件事在运行时完全
 * 看不出来，插件照常加载、照常挂载、`announce` 照常打印"层已启用"，
 * 只是它拦的规则是旧的（"插件看着装上了、其实层是哑的"这一类失效）。
 * 生成则让漂移在结构上不可能发生，校验退化成一次文本比对。
 *
 * @module ctd-acte/plugins/acte-shield
 */

import { announce, EMPTY_VERDICT, KIND_ACTIVE, KIND_PASSIVE,
         SHIELD_LAYER } from './acte-protocol.js'

export const name = 'acte-shield'

/** 本层标识（名字的唯一来源在协议模块，避免两处各写一份）。 */
export const LAYER = SHIELD_LAYER

// >>> GENERATED:T_IRR，由 code/tools/gen_plugin_shield.py 生成，勿手改
export const T_IRR = [
  {
    gateId: 'gate.escalation-risk',
    kind: 'active',
    action: '对行政机关提起诉讼或提起复议',
    levelChange: '处罚层级 → 更高（加重处罚 / 转为刑事案件）',
    ruleRef: 'ch1.3(2)',
    sourcePage: '15-16',
  },
  {
    gateId: 'gate.suspension-of-execution',
    kind: 'passive',
    action: '未在起诉同时提交停止执行申请',
    levelChange: '标的物存续 → 已灭失（拆除/销毁，胜诉亦无意义）',
    ruleRef: 'ch1.3(7)',
    sourcePage: '20',
  },
  {
    gateId: 'gate.statute-of-limitations',
    kind: 'passive',
    action: '未在法定期限内起诉/上诉（等待至期限届满）',
    levelChange: '起诉通道开放 → 关闭',
    ruleRef: 'ch9.8E',
    sourcePage: '162',
  },
]

export const LEVEL_SEMANTICS = '制度可救济度层级（剩余救济手段的多少 / 已承受制度性不利的轻重）'
// <<< GENERATED:T_IRR

/**
 * 不作为型的默认催办阈值（天）：剩余期限不足此值时，即使当事人尚未表态，
 * 盾也主动报出"必须在期限内作为"。可由 profile 的 `urgent_days` 覆盖。
 *
 * 为什么阈值可以是个常数：它不是风险偏好（那属姿态层），而是程序事实，
 * 停止执行申请与起诉需同期提出、起诉有立案与答辩周期，留出的缓冲少了
 * 就无法按期完成。姿态的差异体现在"接受多少不可逆风险"，不在"何时开始催"。
 *
 * @constant {number}
 */
export const DEFAULT_URGENT_DAYS = 15

/**
 * 可逆风险表（ 刻意不入 `T_IRR`，理由要写清楚）。
 *
 * 依据 `method/formalization.md` 定理 1′：解耦（闸门可独立于策略实现）
 * 只在不可逆风险上成立。对可逆风险，独立闸门次优，
 * 定理 1′(ii) 的反例给出量级"约束最优 5 → 盾内最优 0"（全损）。
 * 机制是可逆风险允许策略"用时间维度做混合"（多次小额跨越、及时回退），
 * 而确定性盾在状态层面无法表达时间混合，故会把这类策略一并禁掉。
 *
 * 故这些条目默认不拦（`include_reversible` 默认为 false）。它们存在有两个意义：
 *
 * 1. 作为 `消融配置目录 消融配置目录 experiment/configs/ablations/no-shield-reversible.yml` 的对照物，
 *    那是一条反向消融：在不该用独立护栏的地方用它，预期变坏。
 *    多数工作只做"加模块 ⇒ 变好"；这条测的是定理 1′ 的失效方向。
 * 2. 作为论文里"`T_irr` 不是『所有风险』、而是『不可逆风险』"的实例说明。
 *
 * @constant {Array<object>}
 */
export const REVERSIBLE_RISKS = [
  {
    riskId: 'W-ESC-03',
    kind: 'reversible',
    action: '申请诉讼保全时未备担保或保全范围超出请求',
    // 为什么可逆：保全可申请解除，损害赔偿责任可赔偿了结，
    // 是财产性负担，不是"剩余救济不可逆地减少"（§1.1 的层级定义）。
    whyNotIrreversible:
      '保全可申请解除、赔偿责任可了结——财产性负担，非剩余救济不可逆减少',
    ruleRef: 'ch1.3(2)',
  },
]

/**
 * 检查一次提交，产出盾的 finding。
 *
 * @param {object} submission - 提交内容：
 *   `{ caseType, actions?, triggered?, deadlineDays?, confirmed? }`
 *   - `triggered`：本状态已触发的条目 id 数组（含 `T_irr` 与可逆风险两类）；
 *   - `deadlineDays`：剩余期限（天）；不作为型判定必须提供；
 *   - `confirmed`：已被当事人确认的动作 id 数组（动作型的放行条件）。
 * @param {object} [options] - 本层选项：
 *   - `urgentDays`：不作为型的催办阈值（默认 `DEFAULT_URGENT_DAYS`）；
 *   - `includeReversible`：是否把可逆风险也纳入拦截（默认 `false`）。
 *     默认值不是保守设置，而是定理 1′ 给出的判据：对可逆风险，独立闸门会次优。
 *     置为 `true` 是 `no-shield-reversible` 消融臂的配置。
 * @returns {Array<object>} findings。
 */
export function check(submission, options) {
  const s = submission === undefined || submission === null ? {} : submission
  const o = options === undefined || options === null ? {} : options
  const urgentDays =
    o.urgentDays === undefined ? DEFAULT_URGENT_DAYS : Number(o.urgentDays)
  const includeReversible = o.includeReversible === true
  const triggered = Array.isArray(s.triggered) ? s.triggered : []
  const confirmed = Array.isArray(s.confirmed) ? s.confirmed : []
  const findings = []

  const known = new Set([
    ...T_IRR.map((e) => e.gateId),
    ...REVERSIBLE_RISKS.map((e) => e.riskId),
  ])
  for (const id of triggered) {
    if (!known.has(id)) {
      findings.push({
        layer: LAYER,
        code: 'unregistered_irreversible',
        // `gateId` 是结构化的条目 id，好让发射端按 id 机械映射回动作，
        // 不必去解析 message 文本（那正是两处实现漂移的常见起点）。
        gateId: String(id),
        locus: '行动前检查',
        evidence: String(id).slice(0, 120),
        message:
          `触发了未登记的条目「${id}」——未登记的条目不在盾的管辖内，` +
          '静默忽略等于漏防，故此处报出。',
        severity: 'warn_and_log',
      })
    }
  }

  // ── 双源时钟冲突：强制"时钟存疑"确认，正交于催办阈值 ──────────────
  //
  // 实测事故（BASELINES §4.3）：模型把 10 天算成 180 天 ⇒ 盾按错误时钟判
  // "不催办" ⇒ 失权。发射端已把 `deadlineDays` 归一为两源的 min（催办必响），
  // 盾在这里负责把冲突本身报成必须核实的确认项，即使 min 仍在阈值之上，
  // 冲突也意味着状态里那个 r 可疑，涉期限动作不得直接采取。
  const conflict = s.clockConflict
  if (conflict && conflict.structured !== conflict.declared) {
    // 挂到"消费时钟"的闸门上（已触发的被动型条目），使 summarize 能把它
    // 机械映射回触发该闸门的动作；没有被动闸门触发时退化为一条无 gateId 的
    // 提示（不拦任何动作，但模型与日志都看得见）。
    const clockGates = T_IRR.filter(
      (e) => e.kind === KIND_PASSIVE && triggered.includes(e.gateId))
    const targets = clockGates.length > 0 ? clockGates : [null]
    for (const item of targets) {
      findings.push({
        layer: LAYER,
        code: 'irreversible_guard_clock_conflict',
        ...(item ? { gateId: item.gateId, ruleRef: item.ruleRef } : {}),
        locus: '行动前检查',
        evidence: `structured=${conflict.structured} declared=${conflict.declared}`,
        message:
          `时钟存疑：结构化字段声明剩余 ${conflict.structured} 天，`
          + `提交自报 ${conflict.declared} 天——盾已按较小值 ${conflict.used} 天`
          + '判定催办。闸门按状态里的时钟判，时钟错了催办就不响：'
          + '在向当事人核实起算点前，涉期限的动作不得直接采取。',
        severity: 'require_confirmation',
      })
    }
  }

  // ── 可逆风险：默认不拦（见 REVERSIBLE_RISKS 的说明）──
  if (includeReversible) {
    for (const item of REVERSIBLE_RISKS) {
      if (!triggered.includes(item.riskId)) continue
      findings.push({
        layer: LAYER,
        code: 'reversible_risk_overblocked',
        gateId: item.riskId,
        ruleRef: item.ruleRef,
        locus: '行动前检查',
        evidence: item.action,
        message:
          `本条为**可逆**风险（${item.whyNotIrreversible}），已被独立护栏拦下。` +
          '定理 1′：对可逆风险，独立闸门会次优——它把"值得冒的险"一并禁掉了。' +
          '本项仅在 no-shield-reversible 消融臂下出现，用于观测该次优。',
        severity: 'block',
      })
    }
  }

  for (const item of T_IRR) {
    if (!triggered.includes(item.gateId)) continue

    if (item.kind === KIND_ACTIVE) {
      // 动作型：拦（须当事人确认）。未经确认即报 require_confirmation。
      if (confirmed.includes(item.action) || confirmed.includes(item.gateId)) {
        continue
      }
      findings.push({
        layer: LAYER,
        code: 'irreversible_action_guard',
        gateId: item.gateId,
        ruleRef: item.ruleRef,
        locus: '行动前检查',
        evidence: item.action,
        message:
          `拟采取的行动「${item.action}」跨制度层级且不可回卷（${item.levelChange}）。` +
          '须向当事人前置提示升级风险并取得书面确认后方可继续。',
        severity: 'require_confirmation',
      })
    } else if (item.kind === KIND_PASSIVE) {
      // 不作为型：催（强制在期限内作为）。这是与动作型的实质差别。
      const days = s.deadlineDays
      if (days === undefined || days === null) {
        // 时间增广分量缺失：报"无法判定"，不静默放行。
        findings.push({
          layer: LAYER,
          code: 'irreversible_guard_no_clock',
          gateId: item.gateId,
          ruleRef: item.ruleRef,
          locus: '行动前检查',
          evidence: item.action,
          message:
            `检测到不作为型不可逆风险「${item.gateId}」，但状态未提供剩余期限，` +
            '无法判定是否已跨层级边界。须补上期限分量（§1.1.1 的时间增广要求）——' +
            '缺期限会让该闸门时灵时不灵。',
          severity: 'warn_and_log',
        })
        continue
      }
      if (Number(days) > urgentDays) continue
      findings.push({
        layer: LAYER,
        code: 'irreversible_omission_guard',
        gateId: item.gateId,
        ruleRef: item.ruleRef,
        locus: '行动前检查',
        evidence: item.action,
        message:
          `剩余期限仅 ${days} 天，若不作为将跨制度层级且不可回卷（${item.levelChange}）。` +
          '须在期限内作为——本条是**催办**，不是拦截：不采取该保护性行动本身就是风险。',
        severity: 'block',
      })
    }
  }
  return findings
}

/**
 * 由行配置取本层选项。
 *
 * `urgent_days` 与 `include_reversible` 由 profile overlay 声明
 * （见 profile 覆盖配置 与 `ablations/no-shield-reversible.yml`）。
 * 判据必须读配置，不得把这两项内联成常数，否则消融改了配置而行为不变，
 * 会表现为"消融跑了、效应没变"，而原因（配置没被读）看不出来。
 *
 * ## 行配置的参数位置
 *
 * 本函数原先写成 `optionsFrom(ctx)` 并从 ctx 上读 config 属性。这在静态测试下
 * 看不出来，但在真实会话里加载即崩：
 *
 *     Error: failed to apply loader entry acte-shield (@ctd-la/acte-dsh/shield):
 *            cannot get property "config" without inject
 *
 * 原因是 Cordis 的上下文是带守卫的 Proxy：未在 `inject` 里声明的服务属性
 * （`config` 正是其一）一读就抛；而行配置的正路是 `apply(ctx, config)`
 * 的第二个参数（DSH 自己的插件都这么写）。旧写法之所以"测起来全绿"，
 * 是因为当时的 node 测试喂的是 `{ config: {...} }` 这样的假 ctx，
 * 假 ctx 没有那道守卫，于是测试通过、真机崩溃。
 * 故现在的测试改喂会抛异常的假 ctx（见 `tests/test_plugin_shield.mjs` 的 [H] 组）。
 *
 * @param {object} config - 行配置（`apply` 的第二个参数），不是 ctx。
 * @returns {object} 传给 `check` 的 options。
 */
export function optionsFrom(config) {
  const cfg = config === undefined || config === null ? undefined : config
  if (cfg === undefined || cfg === null) return {}
  const out = {}
  if (cfg.urgent_days !== undefined) out.urgentDays = cfg.urgent_days
  if (cfg.include_reversible !== undefined) out.includeReversible = cfg.include_reversible
  return out
}

/**
 * 挂载盾：把本层裁决并入 waterfall 下游结果后委托。
 *
 * 注意本插件不看姿态（细/狠/稳 在它眼里无区别）：这正是定理 1(a) 说的
 * "盾只依赖 ⟨S,A,P,T_irr⟩"。姿态的差异由策略层承担，不在闸门里。
 *
 * @param {object} ctx - 插件上下文；监听器随插件卸载自动移除。
 * @returns {void}
 */
export function apply(ctx, config) {
  const opts = optionsFrom(config)
  const nActive = T_IRR.filter((e) => e.kind === KIND_ACTIVE).length
  const nPassive = T_IRR.length - nActive
  const days = opts.urgentDays === undefined ? DEFAULT_URGENT_DAYS : opts.urgentDays
  const extra = opts.includeReversible ? ' / 含可逆风险（反向消融臂）' : ''
  announce(ctx, LAYER,
    `T_irr ${T_IRR.length} 条（动作型 ${nActive} / 不作为型 ${nPassive}）` +
    ` / 催办阈值 ${days} 天${extra}`)
  ctx.on('acte/shield', async (submission, next) => {
    const downstream = typeof next === 'function' ? await next() : EMPTY_VERDICT
    const base = Array.isArray(downstream && downstream.findings) ? downstream.findings : []
    const inner = Array.isArray(downstream && downstream.layers) ? downstream.layers : []
    // `layers` 把自己并进去：发射端据此区分"盾在、本次无风险要报"与"盾根本没挂上"。
    // 少了它，`no-shield` 消融下发射端只看得到空 findings ⇒ 静默放行。
    return { findings: [...base, ...check(submission, opts)], layers: [LAYER, ...inner] }
  })
}

/** DSH 加载器按 `module.default` 取插件对象。 */
export default { name, inject: [], apply }