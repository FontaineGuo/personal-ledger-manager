---
name: ledger
description: 个人记账：通过 ledger CLI（uv run ledger）完成对话式记账、内部转账、查询、对账封账、可视化报表与 Excel 导出。任何记账/查账/月度对账意图都应使用本 skill。
whenToUse: 用户提到记账、记一笔、花了/收入/转账/报销/AA/补差、查消费/账单/余额、对账、封账、月度复盘、导出 Excel 时。
---

# ledger 对话式记账 skill

数据：SQLite data/ledger.db，唯一写入通道是 CLI：`uv run ledger <command> -i '<JSON>'`（工作区根目录执行，输出 JSON）。

## 铁律（t3 §5，必须遵守）

1. **逐笔确认，禁止静默落库**：任何写入前，先回显七字段卡片（推断项标 *），用户说"对"才调 CLI；用户说"改X"就改完再确认。
2. **歧义必问，不猜测**：金额缺失必问；分类歧义列候选；账户歧义给"最常用+其他"；月末（25日后）交易追问账期归属。
3. **duplicate 处理**：CLI 返回 reason=duplicate 时，把 existing 记录回显给用户："疑似重复，确认再记？"确认后原参数加 confirm:true 重调。
4. **白名单不符**：reason=category_invalid / account_invalid 时把 candidates 列给用户选择；用户想新增就走配置管理流程。
5. **封账保护**：reason=period_closed 时告知"该账期已封账"，经用户确认后 reopen-period → 改 → 重新对账。
6. **账期超期持续提醒**：任何写入返回带 warning(type=period_overdue) 时，必须向用户转达"账期已记账 N 天（>30天），是否开启新月度账期并结算月末/月初余额？"；用户同意 → 走 start-period 流程（报各账户真实余额或按接力）；拒绝 → 明确告知下次记账还会提醒，且每次带 warning 的写入都要重复转达，不得静默。

## 五步管线

用户输入 → ①意图识别 → ②槽位抽取 → ③缺失槽位追问 → ④确认回显 → ⑤调 CLI 落库

意图：记支出/记收入/内部转账/报销到账/查询/对账/修正/配置管理。
槽位：date(默认今天)、period(默认 date 所在月)、direction、category、amount、account、note。

推断规则：
- direction 由分类联动：工资/报销/投资收益→收入，其余→支出
- category 由商品词映射白名单（先调 whitelist 或 match-account）
- account 由别名推断（match-account，如"微信"→微信余额），缺省追问并给最常用建议
- 转账区分：转入方在账户白名单内 → add-transfer（内部转账）；转证券/表外 → 支出-投资；不确定必问

## 一句话多笔拆解

用户一句话含多笔账（典型：发工资+向多卡分配，如"发11月工资13000到招行，转中行1500、建行4000"）：
1. 先拆解为 N 笔并列出清单（意图/金额/账户），让用户看到全貌；
2. 逐笔出确认卡片、逐笔确认落库（铁律1不因批量放松；可连问"笔1对吗？笔2对吗？"）；
3. 分配/流转类（转入白名单账户）→ add-transfer 内部转账；绝不拆成"支出+收入"两笔互抵（旧账本习惯，已废弃）；
4. 金额守恒检查：分配合计若等于/超过工资额，主动提示（如 13000 中 5500 被分配，余 7500 留在招行）。

## 备份与恢复（自动机制，无需用户操作）

每次写入成功后自动备份：db 同级 `backups/ledger-YYYYMMDD.db`，按日一份，当日多次写入覆盖更新当日文件，仅保留最近 30 天。用户问"数据丢了/坏了怎么恢复"时按此顺序建议：活库 → backups/ 最近副本（差一天内的账可从 export xlsx 或记忆补录）→ 当月封账固化的 xlsx。作废/修改类错误不要用备份回滚，用 void/update 纠错（软删除可查）。

## 命令速查

| 意图 | 命令 |
|------|------|
| 记一笔 | add-transaction（duplicate 时 confirm:true 重调）|
| 内部转账 | add-transfer {from,to,amount,note} |
| 查询 | query {period?,category?,account?,direction?,dateFrom?,dateTo?,noteContains?,limit?} |
| 月度汇总 | summary {period} |
| 分类构成 | report-breakdown {period,topN?} |
| 趋势 | report-trend {months,category?} |
| 账户流 | report-account-flow {period} |
| 日消费 | report-daily {period} |
| 明细表 | report-detail {…,page?,pageSize?} |
| 环比复盘 | report-compare {period} |
| 对账 | reconcile {period}；全平后 close-period |
| 改/删 | update-transaction {id,patch} / void-transaction {id,reason} |
| 配置 | whitelist / list-config / add-category / add-account / add-aliases / rename-* / set-*-active / match-account |
| 导出 | export（三表同构） / export-report {kind:summary|category|trend|accountFlow|detail} |

导出的 xlsx 在 data/export/ 下，用 present 交付文件卡片。

## 对话示例

> 用户：午饭和晚饭一共58，微信
> 你：先解析（date=今天*，period=本月*，direction=支出，category=餐饮美食*，amount=58，account=微信余额，note=午+晚餐合并*），回显：
> 📋 记支出确认——日期* 今天｜账期* 2026-XX｜分类* 餐饮美食｜金额 ¥58.00｜账户 微信余额｜备注* 午+晚餐合并。回复"对"或告诉我改哪项。
> 用户：对 → 调 add-transaction，成功后回"✅ 已记 2026-XX 餐饮美食 ¥58.00（微信余额）"

> 用户：转2000到零用钱卡 → 追问/确认转出转入卡 → add-transfer → ✅ 内部转账不入收支统计

> 用户：这个月餐饮花了多少 → summary 或 report-breakdown，用表格回复，附笔数与环比（report-compare）

月度复盘：账期结束首次对话可主动提示（用 report-compare + report-account-flow），勾稽有差异时给出"记一笔补差"入口。

## 月度开账/闭账流程

月初用户说"开新账期"：
1. 调 start-period {period}——各 active 账户 opening 自动接力上期 closing；
2. 回显接力表（账户｜上期月末→本期月初），用户核对银行卡实际数，不符的账户报真实值，用 overrides 重调；
3. closing 暂与 opening 相同，提示"月末报余额时更新"。

月末用户报各账户余额：逐账户 upsert-snapshot {period, account, opening, closing} → reconcile → 全平则询问是否 close-period 封账；不平报差异明细，小差异提供"记一笔补差"入口。

### 封账后固化（冷备，必须询问）

close-period 成功后，**主动提示**用户是否固化该账期数据（不得静默执行）：

> 💾 账期 {period} 已封账。是否固化数据（导出 xlsx 冷备）？
> 1. 仅账本三表（export，指定 periods=[{period}]）
> 2. 账本 + 月度汇总报表（export + export-report summary）
> 3. 跳过本次

用户选择后执行对应导出，把 data/export/ 下的产物路径用文件卡片交付，并提示"该文件已随网盘同步"。用户选"跳过"要告知：跳过不影响记账，但本月冷备缺失，下次封账会再次提示。
