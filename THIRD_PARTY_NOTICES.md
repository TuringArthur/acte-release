# 第三方声明（Third-Party Notices）

本仓库的自有内容（算法内核、运行时插件、登记表与派生工具、评测入口、形式化规范）
采用 [CC BY-NC-SA 4.0](LICENSE)（署名 — 非商业性使用 — 相同方式共享）。

本仓库同时包含或依赖下列第三方组件，它们各自遵循原许可证，不受本仓库的
CC BY-NC-SA 条款约束。分发本仓库或其派生物时，须一并保留以下声明。

---

## 1. DeepSeek Harness（DSH）

- 用途：引擎的运行时基座。本仓库以**声明依赖**的方式引用，不内嵌其源码。
- 上游：https://github.com/deepseek-ai/deepseek-harness.git
- 钉死版本：`0.1.5-rc.2` / commit `c291e7961a515f6d7af9304e7fd1d257929aef26`
- 许可证：MIT

```
MIT License

Copyright (c) 2026 DeepSeek

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

DSH 自身把 Cordis（`vendor/cordis`）等一批包 vendored 进其仓内，并维护了完整的上游
依赖清单；本仓库不重复罗列，以 DSH 仓内的 `THIRD_PARTY_NOTICES.md` 为准。

## 2. 注入签名判定机（`code/vendor/signature.py`）

- 用途：弱点注入签名判定机，同一套判定在多个任务形态间复用。
- 形态：**只读冻结件**，字节复制进本仓库，署名与哈希见 `code/vendor/PROVENANCE.md`。
- 权利归属与许可：与本仓库自有内容相同（CC BY-NC-SA 4.0）。
  该判定机由本仓库作者一方开发并持有，不是上游第三方软件。

## 3. 直接依赖的 npm 包

| 包 | 版本 | 用在哪 | 许可证 |
|---|---|---|---|
| `@deepseek-ai/cordis` | `4.0.2` | `code/plugins/package.json`（插件运行时） | MIT |

---

## 为什么 CC BY-NC-SA 4.0 与 MIT 依赖可以共存

MIT 是宽松许可证，只要求保留版权与许可声明，不要求衍生作品沿用 MIT，也不限制衍生
作品的许可方式。因此本仓库可以把 MIT 组件作为依赖、而自有部分采用 CC BY-NC-SA 4.0，
只要满足两个条件：

1. 保留各 MIT 组件的版权与许可声明（本文件即为履行方式）；
2. 不对 MIT 组件本身主张 CC BY-NC-SA 的"非商业性使用"限制——MIT 组件仍可被他人
   商用，非商业限制只覆盖本仓库自有作品。