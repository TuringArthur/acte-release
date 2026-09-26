#!/usr/bin/env python3
"""perf_report.py — 从会话产物提取护栏性能开销读数。

"零负担/常数时间"这一主张需要数据支撑。本脚本从**既有会话产物**（只读，
不改实验产物、不改 plugins 本体）提取五组读数，产出 JSON + Markdown 报表：

1. **每动作护栏判定延迟**（分层两口径）：
   - plan 提交→盾裁决（同一 submission）：v3/v4 journal 里提交与裁决记在
     **同一行**（`at` 是裁决落盘时刻），该间隔在日志里结构上不可观测；
     脚本按行分类（fused / submit / verdict），有拆行时按 submission 配对
     算 at 差，fused 行只计数并注明"以 --bench 微基准代替"。
   - 回合间隔：同一会话 journal 相邻行 `at` 差值（p50/p95/max）。
2. **每会话时长**：journal 首末行跨度；journal 不可用时退化为
   prompt→stdout 的 mtime 跨度（**上界**，因 prompt 备料可能早于开跑）。
   按 weak/clean × 臂（目录）分组。
3. **强制动作统计**：触发闸门次数、作为型拦截（irreversible_action_guard）、
   不作为催办（irreversible_omission_guard）、须确认入列数
   （severity=require_confirmation）、已确认数（submission.confirmed）。
   产出确认负荷表（须确认/会话、确认率）。
4. **Token/请求量估算**：产物未记录 token 用量 ⇒ 报回合数与请求数，
   注明"token 数无从取、以请求计"（诚实优先，不估单轮均量）。
5. **盾检查微基准**：`--bench` 时经 node 跑 `code/tools/bench_shield.mjs`
   （对 acte-shield.js 的 `check()` 计时，默认 10 万次/场景）。

## 对不完整目录的安全性（主实验 v4-* 正在陆续生成）

- 目录缺 manifest.tsv / run-env.model / journal / stdout：跳过并计数，不抛；
- manifest 行残缺、journal 行坏 JSON、时间戳不可解析：跳过并计数；
- 单会话处理包 try/except：一条会话坏了不影响其余；
- 报表尾部有"数据完整性"一节，把所有跳过计数摆出来——缺数要看得见。

## 用法

    python3 code/tools/perf_report.py                    # 扫 acte-sessions* 与 v4-*，
                                                        # 写 experiment/results/perf-report.{json,md}
    python3 code/tools/perf_report.py --arms acte-sessions,v4-main-k1
    python3 code/tools/perf_report.py --bench            # 附盾 check() 微基准（需 node）
    python3 code/tools/perf_report.py --bench --bench-n 200000

统计口径：分位数用最近秩（nearest-rank），不做插值；均值保留两位小数。
"""

import argparse
import csv
import json
import math
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── 仓库定位：本文件在 code/tools/ 下，仓库根是它的上两级 ──────────────
TOOL_PATH = Path(__file__).resolve()
PAPER_ROOT = TOOL_PATH.parents[2]
DEFAULT_RUNS_DIR = PAPER_ROOT / "experiment" / "run"
DEFAULT_OUT_DIR = PAPER_ROOT / "experiment" / "results"
BENCH_SCRIPT = TOOL_PATH.parent / "bench_shield.mjs"

# 默认扫描的臂（目录名前缀 glob）：v3 产物 + 正在生成的 v4 主实验。
DEFAULT_ARM_GLOBS = ("acte-sessions*", "v4-*")

# verdict findings 的判据字面量（与 code/plugins/acte-shield.js 一致；
# 这里只做计数，不复制判定逻辑——判定逻辑的单一来源在插件）。
CODE_ACTION_GUARD = "irreversible_action_guard"        # 动作型：拦（须确认）
CODE_OMISSION_GUARD = "irreversible_omission_guard"    # 不作为型：催办
CODE_CLOCK_CONFLICT = "irreversible_guard_clock_conflict"
CODE_NO_CLOCK = "irreversible_guard_no_clock"
CODE_UNREGISTERED = "unregistered_irreversible"
CODE_REVERSIBLE_OVERBLOCKED = "reversible_risk_overblocked"
SEV_REQUIRE_CONFIRMATION = "require_confirmation"


# ── 小工具 ─────────────────────────────────────────────────────────────

