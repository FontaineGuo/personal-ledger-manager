"""数据库层：连接、Schema（四表+索引+勾稽视图）、备份。设计依据 docs/t2-数据存储方案.md §2。"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import os

_WORKSPACE = Path(__file__).resolve().parents[2]
DEFAULT_DB = _WORKSPACE / "data" / "ledger.db"

def _configured_db() -> Path | None:
    """数据路径解析（多机网盘分离部署用）：
    1. 环境变量 LEDGER_DB
    2. 工作区根 .ledger-data.json 的 {"db": ...}（机器本地文件，勿入 git/网盘）
    3. 默认 <工作区>/data/ledger.db
    备份目录恒为 <db 所在目录>/backups。"""
    env = os.environ.get("LEDGER_DB")
    if env:
        return Path(env)
    cfg = _WORKSPACE / ".ledger-data.json"
    if cfg.exists():
        try:
            v = json.loads(cfg.read_text("utf-8")).get("db")
            if v:
                return Path(v)
        except (json.JSONDecodeError, OSError):
            pass
    return None

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  date        TEXT    NOT NULL,             -- ISO YYYY-MM-DD
  period      TEXT    NOT NULL,             -- 账期 YYYY-MM（可≠date所在月）
  direction   TEXT    NOT NULL CHECK (direction IN ('收入','支出','内部转账')),
  transfer_to TEXT    DEFAULT NULL,         -- 仅内部转账：转入账户（account 为转出方）
  category    TEXT    NOT NULL,
  amount      REAL    NOT NULL CHECK (amount > 0 AND amount < 1000000),
  account     TEXT    NOT NULL,
  note        TEXT    DEFAULT '',
  source      TEXT    DEFAULT 'chat',       -- chat / import / adjust
  session_id  TEXT    DEFAULT '',
  voided      INTEGER NOT NULL DEFAULT 0,   -- 软删标记：不参与勾稽与统计
  created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  fingerprint TEXT    NOT NULL,
  CHECK (direction != '内部转账' OR (transfer_to IS NOT NULL AND transfer_to != account))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tx_fp ON transactions(fingerprint);
CREATE INDEX IF NOT EXISTS ix_tx_period ON transactions(period, account);
CREATE INDEX IF NOT EXISTS ix_tx_date   ON transactions(date);

CREATE TABLE IF NOT EXISTS accounts (
  name     TEXT PRIMARY KEY,
  kind     TEXT NOT NULL CHECK (kind IN ('bank','wallet')),
  aliases  TEXT NOT NULL DEFAULT '[]',     -- JSON 数组，槽位推断用
  active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS categories (
  name      TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('收入','支出','内部转账')),
  aliases   TEXT NOT NULL DEFAULT '[]',
  active    INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (name, direction)
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
  period  TEXT NOT NULL,
  account TEXT NOT NULL REFERENCES accounts(name),
  opening REAL NOT NULL,
  closing REAL NOT NULL,
  closed  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (period, account)
);

-- 勾稽视图：内部转账拆双边（转出=流出、转入=流入），排除已作废记录
CREATE VIEW IF NOT EXISTS v_period_flow AS
SELECT period, account,
       SUM(CASE WHEN side='in'  THEN amount ELSE 0 END) AS inflow,
       SUM(CASE WHEN side='out' THEN amount ELSE 0 END) AS outflow
FROM (
  SELECT period, account, 'out' AS side, amount FROM transactions
    WHERE voided=0 AND direction IN ('支出','内部转账')
  UNION ALL
  SELECT period, transfer_to AS account, 'in' AS side, amount FROM transactions
    WHERE voided=0 AND direction='内部转账'
  UNION ALL
  SELECT period, account, 'in' AS side, amount FROM transactions
    WHERE voided=0 AND direction='收入'
)
GROUP BY period, account;
"""


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else (_configured_db() or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def fingerprint(direction: str, date: str, amount: float, account: str,
                transfer_to: str | None, category: str, note: str) -> str:
    note_norm = "".join(str(note or "").lower().split())
    parts = "|".join([
        direction, date, f"{round(float(amount), 2):.2f}",
        account or "", transfer_to or "", category, note_norm,
    ])
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


def backup(db_path: str | Path | None = None) -> str:
    """写入事务后调用：每日一份，保留 30 天。备份在 db 同目录的 backups/ 下。"""
    path = Path(db_path) if db_path else (_configured_db() or DEFAULT_DB)
    if not path.exists():
        return ""
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    dest = backup_dir / f"ledger-{stamp}.db"
    shutil.copy2(path, dest)
    # 保留 30 天
    old = sorted(backup_dir.glob("ledger-*.db"))
    for f in old[:-30]:
        f.unlink(missing_ok=True)
    return str(dest)


def load_aliases(raw: str) -> list[str]:
    try:
        v = json.loads(raw or "[]")
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []
