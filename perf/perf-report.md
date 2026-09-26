# 护栏性能开销报表（perf_report）

生成时间：2026-09-27T08:18:08+00:00 ｜ 工具：`code/tools/perf_report.py` ｜ 数据：只读会话产物（experiment/run/）

主张对应关系：护栏开销由 §1 延迟读数、§5 盾检查微基准共同给出；§4 说明 token 口径的限定。

## 0. 概览

- 扫描臂（目录）：10 个（v4-main-k1、v4-main-k2、v4-main-k3、v4-main-k4、v4-main-k5、v4-noshield-k1、v4-noshield-k2、v4-noshield-k3、v4-noshield-k4、v4-noshield-k5）
- 会话行（manifest）：1510 ｜ 有 journal 读数：1300 ｜ 处理异常：0
- 模型溯源：
  - `v4-main-k1`：dots3-note-prev @ note3-prev-api.askdiandian.com（记录于 2026-09-26T22:07:37Z）
  - `v4-main-k2`：dots3-note-prev @ note3-prev-api.askdiandian.com（记录于 2026-09-26T23:22:49Z）
  - `v4-main-k3`：dots3-note-prev @ note3-prev-api.askdiandian.com（记录于 2026-09-27T00:37:15Z）
  - `v4-main-k4`：dots3-note-prev @ note3-prev-api.askdiandian.com（记录于 2026-09-26T23:24:19Z）
  - `v4-main-k5`：dots3-note-prev @ note3-prev-api.askdiandian.com（记录于 2026-09-27T00:42:20Z）
  - `v4-noshield-k1`：dots3-note-prev @ note3-prev-api.askdiandian.com（记录于 2026-09-26T22:52:58Z）
  - `v4-noshield-k2`：dots3-note-prev @ note3-prev-api.askdiandian.com（记录于 2026-09-27T05:19:11Z）
  - `v4-noshield-k3`：dots3-note-prev @ note3-prev-api.askdiandian.com（记录于 2026-09-27T06:07:12Z）
  - `v4-noshield-k4`：dots3-note-prev @ note3-prev-api.askdiandian.com（记录于 2026-09-27T04:50:10Z）
  - `v4-noshield-k5`：dots3-note-prev @ note3-prev-api.askdiandian.com（记录于 2026-09-27T05:46:26Z）

## 1. 每动作护栏判定延迟

### 1a. plan 提交→盾裁决（同一 submission）

journal 里提交与裁决记在**同一行**（fused 行共 2162 条），行内没有第二个时间戳 ⇒ 该间隔在会话日志里结构上不可观测。
单次盾判定的成本改由 §5 微基准给出（node 侧对 `check()` 计时）；若将来 journal 拆行，本节会自动按 submission 配对出读数。

### 1b. 回合间隔（同一会话相邻 journal 行 at 差）

| 组 | n | mean | p50 | p95 | max |
|---|---|---|---|---|---|
| ALL | 862 | 14174.74 | 9416.00 | 41642.00 | 144590.00 ms |
| v4-main-k1 | 100 | 12087.97 | 7916.00 | 31549.00 | 57471.00 ms |
| v4-main-k2 | 104 | 13980.22 | 9636.00 | 40191.00 | 91712.00 ms |
| v4-main-k3 | 96 | 13992.53 | 10202.00 | 46498.00 | 64404.00 ms |
| v4-main-k4 | 105 | 13427.90 | 9172.00 | 41307.00 | 61358.00 ms |
| v4-main-k5 | 97 | 12245.67 | 10240.00 | 28943.00 | 52731.00 ms |
| v4-noshield-k1 | 72 | 10227.58 | 6632.00 | 27685.00 | 42077.00 ms |
| v4-noshield-k2 | 68 | 16792.91 | 9760.00 | 46531.00 | 107198.00 ms |
| v4-noshield-k3 | 67 | 13026.88 | 8210.00 | 39548.00 | 60745.00 ms |
| v4-noshield-k4 | 77 | 23706.13 | 16388.00 | 89759.00 | 144590.00 ms |
| v4-noshield-k5 | 76 | 13662.72 | 8347.00 | 40378.00 | 103001.00 ms |

回合间隔以模型思考/生成时间为主，护栏判定（§5，微秒级）在其中不可见——这正是「零负担」的含义：回合间隔的量级由 LLM 决定，不因护栏而增长。

## 2. 每会话时长（journal 首末行跨度）

| 组 | n | mean | p50 | p95 | max |
|---|---|---|---|---|---|
| ALL（journal 跨度） | 744 | 16.42 | 10.20 | 52.84 | 144.59 s |
| v4-main-k1 | 79 | 15.30 | 10.25 | 46.52 | 84.01 s |
| v4-main-k2 | 86 | 16.91 | 9.98 | 66.10 | 91.71 s |
| v4-main-k3 | 83 | 16.18 | 11.96 | 51.14 | 64.40 s |
| v4-main-k4 | 82 | 17.19 | 11.72 | 52.52 | 81.72 s |
| v4-main-k5 | 78 | 15.23 | 13.04 | 38.84 | 59.04 s |
| v4-noshield-k1 | 68 | 10.83 | 4.81 | 27.68 | 91.30 s |
| v4-noshield-k2 | 68 | 16.79 | 9.76 | 46.53 | 107.20 s |
| v4-noshield-k3 | 64 | 13.64 | 6.42 | 42.03 | 67.01 s |
| v4-noshield-k4 | 67 | 27.24 | 16.07 | 91.50 | 144.59 s |
| v4-noshield-k5 | 69 | 15.05 | 6.43 | 62.76 | 103.00 s |

按 weak/clean × 臂：

| 臂 | kind | n | mean | p50 | p95 | max |
|---|---|---|---|---|---|---|
| v4-main-k1 | clean | 1 | 57.47 | 57.47 | 57.47 | 57.47 s |
| v4-main-k1 | weak | 78 | 14.76 | 10.02 | 44.23 | 84.01 s |
| v4-main-k2 | clean | 2 | 33.99 | 20.64 | 47.34 | 47.34 s |
| v4-main-k2 | weak | 84 | 16.50 | 9.79 | 66.10 | 91.71 s |
| v4-main-k3 | clean | 1 | 7.93 | 7.93 | 7.93 | 7.93 s |
| v4-main-k3 | weak | 82 | 16.28 | 11.96 | 51.14 | 64.40 s |
| v4-main-k4 | clean | 1 | 3.35 | 3.35 | 3.35 | 3.35 s |
| v4-main-k4 | weak | 81 | 17.37 | 11.96 | 52.52 | 81.72 s |
| v4-main-k5 | clean | 1 | 1.88 | 1.88 | 1.88 | 1.88 s |
| v4-main-k5 | weak | 77 | 15.40 | 13.76 | 38.84 | 59.04 s |
| v4-noshield-k1 | clean | 1 | 10.33 | 10.33 | 10.33 | 10.33 s |
| v4-noshield-k1 | weak | 67 | 10.84 | 4.81 | 27.68 | 91.30 s |
| v4-noshield-k2 | clean | 1 | 25.43 | 25.43 | 25.43 | 25.43 s |
| v4-noshield-k2 | weak | 67 | 16.66 | 9.76 | 46.53 | 107.20 s |
| v4-noshield-k3 | clean | 2 | 12.55 | 6.42 | 18.67 | 18.67 s |
| v4-noshield-k3 | weak | 62 | 13.67 | 5.07 | 42.03 | 67.01 s |
| v4-noshield-k4 | clean | 1 | 52.50 | 52.50 | 52.50 | 52.50 s |
| v4-noshield-k4 | weak | 66 | 26.86 | 15.72 | 91.50 | 144.59 s |
| v4-noshield-k5 | clean | 4 | 9.38 | 2.89 | 22.27 | 22.27 s |
| v4-noshield-k5 | weak | 65 | 15.40 | 6.43 | 62.76 | 103.00 s |

时长口径注记：n 只含 ≥2 行 journal 的会话（首末行跨度）；单行会话与空 journal 的计数见 §6。journal 不可用时的 mtime 兜底是**上界**（prompt 备料可能早于开跑），默认不计入上表（计在 `duration_s_all_sources`）。

## 3. 强制动作统计（合计 / 每会话）

| 组 | 触发闸门 | 作为型拦截 | 不作为催办 | 须确认入列 | 已确认 | 未登记告警 |
|---|---|---|---|---|---|---|
| ALL（合计） | 4042 | 159 | 258 | 159 | 767 | 1280 |
| ALL（每会话均值） | 2.68 | 0.11 | 0.17 | 0.11 | 0.51 | 0.85 |
| v4-main-k1（合计） | 419 | 27 | 47 | 27 | 98 | 267 |
| v4-main-k2（合计） | 415 | 30 | 52 | 30 | 82 | 267 |
| v4-main-k3（合计） | 370 | 25 | 51 | 25 | 68 | 236 |
| v4-main-k4（合计） | 411 | 41 | 55 | 41 | 89 | 242 |
| v4-main-k5（合计） | 433 | 36 | 53 | 36 | 82 | 268 |
| v4-noshield-k1（合计） | 390 | 0 | 0 | 0 | 77 | 0 |
| v4-noshield-k2（合计） | 414 | 0 | 0 | 0 | 59 | 0 |
| v4-noshield-k3（合计） | 388 | 0 | 0 | 0 | 74 | 0 |
| v4-noshield-k4（合计） | 415 | 0 | 0 | 0 | 56 | 0 |
| v4-noshield-k5（合计） | 387 | 0 | 0 | 0 | 82 | 0 |

