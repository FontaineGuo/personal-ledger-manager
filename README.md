# 对话式个人记账（ledger）

基于 docs/ 设计书实现：SQLite 单库 + JSON CLI，供 DSH / Claude Code 等对话环境调用。
数据：`data/ledger.db`（已迁移 历史数据/2026下半年.xlsx：290 笔、六账期勾稽全平）。

## 常用命令（uv）

```bash
uv run ledger whitelist                                   # 查看分类/账户白名单
uv run ledger add-transaction -i '{"direction":"支出","category":"餐饮美食","amount":25,"account":"微信余额","note":"午餐"}'
uv run ledger add-transfer -i '{"from":"招行","to":"中行","amount":2000,"note":"零用钱"}'
uv run ledger query -i '{"period":"2026-11","account":"微信余额"}'
uv run ledger report-breakdown -i '{"period":"2026-11"}'   # 分类构成（饼图）
uv run ledger report-trend -i '{"months":6}'               # 月度趋势
uv run ledger report-account-flow -i '{"period":"2026-11"}'
uv run ledger report-daily -i '{"period":"2026-11"}'
uv run ledger report-compare -i '{"period":"2026-10"}'     # 环比复盘
uv run ledger export                                      # 三表同构 xlsx → data/export/
uv run ledger export-report -i '{"kind":"summary"}'        # 报表 xlsx（category/trend/accountFlow/detail）
uv run ledger reconcile -i '{"period":"2026-11"}'          # 对账
uv run ledger close-period -i '{"period":"2026-11"}'       # 封账（须先全平）
```

纠错 / 配置维护：`update-transaction` `void-transaction` `reopen-period` `add-category` `add-account` `add-aliases` `rename-category` `rename-account` `set-category-active` `set-account-active` `list-config` `match-account`。

## 约定（对话层须遵守，t3 §5 铁律）

- **逐笔确认**：写入接口调用前必须向用户回显七字段（推断项标 *）并获确认；
- **重复处理**：返回 `duplicate` 时回显已有记录，用户确认后带 `confirm:true` 重调；
- **歧义必问**：`category_invalid/account_invalid` 返回 `candidates`，据此追问；
- **封账保护**：`period_closed` 时提示先 `reopen-period`。

## 测试与迁移

```bash
uv run --group dev pytest tests/ -q        # 11 项验收测试
uv run python scripts/migrate.py           # 增量迁移 xlsx（幂等）
```


## 多机使用（坚果云等网盘同步）

账本数据（data/ledger.db）为 SQLite 单文件、delete 日志模式（无 -wal/-shm 边车），同步友好。
跨机器使用注意：

1. **同步排除**（体积/机器相关，勿同步）：`.venv/`、`__pycache__/`、`.pytest_cache/`
   —— 新机器上安装 uv 后执行 `uv sync` 即可重建环境（Python ≥3.12）。
2. **单写入者纪律**：同一时间只在一台机器记账。切换机器前确认网盘同步完成（托盘图标转完），
   避免两端各写一版导致冲突副本（坚果云会保留冲突文件，但需人工裁决，指纹/快照会分叉）。
3. **云盘按需下载**：若开启"智能同步/按需下载"，使用前确保 data/ 整个目录已下载为本地实体文件
   （SQLite 对占位文件读写会失败）。建议把 data/ 设为"始终保留在此设备"。
4. **冲突处理**：若出现 `... (冲突副本 ...).db`，用 `uv run python -c "import sqlite3;..." `
   的 integrity_check + reconcile 对比两版，保留勾稽全平且笔数多的一版，另一版导出 xlsx 备查。
5. **灾难恢复**：backups/（每次写入后自动备份，按日一份、当日多次写入覆盖更新当日文件、
   仅保留最近 30 天，同步随行）与 data/export/（封账固化 xlsx）都是恢复来源；
   旧格式账本可用 scripts/migrate.py 重建。恢复优先级：活库 → 昨日备份 → 当月封账 xlsx。
6. **路径**：数据路径可配置（--db / LEDGER_DB / .ledger-data.json，见 docs/sync-workflow.md），
   备份目录恒在 db 同级 backups/；不配置时默认 <工作区>/data/ledger.db。
7. **skill 随行**：.dsh/skills/ledger/SKILL.md 在工作区内，另一台机器的 DSH 打开该工作区即自动加载。
8. **注意**：两台机器系统时间需大致正确（账期超期提醒按本机日期计算）；DSH 文件沙箱策略
   两台机器需一致（建议 workspace-write 以上）。

- 多机同步（代码归 git / 数据归网盘）：见 docs/sync-workflow.md