def quantile(sorted_vals, p):
    """最近秩分位数（不动插值——样本量小，插值会造出不存在的读数）。"""
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, math.ceil(p / 100.0 * len(sorted_vals)) - 1))
    return sorted_vals[idx]


def stats(values):
    """{n, mean, p50, p95, max}；空输入给 n=0 与全 None，不抛。"""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "max": None}
    return {
        "n": len(vals),
        "mean": round(sum(vals) / len(vals), 2),
        "p50": round(quantile(vals, 50), 2),
        "p95": round(quantile(vals, 95), 2),
        "max": round(vals[-1], 2),
    }


def parse_at(text):
    """解析 journal 的 `at`（UTC ISO，形如 2026-09-24T03:31:32.086Z）。"""
    if not isinstance(text, str) or not text:
        return None
    t = text.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def safe_mtime(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def round2(x, nd=2):
    return None if x is None else round(x, nd)


# ── 单会话取数 ─────────────────────────────────────────────────────────

class Counters:
    """跳过/异常计数。缺文件跳过必须计数——静默略过会把"没跑"读成"零开销"。"""

    def __init__(self):
        self.d = {}

    def bump(self, key, n=1):
        self.d[key] = self.d.get(key, 0) + n

    def snapshot(self):
        return dict(sorted(self.d.items()))


def read_journal(path, counters):
    """读一份 journal JSONL。坏行跳过并计数；返回 (rows, bad_lines)。"""
    rows = []
    bad = 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        counters.bump("journal_unreadable")
        return rows, bad
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            counters.bump("journal_bad_json_lines")
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            bad += 1
            counters.bump("journal_bad_json_lines")
    return rows, bad


def classify_row(row):
    """journal 行分类。

    - fused：提交与裁决同一行（v3/v4 实测形态）⇒ 提交→裁决间隔不可观测；
    - submit：只有 submission（拆行形态的提交行）；
    - verdict：只有 verdict（拆行形态的裁决行）；
    - other：两者皆无（不参与延迟配对，仍计入回合数）。
    """
    has_sub = isinstance(row.get("submission"), dict)
    has_ver = isinstance(row.get("verdict"), dict)
    if has_sub and has_ver:
        return "fused"
    if has_sub:
        return "submit"
    if has_ver:
        return "verdict"
    return "other"


def pair_submit_verdict(rows, counters):
    """拆行形态下按 submission 配对，算 提交→裁决 at 差（秒）。

    配对规则：verdict 行配最近一个未配对的 submit 行；`action_ids` 相同者优先。
    fused 行不参与（同一行内无第二个时间戳），只计数。
    """
    gaps = []
    pending = []  # [(dt, action_ids)]
    fused = 0
    for row in rows:
        kind = classify_row(row)
        if kind == "fused":
            fused += 1
            continue
        dt = parse_at(row.get("at"))
        if dt is None:
            counters.bump("journal_bad_at")
            continue
        acts = tuple(row.get("action_ids") or ())
        if kind == "submit":
            pending.append((dt, acts))
        elif kind == "verdict":
            if not pending:
                counters.bump("verdict_without_submit")
                continue
            idx = next((i for i, (_, a) in enumerate(pending) if a == acts),
                       len(pending) - 1)
            t0, _ = pending.pop(idx)
            gap_s = (dt - t0).total_seconds()
            if gap_s >= 0:
                gaps.append(gap_s * 1000.0)
            else:
                counters.bump("negative_submit_verdict_gap")
    counters.bump("submit_rows_left_unpaired", len(pending))
    return gaps, fused


def count_findings(row):
    """从一行里数 findings 的各类计数（只数数，不重跑判定）。"""
    out = {
        "triggered": 0,
        "action_blocks": 0,
        "omission_nudges": 0,
        "conf_queue": 0,
        "confirmed_items": 0,
        "confirmed_nonempty_submissions": 0,
        "unregistered": 0,
        "clock_conflict": 0,
        "no_clock": 0,
        "reversible_overblocked": 0,
        "shield_layer_findings": 0,
        "actions": 0,
        "final_true": 0,
    }
    sub = row.get("submission")
    if isinstance(sub, dict):
        trig = sub.get("triggered")
        out["triggered"] = len(trig) if isinstance(trig, list) else 0
        conf = sub.get("confirmed")
        n_conf = len(conf) if isinstance(conf, list) else 0
        out["confirmed_items"] = n_conf
        out["confirmed_nonempty_submissions"] = 1 if n_conf > 0 else 0
    acts = row.get("action_ids")
    out["actions"] = len(acts) if isinstance(acts, list) else 0
    if row.get("final") is True:
        out["final_true"] = 1
    verdict = row.get("verdict")
    findings = verdict.get("findings") if isinstance(verdict, dict) else None
    for f in (findings if isinstance(findings, list) else []):
        if not isinstance(f, dict):
            continue
        code = f.get("code")
        sev = f.get("severity")
        if code == CODE_ACTION_GUARD:
            out["action_blocks"] += 1
        elif code == CODE_OMISSION_GUARD:
            out["omission_nudges"] += 1
        elif code == CODE_CLOCK_CONFLICT:
            out["clock_conflict"] += 1
        elif code == CODE_NO_CLOCK:
            out["no_clock"] += 1
        elif code == CODE_UNREGISTERED:
            out["unregistered"] += 1
        elif code == CODE_REVERSIBLE_OVERBLOCKED:
            out["reversible_overblocked"] += 1
        if sev == SEV_REQUIRE_CONFIRMATION:
            out["conf_queue"] += 1
        if f.get("layer") == "irreversible-shield":
            out["shield_layer_findings"] += 1
    return out


def process_session(arm, kind, tid, row_fields, run_dir, counters):
    """取一条会话的全部读数。任何异常只记进 counters，不外抛。"""
    prompt_rel, out_rel, err_rel, journal_rel, deadline = row_fields
    sess = {
        "arm": arm,
        "kind": kind,
        "tid": tid,
        "deadline": None if deadline in ("", "-") else deadline,
        "turns": 0,
        "bad_lines": 0,
        "duration_s": None,
        "duration_source": None,
        "round_gaps_ms": [],
        "submit_verdict_gaps_ms": [],
        "fused_rows": 0,
        "session_state": None,
    }
    try:
        # 取数优先 run_dir 本目录（pre-v3 的 manifest 路径指向兄弟目录的产物，
        # 按 manifest 取会把别人的会话记到本臂头上）；manifest 路径兜底
        # （断点续跑时 journal 可能先于 manifest 出现在别处布局）。
        def resolve(rel, subdir, suffix):
            if rel in ("", "-"):
                rel = None
            cands = [run_dir / subdir / (tid + suffix)]
            if rel:
                cands.append(PAPER_ROOT / rel)
            for c in cands:
                if c.is_file():
                    return c
            return None

        prompt_p = resolve(prompt_rel, "prompts", ".txt")
        out_p = resolve(out_rel, "stdout", ".txt")
        err_p = resolve(err_rel, "stderr", ".log")
        journal_p = resolve(journal_rel, "journal", ".jsonl")

        if prompt_p is None:
            counters.bump("prompt_missing")
        if out_p is None:
            counters.bump("stdout_missing")
            sess["session_state"] = "pending_or_unstarted"
        else:
            try:
                sess["session_state"] = (
                    "complete" if out_p.stat().st_size > 0 else "stdout_empty")
                if out_p.stat().st_size == 0:
                    counters.bump("stdout_empty_files")
            except OSError:
                sess["session_state"] = "stat_failed"
        if err_p is None:
            counters.bump("stderr_missing")

        rows = []
        if journal_p is None:
            counters.bump("journal_missing")
        else:
            rows, sess["bad_lines"] = read_journal(journal_p, counters)
            if not rows:
                counters.bump("journal_empty")

        # ── 回合与延迟 ────────────────────────────────────────────
        sess["turns"] = len(rows)
        ats = []
        for r in rows:
            dt = parse_at(r.get("at"))
            if dt is None:
                counters.bump("journal_bad_at")
            else:
                ats.append(dt)
        for a, b in zip(ats, ats[1:]):
            gap_ms = (b - a).total_seconds() * 1000.0
            if gap_ms >= 0:
                sess["round_gaps_ms"].append(gap_ms)
            else:
                counters.bump("negative_round_gap")
        sv_gaps, fused = pair_submit_verdict(rows, counters)
        sess["submit_verdict_gaps_ms"] = sv_gaps
        sess["fused_rows"] = fused

        # ── 时长：journal 跨度优先，mtime 上界兜底 ─────────────────
        if len(ats) >= 2:
            sess["duration_s"] = (ats[-1] - ats[0]).total_seconds()
            sess["duration_source"] = "journal_span"
        elif len(ats) == 1:
            counters.bump("single_row_sessions")
            sess["duration_s"] = 0.0
            sess["duration_source"] = "journal_span_single_row"
        else:
            t0 = safe_mtime(prompt_p) if prompt_p else None
            t1 = None
            for p in (out_p, err_p):
                mt = safe_mtime(p) if p else None
                if mt is not None and (t1 is None or mt > t1):
                    t1 = mt
            if t0 is not None and t1 is not None and (t1 - t0).total_seconds() >= 0:
                sess["duration_s"] = (t1 - t0).total_seconds()
                sess["duration_source"] = "mtime_span_upper_bound"
                counters.bump("duration_from_mtime")
            else:
                counters.bump("duration_unavailable")

        # ── 强制动作统计 ──────────────────────────────────────────
        agg = {k: 0 for k in count_findings({})}
        for r in rows:
            c = count_findings(r)
            for k in agg:
                agg[k] += c[k]
        sess.update(agg)
    except Exception as exc:  # 单会话坏了不拖垮全报表
        counters.bump("session_processing_errors")
        sess["error"] = "%s: %s" % (type(exc).__name__, exc)
    return sess


# ── 分组聚合 ───────────────────────────────────────────────────────────

SUM_KEYS = (
    "turns", "triggered", "action_blocks", "omission_nudges", "conf_queue",
    "confirmed_items", "confirmed_nonempty_submissions", "unregistered",
    "clock_conflict", "no_clock", "reversible_overblocked",
    "shield_layer_findings", "actions", "final_true", "fused_rows",
)


def aggregate(sessions):
    """把若干会话记录聚成一组：时长分布、回合间隔、强制动作合计与每会话均值。"""
    g = {
        "n_sessions": len(sessions),
        "n_with_journal_rows": sum(1 for s in sessions if s.get("turns", 0) > 0),
        "duration_s": stats([s["duration_s"] for s in sessions
                             if s.get("duration_source") == "journal_span"]),
        "duration_s_all_sources": stats([s["duration_s"] for s in sessions
                                         if s.get("duration_s") is not None]),
        "duration_source_counts": {},
        "round_gaps_ms": stats([g for s in sessions
                                for g in s.get("round_gaps_ms", [])]),
        "submit_verdict_gaps_ms": stats([g for s in sessions
                                         for g in s.get("submit_verdict_gaps_ms", [])]),
    }
    for s in sessions:
        src = s.get("duration_source") or "unavailable"
        g["duration_source_counts"][src] = g["duration_source_counts"].get(src, 0) + 1
    for k in SUM_KEYS:
        total = sum(s.get(k, 0) for s in sessions)
        g[k + "_total"] = total
        g[k + "_per_session"] = round(total / len(sessions), 2) if sessions else None
    # 确认负荷：须确认/会话、确认率（已确认/须确认；单位见报表脚注）
    n = len(sessions)
    g["conf_queue_per_session"] = round(g["conf_queue_total"] / n, 2) if n else None
    g["confirmation_rate"] = (
        round(g["confirmed_items_total"] / g["conf_queue_total"], 3)
        if g["conf_queue_total"] else None)
    g["turns_per_session"] = stats([s.get("turns", 0) for s in sessions])
    return g


def group_key_sessions(sessions, key_fn):
    groups = {}
    for s in sessions:
        groups.setdefault(key_fn(s), []).append(s)
    return {k: aggregate(v) for k, v in sorted(groups.items())}


# ── 盾微基准（node 子进程）─────────────────────────────────────────────

def run_bench(n_iter):
    """跑 bench_shield.mjs。argv 是固定字面量（与 verify_all.py 同一纪律：
    子进程命令面不接受任何用户输入，杜绝命令注入面）。失败返回带 error 的
    dict 而不抛——微基准缺了不影响会话侧报表。"""
    node = shutil.which("node")
    if not node:
        return {"ok": False, "error": "找不到 node，微基准未执行"}
    if not BENCH_SCRIPT.is_file():
        return {"ok": False, "error": "找不到 %s" % BENCH_SCRIPT}
    argv = [node, str(BENCH_SCRIPT), str(n_iter)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              shell=False, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    if proc.returncode != 0:
        return {"ok": False,
                "error": "bench 退出码 %d：%s" % (proc.returncode,
                                               proc.stderr.strip()[:400])}
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        return {"ok": False, "error": "bench 输出不可解析：%s" % exc}
    data["ok"] = True
    return data


# ── 模型溯源 ───────────────────────────────────────────────────────────

def read_run_env(run_dir, counters):
    p = run_dir / "run-env.model"
    if not p.is_file():
        counters.bump("run_env_missing")
        return {"present": False}
    out = {"present": True}
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        counters.bump("run_env_unreadable")
    return out


# ── Markdown 渲染 ──────────────────────────────────────────────────────

def fmt(v, nd=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        return ("%%.%df" % nd) % v
    return str(v)


def stat_row(label, st, unit=""):
    return "| %s | %d | %s | %s | %s | %s |" % (
        label, st.get("n", 0), fmt(st.get("mean")), fmt(st.get("p50")),
        fmt(st.get("p95")), fmt(st.get("max")) + unit)


def render_markdown(report):
    md = []
    add = md.append
    add("# 护栏性能开销报表（perf_report）")
    add("")
    add("生成时间：%s ｜ 工具：`code/tools/perf_report.py` ｜ 数据：只读会话产物"
        "（experiment/run/）" % report["generated_at"])
    add("")
    add("主张对应关系：护栏开销由 §1 延迟读数、"
        "§5 盾检查微基准共同销账；§4 说明 token 口径的诚实限定。")
    add("")

    # §0 概览
    add("## 0. 概览")
    add("")
    add("- 扫描臂（目录）：%d 个（%s）" % (
        len(report["arms"]), "、".join(report["arms"])))
    add("- 会话行（manifest）：%d ｜ 有 journal 读数：%d ｜ 处理异常：%d" % (
        report["integrity"].get("sessions_listed", 0),
        report["overall"]["n_with_journal_rows"],
        report["integrity"].get("session_processing_errors", 0)))
    add("- 模型溯源：")
    for arm, env in sorted(report["model_provenance"].items()):
        if env.get("present"):
            add("  - `%s`：%s @ %s（记录于 %s）" % (
                arm, env.get("model_id", "?"), env.get("base_url_host", "?"),
                env.get("recorded_at", "?")))
        else:
            add("  - `%s`：无 run-env.model（跳过并计数）" % arm)
    add("")

    # §1 延迟
    add("## 1. 每动作护栏判定延迟")
    add("")
    add("### 1a. plan 提交→盾裁决（同一 submission）")
    add("")
    fused_total = report["integrity"].get("fused_rows_total", 0)
    sv = report["overall"]["submit_verdict_gaps_ms"]
    if sv["n"] > 0:
        add(stat_row("拆行配对（ms）", sv))
    else:
        add("journal 里提交与裁决记在**同一行**（fused 行共 %d 条），"
            "行内没有第二个时间戳 ⇒ 该间隔在会话日志里结构上不可观测。" % fused_total)
        add("单次盾判定的成本改由 §5 微基准给出（node 侧对 `check()` 计时）；"
            "若将来 journal 拆行，本节会自动按 submission 配对出读数。")
    add("")
    add("### 1b. 回合间隔（同一会话相邻 journal 行 at 差）")
    add("")
    add("| 组 | n | mean | p50 | p95 | max |")
    add("|---|---|---|---|---|---|")
    add(stat_row("ALL", report["overall"]["round_gaps_ms"], " ms"))
    for arm, g in sorted(report["by_arm"].items()):
        add(stat_row(arm, g["round_gaps_ms"], " ms"))
    add("")
    add("回合间隔以模型思考/生成时间为主，护栏判定（§5，微秒级）在其中不可见——"
        "这正是「零负担」的含义：回合间隔的量级由 LLM 决定，不因护栏而增长。")
    add("")

    # §2 时长
    add("## 2. 每会话时长（journal 首末行跨度）")
    add("")
    add("| 组 | n | mean | p50 | p95 | max |")
    add("|---|---|---|---|---|---|")
    add(stat_row("ALL（journal 跨度）", report["overall"]["duration_s"], " s"))
    for arm, g in sorted(report["by_arm"].items()):
        add(stat_row(arm, g["duration_s"], " s"))
    add("")
    add("按 weak/clean × 臂：")
    add("")
    add("| 臂 | kind | n | mean | p50 | p95 | max |")
    add("|---|---|---|---|---|---|---|")
    for key, g in sorted(report["by_arm_kind"].items()):
        arm, kind = key.split("|", 1)
        add("| %s | %s | %d | %s | %s | %s | %s s |" % (
            arm, kind, g["duration_s"]["n"], fmt(g["duration_s"]["mean"]),
            fmt(g["duration_s"]["p50"]), fmt(g["duration_s"]["p95"]),
            fmt(g["duration_s"]["max"])))
    add("")
    add("时长口径注记：n 只含 ≥2 行 journal 的会话（首末行跨度）；单行会话与"
        "空 journal 的计数见 §6。journal 不可用时的 mtime 兜底是**上界**"
        "（prompt 备料可能早于开跑），默认不计入上表（计在 `duration_s_all_sources`）。")
    add("")

    # §3 强制动作
    add("## 3. 强制动作统计（合计 / 每会话）")
    add("")
    add("| 组 | 触发闸门 | 作为型拦截 | 不作为催办 | 须确认入列 | 已确认 | 未登记告警 |")
    add("|---|---|---|---|---|---|---|")
    o = report["overall"]
    add("| ALL（合计） | %d | %d | %d | %d | %d | %d |" % (
        o["triggered_total"], o["action_blocks_total"], o["omission_nudges_total"],
        o["conf_queue_total"], o["confirmed_items_total"], o["unregistered_total"]))
    add("| ALL（每会话均值） | %s | %s | %s | %s | %s | %s |" % (
        fmt(o["triggered_per_session"]), fmt(o["action_blocks_per_session"]),
        fmt(o["omission_nudges_per_session"]), fmt(o["conf_queue_per_session"]),
        fmt(o["confirmed_items_per_session"]), fmt(o["unregistered_per_session"])))
    for arm, g in sorted(report["by_arm"].items()):
        add("| %s（合计） | %d | %d | %d | %d | %d | %d |" % (
            arm, g["triggered_total"], g["action_blocks_total"],
            g["omission_nudges_total"], g["conf_queue_total"],
            g["confirmed_items_total"], g["unregistered_total"]))
    add("")
    add("口径：作为型拦截 = findings `irreversible_action_guard`；不作为催办 = "
        "`irreversible_omission_guard`（盾语义上是催办不是拦截）；须确认入列 = "
        "severity `require_confirmation`（含时钟冲突项）；已确认 = 各 submission 的 "
        "`confirmed` 条目数（确认后放行的项在后续行不再报 finding，单行无法区分"
        "「从未触发」与「确认后放行」，故不用差值口径）。")
    add("")

    # §3b 确认负荷
    add("## 3b. 确认负荷表")
    add("")
    add("| 臂 | 会话数 | 须确认合计 | 须确认/会话 | 已确认 | 确认率 | 有确认声明的会话 |")
    add("|---|---|---|---|---|---|---|")
    for arm, g in sorted(report["by_arm"].items()):
        add("| %s | %d | %d | %s | %d | %s | %d |" % (
            arm, g["n_sessions"], g["conf_queue_total"],
            fmt(g["conf_queue_per_session"]), g["confirmed_items_total"],
            fmt(g["confirmation_rate"], 3), g["confirmed_nonempty_submissions_total"]))
    add("")
    add("确认率 = 已确认条目 / 须确认入列（两者单位分别是「已声明确认的条目」与"
        "「require_confirmation findings」，量纲相近但非同一集合；>1 表示"
        "确认来自先前会话/案情自带）。n 会话数含无 journal 读数的行（记 0）。")
    add("")

    # §4 请求量
    add("## 4. Token / 请求量估算")
    add("")
    rq = report["request_estimate"]
    add("- 回合数（acte_plan 提交次数）：%d（每会话 %s）" % (
        rq["rounds_total"], fmt(rq["rounds_per_session_mean"])))
    add("- 请求数：= 回合数 × 1 次护栏判定 = %d 次盾判定请求（每回合恰一次）" % (
        rq["shield_requests_total"]))
    add("- **token 数无从取、以请求计**：会话产物（journal/stdout/stderr）未记录 "
        "token 用量，脚本不估算单轮均量——估出来的均量没有依据，写进论文会被再问一次。")
    add("")

    # §5 微基准
    add("## 5. 盾检查微基准（acte-shield.js `check()`）")
    add("")
    bench = report.get("shield_microbench") or {}
    if bench.get("ok"):
        add("- node %s（%s）｜每场景 %d 次" % (
            bench.get("node"), bench.get("arch"), bench.get("iterations_per_scenario")))
        add("")
        add("| 场景 | n | mean | p50 | p95 | max | 吞吐（次/秒） |")
        add("|---|---|---|---|---|---|---|")
        for sc in bench.get("scenarios", []):
            us = sc["per_call_us"]
            add("| %s | %d | %s µs | %s µs | %s µs | %s µs | %s |" % (
                sc["scenario"], sc["iterations"], fmt(us["mean"], 3),
                fmt(us["p50"], 3), fmt(us["p95"], 3), fmt(us["max"], 1),
                fmt(sc["batch_throughput_per_s"], 0)))
        ov = bench.get("timer_overhead", {}).get("pair_overhead_ns", {})
        add("")
        add("计时器开销（hrtime 差分对）：p50 %s ns —— per-call 读数未扣该开销，"
            "在微秒级读数里可忽略不计。" % fmt(ov.get("p50"), 0))
        add("")
        add("hot = 触发全部分支（动作型未确认 + 两条不作为催办 + 未登记 + 时钟冲突）；"
            "cold = 零触发。论文引用以 hot 为上界口径。")
    else:
        add("微基准未执行：%s（用 `--bench` 重跑；需 node）" % bench.get("error", "?"))
    add("")

    # §6 完整性
    add("## 6. 数据完整性（跳过/异常计数——缺数要看得见）")
    add("")
    add("| 项 | 计数 |")
    add("|---|---|")
    for k, v in report["integrity"].items():
        add("| %s | %d |" % (k, v))
    add("")
    add("注：v4-* 目录在主实验中陆续生成，`journal_missing` / `stdout_missing` / "
        "`pending_or_unstarted` 属预期状态，不是错误；报表重跑即可刷新。")
    add("")
    add("### 注意事项（口径与数据陷阱）")
    add("")
    for note in report["notes"]:
        add("- %s" % note)
    add("")
    return "\n".join(md) + "\n"


# ── 主流程 ─────────────────────────────────────────────────────────────

def collect_sessions(runs_dir, arm_names, counters):
    sessions = []
    provenance = {}
    for arm in arm_names:
        run_dir = runs_dir / arm
        manifest = run_dir / "manifest.tsv"
        if not manifest.is_file():
            counters.bump("manifest_missing_dirs")
            continue
        provenance[arm] = read_run_env(run_dir, counters)
        try:
            lines = manifest.read_text(encoding="utf-8").splitlines()
        except OSError:
            counters.bump("manifest_unreadable")
            continue
        for lineno, line in enumerate(lines, 1):
            if not line.strip():
                continue
            cols = next(csv.reader([line], delimiter="\t"))
            if len(cols) < 7:
                counters.bump("manifest_bad_rows")
                continue
            if len(cols) < 8:
                # pre-v3 清单是 7 列（无 deadline 列）：补 '-' 占位照常取数。
                counters.bump("manifest_rows_7col")
                cols = cols + ["-"] * (8 - len(cols))
            kind, tid = cols[0].strip(), cols[1].strip()
            if not tid:
                counters.bump("manifest_bad_rows")
                continue
            counters.bump("sessions_listed")
            sessions.append(process_session(
                arm, kind, tid, cols[3:8], run_dir, counters))
    return sessions, provenance


def build_report(runs_dir, arm_names, bench_n, do_bench):
    counters = Counters()
    sessions, provenance = collect_sessions(runs_dir, arm_names, counters)

    # fused 行总数进 integrity（§1a 引用）
    counters.d["fused_rows_total"] = sum(s.get("fused_rows", 0) for s in sessions)

    by_arm = group_key_sessions(sessions, lambda s: s["arm"])
    by_arm_kind = group_key_sessions(
        sessions, lambda s: "%s|%s" % (s["arm"], s["kind"]))
    overall = aggregate(sessions)

    rounds_total = overall["turns_total"]
    n_sessions = len(sessions)

    notes = [
        "提交→盾裁决间隔在 journal 里不可观测（fused 行），单次判定成本见 §5 微基准",
        "回合间隔以 LLM 思考/生成时间为主，护栏判定（微秒级）不可见",
        "mtime 兜底的会话时长是上界（prompt 备料可能早于开跑）",
        "token 用量不在产物中，请求量以 acte_plan 提交次数计",
    ]
    # 数据陷阱自动标注：no-shield 消融臂若仍带 irreversible-shield 层 findings，
    # 说明消融没生效（旧驱动的已知失效，见 experiment/results/RERUN-NOSHIELD-
    # REPORT-2026-09-26.md）——照实计数但必须喊出来，否则会把无效消融读成效应。
    for arm, g in sorted(by_arm.items()):
        if "noshield" in arm and g.get("shield_layer_findings_total", 0) > 0:
            notes.append(
                "臂 `%s`：journal 里有 %d 条 irreversible-shield 层 findings ⇒ "
                "该目录的 no-shield 消融**未生效**（无效数据，见 "
                "experiment/results/RERUN-NOSHIELD-REPORT-2026-09-26.md）；"
                "对比 no-shield 效应用 `-rerun` 臂" % (
                    arm, g["shield_layer_findings_total"]))
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": "code/tools/perf_report.py",
        "runs_dir": str(runs_dir),
        "arms": arm_names,
        "model_provenance": provenance,
        "integrity": counters.snapshot(),
        "overall": overall,
        "by_arm": by_arm,
        "by_arm_kind": by_arm_kind,
        "sessions": sessions,
        "request_estimate": {
            "rounds_total": rounds_total,
            "rounds_per_session_mean": (
                round(rounds_total / n_sessions, 2) if n_sessions else None),
            "shield_requests_total": rounds_total,
            "tokens": None,
            "note": "token 数无从取、以请求计：产物未记录 token 用量，"
                    "请求数 = acte_plan 提交次数（每回合恰一次护栏判定）",
        },
        "shield_microbench": run_bench(bench_n) if do_bench else {
            "ok": False, "error": "未启用（加 --bench）"},
        "notes": notes,
    }
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR),
                    help="会话产物目录（默认 experiment/run/）")
    ap.add_argument("--arms", default="",
                    help="逗号分隔的臂（目录名）；默认 glob %s" % ",".join(DEFAULT_ARM_GLOBS))
    ap.add_argument("--out-json", default=str(DEFAULT_OUT_DIR / "perf-report.json"))
    ap.add_argument("--out-md", default=str(DEFAULT_OUT_DIR / "perf-report.md"))
    ap.add_argument("--bench", action="store_true",
                    help="跑盾检查微基准（node code/tools/bench_shield.mjs）")
    ap.add_argument("--bench-n", type=int, default=100000,
                    help="微基准每场景迭代次数（默认 100000）")
    args = ap.parse_args(argv)

    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_dir():
        raise SystemExit("找不到会话产物目录：%s" % runs_dir)

    if args.arms.strip():
        arm_names = [a.strip() for a in args.arms.split(",") if a.strip()]
    else:
        arm_names = sorted({p.name for pat in DEFAULT_ARM_GLOBS
                            for p in runs_dir.glob(pat) if p.is_dir()})
    if not arm_names:
        raise SystemExit("在 %s 下没有找到任何臂目录" % runs_dir)

    report = build_report(runs_dir, arm_names, args.bench_n, args.bench)

    out_json = Path(args.out_json)
    out_md = Path(args.out_md)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    out_md.write_text(render_markdown(report), encoding="utf-8")
    print("已写出 %s" % out_json)
    print("已写出 %s" % out_md)
    print("臂 %d ｜ 会话行 %d ｜ 有 journal 读数 %d ｜ 回合 %d ｜ 跳过计数见报表 §6"
          % (len(arm_names), report["integrity"].get("sessions_listed", 0),
             report["overall"]["n_with_journal_rows"],
             report["overall"]["turns_total"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
