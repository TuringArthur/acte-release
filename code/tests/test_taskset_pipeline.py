#!/usr/bin/env python3
"""任务集流水线回归测试：生成器的守卫 + 端到端判定。

首先运行它：它红了，任务集的数字就不可信。

覆盖两段：

1. 生成器有牙（§A）：把"任一锚点未命中即报错退出"这句声称变成可执行检查。
   坏输入：空注入计划 / 锚点未命中 / 锚点不唯一 / 痕迹与注入不对应 / 全 lexeme 痕迹。
   每条必须退出码非零。理由与 `test_t_irr.py` 相同：一个从不报错的校验器形同虚设，
   而这里的静默失效尤其隐蔽，任务集照常生成、实验照常跑，只是那个实例的金标准
   判的不是本次注入。

2. 端到端判定（§B）：用生成出来的签名（不是手写的）判标定输出。
   若只测手写签名，测的是"我会写签名"，不是"流水线产出的签名能用"。

不需要规则源：§A 用内联的最小目录/底本，故 `code/` 独立导出后仍可运行。

    python3 code/tests/test_taskset_pipeline.py
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
PAPER_ROOT = HERE.parents[2]
CODE_ROOT = PAPER_ROOT / "code"
TASKS_ROOT = PAPER_ROOT / "experiment" / "tasks"

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 PyYAML：python3 -m pip install pyyaml\n")
    raise SystemExit(2)

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 判定机路径按统一解析器取：环境变量 → 本仓钉版副本 → 上层仓库内的冻结副本
# （见 `code/tools/matcher_path.py`）。此处不写 `parents[N]`：独立导出的仓库里
# 不在上位仓库里时靠钉版副本工作，路径深度也不再固定。
_matcher_path = _load("f1_matcher_path", CODE_ROOT / "tools" / "matcher_path.py")
MATCHER_PATH = _matcher_path.resolve(CODE_ROOT, HERE)


class Case:
    def __init__(self):
        self.n, self.failed = 0, []

    def check(self, name, cond, detail=""):
        self.n += 1
        if cond:
            print("  ok   %s" % name)
        else:
            print("  FAIL %s%s" % (name, ("  — " + detail) if detail else ""))
            self.failed.append(name)


# ── 内联最小夹具（不依赖真实目录）─────────────────────────────
MINIMAL_WEAKNESS = {
    "id": "W-T-01", "class": "limitation",
    "grounded_in": ["test-fixture"],
    "carrier": "日期", "trigger": "超期",
    "trace": {"kind": "pattern", "atomic": True},
    "consequence": "时效已过", "assertion": {"require_unquotable": True, "alts": ["时效已过"]},
}
BASE_FACTS = "案件事实：2022年6月1日，原告甲知悉被侵害。原告甲拟起诉被告乙。"
BASE_CASE = {"base_case_id": "T-001", "case_type": "civil", "family": "M1",
             "facts_text": BASE_FACTS, "fields": {}}


def _fixture(tmp, *, weakness=None, injection=None, keep_base=True):
    root = Path(tmp)
    (root / "cases" / "base").mkdir(parents=True, exist_ok=True)
    if keep_base:
        (root / "cases" / "base" / "T-001.json").write_text(
            json.dumps(BASE_CASE, ensure_ascii=False), encoding="utf-8")
    doc = {
        "version": 1,
        "catalogue": [copy.deepcopy(weakness or MINIMAL_WEAKNESS)],
        "injections": [] if injection is None else [copy.deepcopy(injection)],
    }
    wpath = root / "weaknesses.yaml"
    wpath.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return root


def _ok_injection():
    return {
        "injection_id": "T-INJ-1", "weakness_id": "W-T-01", "base_case": "T-001",
        "family": "M1", "posture": "细", "task_kind": "case_assessment",
        "field": "知悉日",
        "from": "2022年6月1日", "to": "2019年6月1日",
    }


def test_generator_has_teeth(c):
    print("\n[A] 生成器有牙（五种坏输入必须报错）")
    tool = _load("f1_make_taskset", CODE_ROOT / "tools" / "make_taskset.py")
    matcher = _load("f1_matcher", MATCHER_PATH)

    def run(root, allow_empty=False):
        try:
            tool.build(matcher, root)
            return None
        except tool.InjectError as exc:
            return exc
        except Exception as exc:  # 非预期异常也算失败（含 matcher.SignatureError 未被包装）
            return exc

    with tempfile.TemporaryDirectory() as tmp:
        # 基线：良构必须通过，否则后面的"拦截"没有意义
        root = _fixture(tmp, injection=_ok_injection())
        exc = run(root)
        c.check("基线：良构注入通过", exc is None, repr(exc))

        # ① 空注入计划
        root = _fixture(Path(tmp) / "e1", injection=None)
        exc = run(root)
        c.check("① 空注入计划被拒（不产出空任务集）",
                exc is not None and "注入计划为空" in str(exc), repr(exc))

        # ② 锚点未命中
        inj = _ok_injection(); inj["from"] = "2033年1月1日"
        root = _fixture(Path(tmp) / "e2", injection=inj)
        exc = run(root)
        c.check("② 锚点未命中被拦", exc is not None and "锚点未命中" in str(exc), repr(exc))

        # ③ 锚点不唯一
        root = _fixture(Path(tmp) / "e3", injection=_ok_injection())
        base = json.loads((root / "cases" / "base" / "T-001.json").read_text("utf-8"))
        base["facts_text"] = "案件事实：2022年6月1日，原告甲知悉。2022年6月1日，原告甲再确认。"
        (root / "cases" / "base" / "T-001.json").write_text(
            json.dumps(base, ensure_ascii=False), encoding="utf-8")
        exc = run(root)
        c.check("③ 锚点不唯一被拦", exc is not None and "锚点不唯一" in str(exc), repr(exc))

        # ④ 痕迹 regex 与注入文本不对应
        w = copy.deepcopy(MINIMAL_WEAKNESS)
        w["trace"] = {"kind": "pattern", "regex": "与本次注入无关的措辞"}
        root = _fixture(Path(tmp) / "e4", weakness=w, injection=_ok_injection())
        exc = run(root)
        c.check("④ 痕迹与注入不对应被拦",
                exc is not None and "没有命中" in str(exc), repr(exc))

        # ⑤ 痕迹尽是 lexeme ⇒ definitional，tier2 永不命中
        w = copy.deepcopy(MINIMAL_WEAKNESS)
        w["trace"] = {"kind": "lexeme", "alts": ["违约责任", "侵权责任"]}
        root = _fixture(Path(tmp) / "e5", weakness=w, injection=_ok_injection())
        exc = run(root)
        c.check("⑤ 全 lexeme 痕迹被拦（否则该弱点恒判漏检）",
                exc is not None and "definitional" in str(exc), repr(exc))


def test_end_to_end(c):
    print("\n[B] 端到端：用**生成出来的**签名判标定输出")

    tool = _load("f1_make_taskset_e2e", CODE_ROOT / "tools" / "make_taskset.py")
    matcher = _load("f1_matcher_e2e", MATCHER_PATH)

    taskset_path = TASKS_ROOT / "taskset.json"
    if not taskset_path.is_file():
        # 算法本体发布的仓库不含任务集：§B 无从执行，§A（生成器守卫，用内联夹具）
        # 仍然有效。这里显式说明并返回，而不是记为失败。
        print("  [B] 不适用：本仓库不含任务集（缺 %s）" % taskset_path)
        return
    taskset = json.loads(taskset_path.read_text(encoding="utf-8"))
    if not taskset["instances"]:
        c.check("任务集非空", False, "0 实例")
        return

    import re

    class F:
        def __init__(self, texts):
            self.evidence, self.locus, self.message = texts

    # 逐实例核对（不只第一条）：每条都要满足三条不变量
    for inst in taskset["instances"]:
        tid = inst["task_id"]
        sig = inst["expected"]["signature"]
        weak = json.loads((TASKS_ROOT / inst["weak_file"]).read_text(encoding="utf-8"))
        weak_text = weak["facts_text"]
        base = json.loads((TASKS_ROOT / "cases" / "base" / ("%s.json" % inst["base_case"]))
                          .read_text(encoding="utf-8"))
        clean_text = base["facts_text"]
        injected = inst["injected"][0]
        added = injected["to"]

        # ① 干净件不含痕迹、弱点件含痕迹（生成期已核验，此处复核产出）
        grounded = [cl for cl in sig["clauses"] if cl.get("grounded")]
        if not grounded:
            c.check("%s：有 grounded 子句" % tid, False, "全 definitional ⇒ tier2 永不命中")
            continue
        pat_list = [t for t in grounded[0]["terms"] if t["kind"] == "pattern"]
        if not pat_list:
            c.check("%s：grounded 子句含 pattern" % tid, False, "（witness 型不在本测试覆盖内）")
            continue
        # 任务集 v3 起痕迹子句可含多项（子句内项间是"与"，如 W-LIM-03 的
        # 原子日期 + 改写措辞）：①与③都必须逐项处理，只取第一项会假绿/假红。
        regex = pat_list[0]["regex"]
        c.check("%s：干净件不含痕迹、弱点件含痕迹" % tid,
                all(re.search(t["regex"], clean_text) is None
                    and re.search(t["regex"], weak_text) is not None
                    for t in pat_list),
                "regex=%r" % [t["regex"] for t in pat_list])

        # ② 弱点件恰为干净件施加一次注入
        c.check("%s：弱点件 == 干净件.replace(from→to)" % tid,
                weak_text == clean_text.replace(injected["from"], injected["to"]))

        # ③ 正向判定：围绕每个 pattern 项取窗口拼成引文 + 断言后果 ⇒ tier2 检出
        # 定位必须在 weak_text 上做，不能用 `added` 里的匹配下标去索引 weak_text，
        #   两个字符串不同，下标一混就会取到无关段落，正向用例会假失败
        #   （本测试初版就这么错：取出的引文落在文书开头，看着像判定机失灵）。
        windows = []
        for pt in pat_list:
            m = re.search(pt["regex"], weak_text)
            if m is None:
                c.check("%s：痕迹在弱点件里可定位（前置条件）" % tid, False,
                        "regex=%r" % pt["regex"])
                windows = None
                break
            windows.append(weak_text[max(0, m.start() - 10):m.end() + 10])
        if windows is None:
            continue
        loc = "……".join(windows)
        assertion_alt = sig["clauses"][-1]["terms"][0]["alts"][0]
        f = F([loc, "", assertion_alt])
        c.check("%s：正向（引到痕迹 + 断言）⇒ tier2 检出" % tid,
                matcher.match(matcher.finding_texts(f), sig, tier=2) is not None,
                "引文 %r / 断言 %r" % (loc, assertion_alt))

        # ④ 关键负例：引文落在干净件也有的文字上 ⇒ tier2 不检出
        f2 = F([clean_text[:20], "", assertion_alt])
        c.check("%s：负例（引文只在干净件）⇒ tier2 不检出" % tid,
                matcher.match(matcher.finding_texts(f2), sig, tier=2) is None)


def main():
    print("=" * 70)
    print("任务集流水线回归测试")
    print("=" * 70)
    c = Case()
    if MATCHER_PATH is None:
        sys.stderr.write(_matcher_path.describe(CODE_ROOT, HERE) + "\n")
        return 2
    test_generator_has_teeth(c)
    test_end_to_end(c)
    print("\n" + "=" * 70)
    if c.failed:
        print("失败 %d / %d 项：" % (len(c.failed), c.n))
        for f in c.failed:
            print("  - %s" % f)
        return 1
    print("全部通过（%d 项断言）" % c.n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())