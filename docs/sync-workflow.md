# 多机同步流程（代码/数据分离方案）

> 前提约束：坚果云不建议存放代码类文件。因此 **代码归 git，数据归网盘**，
> 两者在每台机器上通过本地配置文件对接。

## 目录职责划分

| 内容 | 存放 | 同步方式 |
|------|------|---------|
| 代码（src/ scripts/ tests/ pyproject uv.lock）| 各机器本地 git 仓库 | git push/pull（GitHub/Gitee 私库均可）|
| skill（.agents/skills/ .claude/skills/）| 随代码仓库 | git |
| 文档（docs/ README）| 随代码仓库 | git |
| **账本数据 ledger.db + backups/** | 坚果云 `ledger-data/` 目录 | 坚果云自动同步 |
| 导出 xlsx（export/）、历史 xlsx | 坚果云 `ledger-data/` | 坚果云自动同步 |
| .venv、.ledger-data.json、缓存 | 各机器本地 | 不同步 |

网盘目录结构（只有一个目录，全是数据，无代码）：

```
坚果云/ledger-data/
  ├── ledger.db            # 唯一账本
  ├── backups/             # 每次写入自动备份（30天）
  ├── export/              # 封账后固化的 xlsx
  └── 历史数据/2026下半年.xlsx
```

## 一次性迁移（当前机器）

1. 代码仓库初始化并推送：`git init && git add -A && git commit && git remote add origin <私库> && git push`
   （.gitignore 已排除 .venv/缓存/.ledger-data.json/.agent-teams）
2. `mkdir <坚果云>/ledger-data`，移动：`data/ledger.db`、`data/backups/`、`data/export/`、`历史数据/` 进去
3. 在工作区根创建 `.ledger-data.json`（照抄 .ledger-data.json.example）：
   `{"db": "<坚果云盘符:/路径>/ledger-data/ledger.db"}`
4. 验证：`uv run ledger whitelist` 正常返回 → 说明已指向网盘库

## 新机器接入（约 5 分钟）

1. `git clone <私库> personalAssetManager && cd personalAssetManager`
2. 安装 uv，然后 `uv sync`
3. 等坚果云把 ledger-data/ 同步完成（托盘图标静止）
4. 创建本机的 `.ledger-data.json` 指向本机坚果云路径
5. `uv run ledger reconcile -i '{"period":"<最新账期>"}'` 确认勾稽全平 → 接入完成
6. DSH/Claude Code 打开该工作区，skill 自动生效

## 日常纪律（与之前一致）

- **单写入者**：同一时间只在一台机器记账；换机前确认网盘同步完成
- **data 目录始终保留本地**：坚果云若开智能同步，把 ledger-data/ 设为"始终保留在此设备"
- **封账后固化**：月末封账时按 skill 提示导出 xlsx（进网盘 export/，即冷备）
- 代码改动（改 skill/改功能）：正常 git 提交推送；另一台机器 pull 即可，与数据同步互不干扰

## 故障恢复路径（按优先级）

1. 网盘出现冲突副本 → integrity_check + reconcile 对比，留勾稽全平且笔数多的一版
2. ledger.db 损坏 → 用 backups/ 最新每日副本恢复（差一天的账从 export/ xlsx 或记忆补录）
3. 极端情况 → 从最近一次封账固化的 xlsx 重建（migrate.py 适配新格式后）
4. 网盘整体不可用 → 本机 backups/ 仍有最近副本，数据不丢

## 路径解析优先级（db.py）

`--db 参数` > 环境变量 `LEDGER_DB` > `.ledger-data.json` 的 db 字段 > 默认 `<工作区>/data/ledger.db`
（最后一项保证不配置时单机模式照常工作；备份目录恒在 db 同级 backups/）
