# 数据表初始化

本版通过 `src.models.base.Base.metadata.create_all` 创建首次运行所需的九张表（新增 `material_accounts`、`correction_vouchers`、`ledger_entries` 三张领退料更正账表）。模型在 `src/models/workflow.py`。`python -m scripts.seed_demo` 按观测内容键补全演示样本，保留现有分析、签核与更正账。数据库端口只发布到本机。
