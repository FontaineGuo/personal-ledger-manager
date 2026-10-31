"""迁移：历史数据/2026下半年.xlsx → data/ledger.db（t2 §4 验收清单）。
用法：uv run python scripts/migrate.py [--wipe]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import openpyxl

from ledger.db import connect, fingerprint
from ledger.service import reconcile_period

XLSX = Path(__file__).resolve().parents[1] / "历史数据" / "2026下半年.xlsx"

INIT_EXPENSE = ["数码电器", "日用百货", "餐饮美食", "交通出行", "文化休闲",
                "医疗健康", "旅行", "投资", "杂项"]
INIT_INCOME = ["工资收入", "股权收入", "投资收益", "额外/其他", "报销收入"]
INIT_ACCOUNTS = [("中行", "bank"), ("招行", "bank"), ("建行", "bank"), ("农行", "bank"),
                 ("成都银行", "bank"), ("微信余额", "wallet"), ("支付宝余额", "wallet"), ("余额宝", "wallet")]
ACCOUNT_ALIASES = {"微信余额": ["微信", "微信零钱"], "支付宝余额": ["支付宝", "花呗"],
                   "招行": ["招行卡", "招行信用卡", "信用卡"], "中行": ["中行卡"],
                   "余额宝": ["余额宝理财"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wipe", action="store_true", help="删除现有 ledger.db 重建")
    ap.add_argument("--xlsx", default=str(XLSX))
    args = ap.parse_args()

    db = Path(__file__).resolve().parents[1] / "data" / "ledger.db"
    if args.wipe and db.exists():
        db.unlink()
    conn = connect(db)

    wb = openpyxl.load_workbook(args.xlsx, data_only=True)

    # 配置白名单（t1 §3.2 + 内部流转）
    for name in INIT_EXPENSE:
        conn.execute("INSERT OR IGNORE INTO categories(name, direction) VALUES(?, '支出')", (name,))
    for name in INIT_INCOME:
        conn.execute("INSERT OR IGNORE INTO categories(name, direction) VALUES(?, '收入')", (name,))
    conn.execute("INSERT OR IGNORE INTO categories(name, direction) VALUES('内部流转', '内部转账')")
    for name, kind in INIT_ACCOUNTS:
        import json
        conn.execute("INSERT OR IGNORE INTO accounts(name, kind, aliases) VALUES(?,?,?)",
                     (name, kind, json.dumps(ACCOUNT_ALIASES.get(name, []), ensure_ascii=False)))

    # 交易流水
    ws = wb["交易流水"]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    n_tx = 0
    for r in rows:
        if r[0] is None:
            continue
        date = r[0].strftime("%Y-%m-%d") if hasattr(r[0], "strftime") else str(r[0])[:10]
        period, direction, category = str(r[1]), str(r[2]), str(r[3])
        amount, account, note = round(float(r[4]), 2), str(r[5]), str(r[6] or "")
        fp = fingerprint(direction, date, amount, account, None, category, note)
        cur = conn.execute("SELECT id FROM transactions WHERE fingerprint=?", (fp,)).fetchone()
        if cur:
            continue
        conn.execute("""INSERT INTO transactions
            (date, period, direction, transfer_to, category, amount, account, note, source, fingerprint)
            VALUES (?,?,?,NULL,?,?,?,?, 'import', ?)""",
            (date, period, direction, category, amount, account, note, fp))
        n_tx += 1

    # 余额快照
    ws2 = wb["余额快照"]
    n_snap = 0
    for r in ws2.iter_rows(min_row=2, values_only=True):
        if r[0] is None:
            continue
        conn.execute("""INSERT OR IGNORE INTO balance_snapshots(period, account, opening, closing)
                        VALUES(?,?,?,?)""", (str(r[0]), str(r[1]), round(float(r[2]), 2), round(float(r[3]), 2)))
        n_snap += 1
    conn.commit()

    # 验收
    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    periods = sorted({r[0] for r in conn.execute("SELECT DISTINCT period FROM balance_snapshots")})
    print(f"迁移完成：交易 {n_tx} 新增 / 共 {total} 笔；快照 {n_snap} 行；账期 {periods}")
    all_balanced = True
    for p in periods:
        rec = reconcile_period(conn, p)
        bad = [a for a in rec["data"]["accounts"] if abs(a["diff"]) > 0.01]
        status = "✅ 平" if not bad else f"❌ 差 {[(a['account'], a['diff']) for a in bad]}"
        print(f"  {p}: {status}")
        all_balanced = all_balanced and not bad
    src_rows = sum(1 for r in wb["交易流水"].iter_rows(min_row=2, values_only=True) if r[0] is not None)
    print("验收：", "全部通过" if (total == src_rows and all_balanced) else f"存在偏差（xlsx {src_rows} vs db {total}）")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