口径：作为型拦截 = findings `irreversible_action_guard`；不作为催办 = `irreversible_omission_guard`（盾语义上是催办不是拦截）；须确认入列 = severity `require_confirmation`（含时钟冲突项）；已确认 = 各 submission 的 `confirmed` 条目数（确认后放行的项在后续行不再报 finding，单行无法区分「从未触发」与「确认后放行」，故不用差值口径）。

## 3b. 确认负荷表

| 臂 | 会话数 | 须确认合计 | 须确认/会话 | 已确认 | 确认率 | 有确认声明的会话 |
|---|---|---|---|---|---|---|
| v4-main-k1 | 151 | 27 | 0.18 | 98 | 3.630 | 73 |
| v4-main-k2 | 151 | 30 | 0.20 | 82 | 2.733 | 74 |
| v4-main-k3 | 151 | 25 | 0.17 | 68 | 2.720 | 62 |
| v4-main-k4 | 151 | 41 | 0.27 | 89 | 2.171 | 75 |
| v4-main-k5 | 151 | 36 | 0.24 | 82 | 2.278 | 74 |
| v4-noshield-k1 | 151 | 0 | 0.00 | 77 | — | 63 |
| v4-noshield-k2 | 151 | 0 | 0.00 | 59 | — | 54 |
| v4-noshield-k3 | 151 | 0 | 0.00 | 74 | — | 59 |
| v4-noshield-k4 | 151 | 0 | 0.00 | 56 | — | 55 |
| v4-noshield-k5 | 151 | 0 | 0.00 | 82 | — | 69 |

确认率 = 已确认条目 / 须确认入列（两者单位分别是「已声明确认的条目」与「require_confirmation findings」，量纲相近但非同一集合；>1 表示确认来自先前会话/案情自带）。n 会话数含无 journal 读数的行（记 0）。

## 4. Token / 请求量估算

- 回合数（acte_plan 提交次数）：2162（每会话 1.43）
- 请求数：= 回合数 × 1 次护栏判定 = 2162 次盾判定请求（每回合恰一次）
- **token 数无从取、以请求计**：会话产物（journal/stdout/stderr）未记录 token 用量，脚本不估算单轮均量——估出来的均量没有依据，写进论文会被再问一次。

## 5. 盾检查微基准（acte-shield.js `check()`）

- node v26.7.0（darwin-arm64）｜每场景 100000 次

| 场景 | n | mean | p50 | p95 | max | 吞吐（次/秒） |
|---|---|---|---|---|---|---|
| hot | 100000 | 0.948 µs | 0.459 µs | 1.333 µs | 275.4 µs | 1015392 |
| cold | 100000 | 0.198 µs | 0.167 µs | 0.250 µs | 128.6 µs | 4174959 |

计时器开销（hrtime 差分对）：p50 41 ns —— per-call 读数未扣该开销，在微秒级读数里可忽略不计。

hot = 触发全部分支（动作型未确认 + 两条不作为催办 + 未登记 + 时钟冲突）；cold = 零触发。论文引用以 hot 为上界口径。

## 6. 数据完整性（跳过/异常计数——缺数要看得见）

| 项 | 计数 |
|---|---|
| duration_from_mtime | 210 |
| fused_rows_total | 2162 |
| journal_empty | 210 |
| sessions_listed | 1510 |
| single_row_sessions | 556 |
| submit_rows_left_unpaired | 0 |

注：v4-* 目录在主实验中陆续生成，`journal_missing` / `stdout_missing` / `pending_or_unstarted` 属预期状态，不是错误；报表重跑即可刷新。

### 注意事项（口径与数据陷阱）

- 提交→盾裁决间隔在 journal 里不可观测（fused 行），单次判定成本见 §5 微基准
- 回合间隔以 LLM 思考/生成时间为主，护栏判定（微秒级）不可见
- mtime 兜底的会话时长是上界（prompt 备料可能早于开跑）
- token 用量不在产物中，请求量以 acte_plan 提交次数计

