"""ACTE：对抗案件理论机。

## 模块分工

| 模块 | 职责 | 对应形式化 |
|---|---|---|
| `t_irr` | `T_irr` 登记表与安全盾（限制型 / 强制型） | 定理 1、1′ |
| `posture` | 姿态向量 `π = (Obj, ε)` 与嵌套诊断 | 定理 2、3 |
| `toc` | 案件理论图与支撑完备性 / 要件覆盖度 | T2 的机械指标；`M_1` 口径 |
| `engine` | 三层编排（先算盾、再选动作） | 定理 1(c) 的"分两步" |

## 与 DSH 基座的关系

本包是零第三方依赖的纯逻辑内核，可独立测试（`code/tests/test_acte_core.py`，
确定性、不调模型）。挂到 DSH 上的运行时形态（把盾做成插件、按案型换 profile）
在 `code/plugins/` 与 `experiment/configs/`，见 `code/README.md`。

为什么内核不调模型：盾与姿态的取值只依赖 `T_irr` 与登记表，不依赖任何生成式能力。
把它们绑进模型调用会让"闸门是否生效"变成不可复现的读数，而定理 1 的整个价值
就在于闸门可独立于策略计算。
"""

from .engine import Engine, EngineError
from .posture import POSTURES, Posture, PostureError, get as get_posture
from .t_irr import WAIT, IrreversibleAction, ShieldError, TIrR
from .toc import ALL_EDGE_KINDS, Claim, ToC, ToCError

__all__ = [
    "Engine", "EngineError",
    "TIrR", "IrreversibleAction", "ShieldError", "WAIT",
    "Posture", "PostureError", "POSTURES", "get_posture",
    "ToC", "Claim", "ToCError", "ALL_EDGE_KINDS",
]

__version__ = "0.1.0"