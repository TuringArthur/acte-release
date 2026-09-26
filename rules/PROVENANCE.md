# 钉版快照溯源（`rules/gates.yaml`）

本文件是上游闸门注册表的钉版快照，随本仓库固定发布。收录它的目的，是使
`code/tools/verify_all.py` 的第 2 档（T_irr 快照与闸门源的一致性校验）在独立
仓库内仍可执行；否则该档无源可比。

- 冻结时 sha256：`c33153be3e0193cae7172212a19709b87552c232a5304e9fc27f5263e796a7e4`
- 由它导出的快照：`code/data/t-irr.json`（由 `code/tools/derive_t_irr.py` 生成，
  同样附源 sha256，两者可互校）
