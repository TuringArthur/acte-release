#!/usr/bin/env python3
"""从闸门注册表导出不可逆转移表 `T_irr`，并做穷尽校验。

## 这个工具解决什么

`method/formalization.md` 定理 1(a) 要求 `Shield` 只依赖 `⟨S, A, P, T_irr⟩`，
`T_irr` 必须是可识别的，不能靠人临场判断。本工具把 `T_irr` 固化成一份
带溯源指纹的 JSON 快照，供 ACTE 引擎读取。

## 为什么是"声明 + 校验"而不是"自动推断"

不可逆性不能从 `gates.yaml` 现有字段推出。`enforcement` 表达闸门力度，与不可逆
是两个独立维度，且互为反例：

* `gate.document-name-whitelist`：`block`，但名称写错可改了重提 ， 可逆；
* `gate.escalation-risk`：仅 `require_confirmation`，但处罚加重不可回卷 ， 不可逆。

故不可逆性由登记表侧在 `method/t-irr.yaml` 逐条声明；本工具的责任是强制穷尽：

1. 源里每条闸门必须在本表出现恰好一次（漏一条 → 报错退出）；
2. 本表每条闸门必须在源里存在（多了/改了 id → 报错退出）；
3. `t_irr` 类的每条必须给出 `kind`（`active`/`passive`）与 `criterion`（判据），
   且 `kind` 必须是二者之一（防止新增条目时忘写类型，静默变成"只限制不催办"）；
4. 源文件的 `sha256` 写进快照，闸门数变动即可被发现（`--check-drift`）。

第 1 条是这套设计的要害：规则库演进（新增一条闸门）时，本表不会静默过期，
而是直接把构建打断，这正是"插件看着装上了其实层是哑的"一类静默失效的同类防护。

## 用法

    python3 code/tools/derive_t_irr.py --source <path>/rules/gates.yaml \\
                                       --registry method/t-irr.yaml \\
                                       --out code/data/t-irr.json

    python3 code/tools/derive_t_irr.py --check-drift   # 只校验快照与源/表是否一致

源路径也可用环境变量 `CTD_LA_GATES` 给出（不写死在代码里：`code/` 是独立导出单元，
不得对仓库其他部分存在必需的相对引用）。

只依赖标准库 + PyYAML。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 PyYAML：python3 -m pip install pyyaml\n")
    raise SystemExit(2)

SNAPSHOT_VERSION = 2

# 不可逆转移的两种结构（formalization.md §1.1.1）。
# active  = 己方做了某动作而跨层级（盾只需限制）
# passive = 己方未做该做的动作而跨层级（盾须强制，即拿掉 ⊥）
VALID_KINDS = ("active", "passive")

# ── 法条级程序期限节点目录（method/deadline-nodes.yaml）的校验口径 ──────
# 期限节点目录把"覆盖"从 18 条行为闸门扩展为**法条级程序期限节点**，每个节点带
# 触发条件 / 法律依据（精确到条）/ 后果 / 期间性质 / 不可逆判定（分层）/ 作为型。
DEADLINE_PERIOD_TYPES = ("诉讼时效", "除斥期间", "不变期间", "失权期限", "指定期间")
DEADLINE_STAGES = ("立案", "审前", "审理", "裁判", "执行", "特别程序", "实体救济")


class RegistryError(RuntimeError):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _gate_ids_from_source(source):
    """源里全部闸门 id（按出现次序）。"""
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RegistryError("闸门源不是映射结构：%s" % source)
    gates = raw.get("gates")
    if not isinstance(gates, list) or not gates:
        raise RegistryError("闸门源缺少非空的 `gates` 列表：%s" % source)
    ids = []
    for i, g in enumerate(gates):
        if not isinstance(g, dict) or not g.get("id"):
            raise RegistryError("第 %d 条闸门缺少 `id`" % (i + 1))
        ids.append(g["id"])
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    if dupes:
        raise RegistryError("闸门源存在重复 id：%s" % ", ".join(dupes))
    return ids, raw


def _collect_registry(registry):
    """把登记表折成 {gate_id: {'bucket':…, …}} 并做结构校验。"""
    if not isinstance(registry, dict):
        raise RegistryError("登记表不是映射结构")
    entries = {}
    for bucket in ("t_irr", "not_t_irr", "out_of_scope"):
        items = registry.get(bucket)
        if not isinstance(items, list):
            raise RegistryError("登记表缺少 `%s` 列表" % bucket)
        for item in items:
            if not isinstance(item, dict) or not item.get("gate_id"):
                raise RegistryError("`%s` 中存在缺少 `gate_id` 的条目" % bucket)
            gid = item["gate_id"]
            if gid in entries:
                raise RegistryError("闸门 `%s` 在登记表中出现多次" % gid)
            entries[gid] = {"bucket": bucket, **item}
    return entries


def _validate(t_irr_entries, source_ids, registry_raw):
    """穷尽校验。任一不通过即抛错，不静默放宽。"""
    problems = []

    missing = [g for g in source_ids if g not in t_irr_entries]
    if missing:
        problems.append(
            "源里有 %d 条闸门未在 method/t-irr.yaml 分类：%s\n"
            "  → 新增闸门必须补分类（漏分类会让 T_irr 静默过期，故直接报错）"
            % (len(missing), ", ".join(missing))
        )

    extra = sorted(set(t_irr_entries) - set(source_ids))
    if extra:
        problems.append(
            "登记表里有 %d 条闸门不在源中：%s\n"
            "  → 闸门被删除或改名后，登记表须同步更新" % (len(extra), ", ".join(extra))
        )

    for gid, item in sorted(t_irr_entries.items()):
        if item["bucket"] != "t_irr":
            continue
        kind = item.get("kind")
        if kind not in VALID_KINDS:
            problems.append(
                "`%s` 的 `kind` 非法（%r）；须为 %s 之一"
                % (gid, kind, " / ".join(VALID_KINDS))
            )
        if not (item.get("criterion") or "").strip():
            problems.append("`%s` 缺少 `criterion`（判据），无法复核" % gid)
        if item.get("in_scope") is not True:
            problems.append(
                "`%s` 在 `t_irr` 桶内但 `in_scope` 非 true——两者必须一致" % gid
            )

    # 期望条数（若登记表声明了）
    expected = ((registry_raw.get("derived_from") or {}).get("expected_gate_count"))
    if expected is not None and expected != len(source_ids):
        problems.append(
            "登记表声明 expected_gate_count=%s，但源里有 %d 条闸门"
            % (expected, len(source_ids))
        )

    if problems:
        raise RegistryError("T_irr 校验失败（%d 项）：\n\n" % len(problems)
                            + "\n\n".join(problems))


def _validate_deadline_catalog(raw, path):
    """校验法条级程序期限节点目录的结构与两条判定不变量，返回节点列表。

    重点是把期间性质的区分变成**可执行的机械校验**（不是靠人记得区分）：

    * `period_type=诉讼时效`（可中止/中断/延长、抗辩权发生说）⇒ `is_irreversible` 必须为 false；
      若判为不可逆即报错——这正是把时效当成不变期间的那个经典错误。
    * `period_type=除斥期间` / `不变期间`（不可中止/中断、届满失权/权利消灭）⇒ 必须为 true。

    任一不通过即抛错退出，与 T_irr 的穷尽校验同一纪律：不静默放宽。
    """
    problems = []
    nodes = raw.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise RegistryError("期限目录缺少非空的 `nodes` 列表：%s" % path)
    seen = set()
    for i, n in enumerate(nodes):
        if not isinstance(n, dict) or not n.get("id"):
            raise RegistryError("第 %d 条期限节点缺少 `id`" % (i + 1))
        label = n["id"]
        if label in seen:
            problems.append("期限节点 id 重复：%s" % label)
        seen.add(label)

        if n.get("stage") not in DEADLINE_STAGES:
            problems.append("%s：`stage` 非法（%r），须为 %s"
                            % (label, n.get("stage"), " / ".join(DEADLINE_STAGES)))

        lb = n.get("legal_basis")
        if not isinstance(lb, list) or not lb:
            problems.append("%s：缺 `legal_basis`（每条须精确到条）" % label)
        else:
            for b in lb:
                if not isinstance(b, dict) or not (b.get("statute") or "").strip() \
                        or not (b.get("article") or "").strip():
                    problems.append("%s：`legal_basis` 条目缺 statute/article（须精确到条）" % label)

        for f in ("name", "trigger", "deadline", "consequence"):
            if not (n.get(f) or "").strip():
                problems.append("%s：缺 `%s`" % (label, f))

        pt = n.get("period_type")
        if pt not in DEADLINE_PERIOD_TYPES:
            problems.append("%s：`period_type` 非法（%r），须为 %s"
                            % (label, pt, " / ".join(DEADLINE_PERIOD_TYPES)))

        irr = n.get("irreversibility")
        if not isinstance(irr, dict):
            problems.append("%s：缺 `irreversibility`" % label)
        else:
            is_irr = irr.get("is_irreversible")
            if not isinstance(is_irr, bool):
                problems.append("%s：`is_irreversible` 须为布尔" % label)
            kind = irr.get("kind")
            if is_irr is True and kind not in ("active", "passive"):
                problems.append("%s：is_irreversible=true 但 kind=%r（须 active/passive）" % (label, kind))
            if is_irr is False and kind not in ("none", "active", "passive"):
                problems.append("%s：`kind` 非法（%r）" % (label, kind))
            if not (irr.get("reason") or "").strip():
                problems.append("%s：`irreversibility.reason` 缺失——必须写清可逆/不可逆理由" % label)
            # 机械不变量：期间性质必须与可逆判定一致
            if pt == "诉讼时效" and is_irr is not False:
                problems.append(
                    "%s：period_type=诉讼时效（可中止/中断/延长、抗辩权发生说）却判为不可逆"
                    "——时效不是真不可逆" % label)
            if pt in ("除斥期间", "不变期间") and is_irr is not True:
                problems.append(
                    "%s：period_type=%s（不可中止/中断、届满失权/权利消灭）却判为可逆" % (label, pt))

        ver = n.get("verification")
        if not isinstance(ver, dict) or ver.get("status") not in ("verified", "待核"):
            problems.append("%s：`verification.status` 须为 verified / 待核" % label)
        elif not (ver.get("source") or "").strip():
            problems.append("%s：`verification.source` 缺失" % label)

    if problems:
        raise RegistryError("期限目录校验失败（%d 项）：\n\n" % len(problems)
                            + "\n\n".join(problems))
    return nodes


def _build_deadline_section(deadline_path):
    """把期限目录折成快照的 `deadline_nodes` + `coverage` 区块。"""
    raw = yaml.safe_load(deadline_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RegistryError("期限目录不是映射结构：%s" % deadline_path)
    nodes = _validate_deadline_catalog(raw, deadline_path)

    from collections import Counter
    by_stage = Counter(n["stage"] for n in nodes)
    by_period = Counter(n["period_type"] for n in nodes)
    n_irr = sum(1 for n in nodes if n["irreversibility"]["is_irreversible"] is True)
    n_rev = sum(1 for n in nodes if n["irreversibility"]["is_irreversible"] is False)
    n_active = sum(1 for n in nodes if n["irreversibility"].get("kind") == "active")
    n_passive = sum(1 for n in nodes if n["irreversibility"].get("kind") == "passive")
    verified = [n for n in nodes if n["verification"]["status"] == "verified"]
    pending = [n for n in nodes if n["verification"]["status"] == "待核"]

    return {
        "deadline_nodes": [
            {
                "id": n["id"],
                "stage": n["stage"],
                "name": n["name"],
                "trigger": (n.get("trigger") or "").strip(),
                "legal_basis": n.get("legal_basis"),
                "deadline": (n.get("deadline") or "").strip(),
                "consequence": (n.get("consequence") or "").strip(),
                "period_type": n.get("period_type"),
                "is_irreversible": n["irreversibility"]["is_irreversible"],
                "kind": n["irreversibility"].get("kind"),
                "level_change": (n["irreversibility"].get("level_change") or "").strip() or None,
                "reason": (n["irreversibility"].get("reason") or "").strip(),
                "verification_status": n["verification"]["status"],
                "verification_source": (n["verification"].get("source") or "").strip(),
            }
            for n in nodes
        ],
        "coverage": {
            "n_nodes": len(nodes),
            "n_irreversible": n_irr,
            "n_reversible": n_rev,
            "n_active": n_active,
            "n_passive": n_passive,
            "n_verified": len(verified),
            "n_pending_verification": len(pending),
            "by_stage": dict(sorted(by_stage.items())),
            "by_period_type": dict(sorted(by_period.items())),
            "pending_verification_ids": [n["id"] for n in pending],
            "note_passive_dominance": (
                "期限节点的不可逆几乎全为**不作为型**（漏期限即失权）：这不是登记疏漏，"
                "而是期限风险的结构本质——它正说明把闸门一律实现为『拦截器』（只拦动作）"
                "会全数漏防期限类失权（formalization.md §1.1.1 的核心论证）。"
                "作为型的不可逆（如 gate.escalation-risk 的起诉引致加重）在行为闸门侧，不在期限节点侧。"
            ),
        },
        "provenance_deadline": {
            "deadline_source_basename": deadline_path.name,
            "deadline_source_sha256": sha256_file(deadline_path),
        },
    }


def build_snapshot(source, registry_path, deadline_path=None):
    source_ids, source_raw = _gate_ids_from_source(source)
    registry_raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    entries = _collect_registry(registry_raw)
    _validate(entries, source_ids, registry_raw)

    # 按源的次序输出，便于与 gates.yaml 逐行对照
    t_irr = [entries[g] for g in source_ids if entries[g]["bucket"] == "t_irr"]
    exclusions = [
        {"gate_id": entries[g]["gate_id"],
         "bucket": entries[g]["bucket"],
         "reason": (entries[g].get("reason") or entries[g].get("note") or "").strip(),
         "domain": entries[g].get("domain")}
        for g in source_ids if entries[g]["bucket"] != "t_irr"
    ]

    snap = {
        "snapshot_version": SNAPSHOT_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provenance": {
            "source_basename": source.name,
            "source_sha256": sha256_file(source),
            "registry_basename": registry_path.name,
            "registry_sha256": sha256_file(registry_path),
            "gates_version": source_raw.get("version"),
            "n_gates_source": len(source_ids),
            "note": (
                "由 code/tools/derive_t_irr.py 生成。源路径不入库"
                "（本仓库内规范源为 rules/gates.yaml）。不可逆性为**显式声明**，"
                "非从 gates 字段自动推断——理由见 method/t-irr.yaml 头部说明。"
                "程序期限节点见 method/deadline-nodes.yaml（法条级期限节点目录）。"
            ),
        },
        "level_semantics": (registry_raw.get("derived_from") or {}).get("level_semantics"),
        # 定理 1 的 Shield 由这三个条目导出
        "t_irr": [
            {
                "gate_id": e["gate_id"],
                "yaojue": e.get("yaojue"),
                "kind": e["kind"],
                "action": (e.get("action") or "").strip() or None,
                "level_change": (e.get("level_change") or "").strip() or None,
                "criterion": (e.get("criterion") or "").strip(),
                "source_page": e.get("source_page"),
                "rule_ref": e.get("rule_ref"),
            }
            for e in t_irr
        ],
        # 被排除的闸门连同理由一并入库：读者要能复核"为什么它不是不可逆"
        "excluded": exclusions,
    }

    # ── 法条级程序期限节点目录 → 快照的 deadline_nodes / coverage 区块 ──
    # 刻意与上面的 18 条闸门 T_irr **分开**：前者是行为闸门的不可逆判定（喂盾），
    # 后者是程序期限节点的覆盖与不可逆判定。两者口径不同，
    # 合并会让"行为闸门"与"期限节点"的边界消失；分开则各自可复核、互不破链。
    if deadline_path is not None:
        dsec = _build_deadline_section(Path(deadline_path))
        snap["deadline_nodes"] = dsec["deadline_nodes"]
        snap["coverage"] = dsec["coverage"]
        snap["provenance"].update(dsec["provenance_deadline"])
    return snap


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=os.environ.get("CTD_LA_GATES"),
                    help="闸门注册表路径（rules/gates.yaml）；也可用 CTD_LA_GATES")
    ap.add_argument("--registry",
                    default=str(Path(__file__).resolve().parents[2] / "method" / "t-irr.yaml"),
                    help="不可逆转移登记表（默认 method/t-irr.yaml）")
    ap.add_argument("--deadline",
                    default=str(Path(__file__).resolve().parents[2] / "method" / "deadline-nodes.yaml"),
                    help="法条级程序期限节点目录（默认 method/deadline-nodes.yaml）；"
                         "缺省且文件存在时并入快照的 deadline_nodes/coverage 区块")
    ap.add_argument("--out",
                    default=str(Path(__file__).resolve().parents[2] / "code" / "data" / "t-irr.json"),
                    help="输出快照 JSON 路径")
    ap.add_argument("--check-drift", action="store_true",
                    help="只校验：快照存在且与当前源/登记表一致，不写盘")
    args = ap.parse_args(argv)

    if not args.source:
        ap.error("缺少 --source（或环境变量 CTD_LA_GATES）")
    source = Path(args.source)
    registry_path = Path(args.registry)
    # 期限目录是**可选并入**：存在则并入快照的 deadline_nodes/coverage 区块，
    # 缺失（如独立导出的 code/ 仓）则跳过该区块，不阻断闸门侧派生（保持 code/ 自足）。
    deadline_path = Path(args.deadline) if args.deadline else None
    if deadline_path is not None and not deadline_path.is_file():
        deadline_path = None
    if not source.is_file():
        ap.error("闸门源不存在：%s" % source)
    if not registry_path.is_file():
        ap.error("登记表不存在：%s" % registry_path)

    try:
        snapshot = build_snapshot(source, registry_path, deadline_path)
    except RegistryError as exc:
        sys.stderr.write("\n%s\n" % exc)
        return 1

    out = Path(args.out)
    if args.check_drift:
        if not out.is_file():
            sys.stderr.write("快照不存在：%s\n" % out)
            return 1
        old = json.loads(out.read_text(encoding="utf-8"))
        old_prov = old.get("provenance") or {}
        new_prov = snapshot["provenance"]
        drift = [k for k in ("source_sha256", "registry_sha256", "n_gates_source",
                             "deadline_sha256")
                 if old_prov.get(k) != new_prov.get(k)]
        if drift:
            sys.stderr.write("漂移：%s 与当前源不一致（%s）\n"
                             % (out, ", ".join(drift)))
            return 1
        print("无漂移：%s 与当前闸门源、登记表一致。" % out)
        print("  T_irr %d 条 / 排除 %d 条"
              % (len(snapshot["t_irr"]), len(snapshot["excluded"])))
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")

    n_active = sum(1 for e in snapshot["t_irr"] if e["kind"] == "active")
    n_passive = len(snapshot["t_irr"]) - n_active
    print("T_irr 快照已写出：%s" % out)
    print("  闸门源 %s（%d 条，sha256 %s…）"
          % (source.name, snapshot["provenance"]["n_gates_source"],
             snapshot["provenance"]["source_sha256"][:16]))
    print("  T_irr %d 条：动作型 %d / 不作为型 %d"
          % (len(snapshot["t_irr"]), n_active, n_passive))
    print("  已排除 %d 条（理由随快照入库，可逐条复核）" % len(snapshot["excluded"]))
    cov = snapshot.get("coverage")
    if cov:
        print("  期限节点 %d 条：不可逆 %d / 可逆 %d（作为型 %d / 不作为型 %d）"
              % (cov["n_nodes"], cov["n_irreversible"], cov["n_reversible"],
                 cov["n_active"], cov["n_passive"]))
        print("    已核 %d / 待核 %d；阶段分布：%s"
              % (cov["n_verified"], cov["n_pending_verification"],
                 "、".join("%s×%d" % (k, v) for k, v in cov["by_stage"].items())))
    else:
        print("  期限目录未并入（deadline-nodes.yaml 缺失），仅生成闸门侧 T_irr")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())