/**
 * ACTE 层的运行时协议 ， 盾插件与后续的策略插件共用。
 *
 * 为什么是 JavaScript 而不是 TypeScript（实测结论，别改回去）：DSH 的行插件从
 * profile 的 `node_modules/` 里按包名加载，而 Node 拒绝为 `node_modules` 下的
 * 文件剥离 TS 类型（实测报错：`Stripping types is currently unsupported for
 * files under node_modules`）。故此处直接交付 DSH 真正加载的形式：
 * 带 JSDoc 的 ESM JavaScript，与 `code/ctd_acte/*.py` 的 Python 内核同口径。
 *
 * 事件走 Cordis 的 waterfall：监听者收到 `(submission, next)`，可包裹下游裁决；
 * 只做标注的监听者必须委托 `next()`。盾插件把本层的裁决并入 `findings` 后委托，
 * 因此消融掉盾（消融配置 里把该行 `disabled: true`）
 * 会让 `findings` 里不再有盾的条目，消融 = 一行配置 diff。
 *
 * 与 Python 内核的关系（防两处实现漂移）：
 * 规则数据的唯一权威是 `code/data/t-irr.json`（由 `code/tools/derive_t_irr.py`
 * 从闸门源生成、带 sha256）。本目录的插件为保持零依赖内联了一份最小子集；
 * 它与权威数据是否一致由 `code/tools/check_plugin_data.py` 校验，
 * 不一致即失败（由 `check_plugin_data.py` 执行该校验），
 * 因为"插件内联表悄悄过期"这类漂移在运行时完全看不出来。
 *
 * @module ctd-acte/plugins/acte-protocol
 */

/** 盾的层标识。放在协议模块里，好让发射端与盾共用同一个名字（不各写一份）。 */
export const SHIELD_LAYER = 'irreversible-shield'

/**
 * 空裁决，作为 waterfall 最内层的默认值。
 *
 * `layers` 是参与本次裁决的层清单：每个监听者把自己的层名并进去
 * （见 `acte-shield.js` 与 `acte-plan.js` 的 `apply`）。它不是日志，而是
 * 消融的可观察性，没有它，发射端只看得到 `findings` 这一个结果，
 * 而"盾挂上了、本次无风险要报"与"盾根本没挂上"完全同形（两者都是空 findings）。
 * 于是 `no-shield` 消融下工具会静默放行，而"静默放行"正是本篇最防的一类失效。
 */
export const EMPTY_VERDICT = { findings: [], layers: [] }

/**
 * 宣告本层已挂载。
 *
 * 为什么要有这行输出：消融实验要能被观察到，挂载盾与消融盾，运行时的层集合
 * 必须不同。这里把层集合打到 stderr（确定性、可 grep、不受日志级别配置影响），
 * 作为消融生效的直接证据。"插件看着装上了、其实层是哑的"这类失效没有任何提示，
 * 故这行判定不是日志而是证据。
 *
 * @param {object} ctx - 插件上下文。
 * @param {string} layer - 层名。
 * @param {string} detail - 该层内联规则规模，便于核对加载的是哪一版。
 * @returns {void}
 */
export function announce(ctx, layer, detail) {
  const line = `acte: layer enabled — ${layer} (${detail})`
  process.stderr.write(line + '\n')
  const logger = ctx === undefined || ctx === null ? undefined : ctx.logger
  if (logger !== undefined && typeof logger.info === 'function') logger.info(line)
}

/**
 * 不可逆转移的两种结构（`method/formalization.md` §1.1.1）。
 *
 * 这两者的区别是实质的，且是本篇闸门与"拦截器"式闸门的根本差异：
 * 把不作为型当动作型处理，会让时效类风险全数漏防，因为"等待"在拦截器
 * 眼里从来不是"被拦下的动作"。实测依据：本引擎范围的三条 `T_irr` 里，
 * 两条是不作为型。
 */
export const KIND_ACTIVE = 'active'
export const KIND_PASSIVE = 'passive'

/** 不作为动作。显式列入动作集是 §1.1.1 的建模要求。 */
export const WAIT = '⊥'