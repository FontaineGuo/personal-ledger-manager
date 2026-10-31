"""记账服务层：docs/t2 §3 全部接口的实现。全部返回 Result JSON。"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from .db import DEFAULT_DB, backup, connect, fingerprint, load_aliases
from .result import err, ok

AMOUNT_MIN, AMOUNT_MAX = 0.01, 1_000_000
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")


# ---------- 内部工具 ----------

def _norm_date(v: str | None, default: str | None = None) -> str:
    d = (v or default or "").strip()
    if not DATE_RE.match(d):
        raise ValueError(f"date 非法（YYYY-MM-DD）：{d!r}")
    return d

def _period_of(date: str) -> str:
    return date[:7]

def _period(v: str | None, date: str) -> str:
    p = (v or "").strip()
    if p and not PERIOD_RE.match(p):
        raise ValueError(f"period 非法（YYYY-MM）：{p!r}")
    return p or _period_of(date)

def _amount(v: Any) -> float:
    a = round(float(v), 2)
    if not (AMOUNT_MIN <= a < AMOUNT_MAX):
        raise ValueError(f"amount 非法（0 < amount < 1000000）：{v!r}")
    return a

def _row(r) -> dict:
    d = dict(r)
    return d

def _active_account(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM accounts WHERE name=? AND active=1", (name,))
    return cur.fetchone() is not None

def _active_category(cur, name: str, direction: str) -> bool:
    cur.execute("SELECT 1 FROM categories WHERE name=? AND direction=? AND active=1",
                (name, direction))
    return cur.fetchone() is not None

def _account_closed(cur, period: str, account: str) -> bool:
    cur.execute("SELECT closed FROM balance_snapshots WHERE period=? AND account=?",
                (period, account))
    row = cur.fetchone()
    return bool(row and row["closed"])

def _account_candidates(cur, word: str) -> list[str]:
    """按名字与别名匹配候选账户（槽位推断后端，供对话层调用）。"""
    out = []
    for r in cur.execute("SELECT name, aliases FROM accounts WHERE active=1"):
        if word in r["name"] or word in load_aliases(r["aliases"]):
            out.append(r["name"])
    return out


# ---------- 写入类 ----------

PERIOD_MAX_DAYS = 30

def _period_overdue_warning(conn, period: str) -> dict | None:
    """账期记账周期超 30 天的持续提醒：从该账期第一笔交易日起算。
    只要未开新账期，每次写入都会带上此 warning。"""
    from datetime import date as _date
    row = conn.execute("SELECT MIN(date) first_day, COUNT(*) n FROM transactions WHERE period=?",
                       (period,)).fetchone()
    if not row or not row["first_day"] or row["n"] == 0:
        return None
    days_used = (_date.today() - _date.fromisoformat(row["first_day"])).days
    if days_used <= PERIOD_MAX_DAYS:
        return None
    latest = conn.execute("SELECT MAX(period) FROM balance_snapshots").fetchone()[0] or ""
    if latest > period:  # 已开更新账期（此账期只是补记历史）
        return None
    next_p = period_next(period)
    return {"type": "period_overdue", "period": period, "daysUsed": days_used,
            "nextPeriod": next_p,
            "message": f"账期 {period} 已记账 {days_used} 天（>30 天），是否开启新月度账期（{next_p}）并结算月末/月初余额？未开启前此提醒会持续出现。"}

def _latest_tx_period(conn) -> str:
    row = conn.execute("SELECT MAX(period) FROM transactions").fetchone()
    return row[0] if row and row[0] else ""

def period_next(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    return f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"

def add_transaction(conn, inp: dict) -> dict:
    try:
        date = _norm_date(inp.get("date"), default=str(__import__("datetime").date.today()))
        period = _period(inp.get("period"), date)
        direction = inp.get("direction")
        if direction not in ("收入", "支出"):
            return err("direction_invalid", message="direction 必须为 收入/支出（转账请用 add_transfer）")
        category = (inp.get("category") or "").strip()
        amount = _amount(inp.get("amount"))
        account = (inp.get("account") or "").strip()
        note = inp.get("note") or ""
        source = inp.get("source") or "chat"
    except (ValueError, TypeError) as e:
        return err("invalid_input", message=str(e))

    cur = conn.cursor()
    if not _active_category(cur, category, direction):
        cur.execute("SELECT name FROM categories WHERE direction=? AND active=1", (direction,))
        return err("category_invalid",
                   candidates=[r["name"] for r in cur.fetchall()])
    if not _active_account(cur, account):
        cur.execute("SELECT name FROM accounts WHERE active=1")
        return err("account_invalid", candidates=[r["name"] for r in cur.fetchall()])
    if _account_closed(cur, period, account):
        return err("period_closed",
                   message=f"{period}/{account} 已封账，请先 reopen_period")

    fp = fingerprint(direction, date, amount, account, None, category, note)
    cur.execute("SELECT * FROM transactions WHERE fingerprint=?", (fp,))
    dup = cur.fetchone()
    if dup and not inp.get("confirm"):
        return err("duplicate", existing=_row(dup))
    if dup:  # 用户确认后的有意重复：指纹加序号后缀，唯一索引继续拦截意外重复
        n = conn.execute("SELECT COUNT(*) FROM transactions WHERE fingerprint LIKE ?",
                         (fp + "#%",)).fetchone()[0]
        fp = f"{fp}#{n + 1}"

    cur.execute("""INSERT INTO transactions
        (date, period, direction, transfer_to, category, amount, account, note,
         source, session_id, fingerprint)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (date, period, direction, None, category, amount, account, note,
         source, inp.get("sessionId") or "", fp))
    conn.commit()
    backup(conn.db_path if hasattr(conn, "db_path") else None)
    tid = cur.lastrowid
    return ok({"id": tid,
               "balanceEffect": {"account": account,
                                 "delta": amount if direction == "收入" else -amount},
               **({"warning": _period_overdue_warning(conn, period)} if _period_overdue_warning(conn, period) else {})})


def add_transfer(conn, inp: dict) -> dict:
    try:
        date = _norm_date(inp.get("date"), default=str(__import__("datetime").date.today()))
        period = _period(inp.get("period"), date)
        src = (inp.get("from") or "").strip()
        dst = (inp.get("to") or "").strip()
        amount = _amount(inp.get("amount"))
        note = inp.get("note") or ""
    except (ValueError, TypeError) as e:
        return err("invalid_input", message=str(e))
    if src == dst:
        return err("same_account", message="转出/转入账户不能相同")

    cur = conn.cursor()
    for name, why in ((src, "from"), (dst, "to")):
        if not _active_account(cur, name):
            cur.execute("SELECT name FROM accounts WHERE active=1")
            return err(f"{why}_account_invalid", candidates=[r["name"] for r in cur.fetchall()])
    if not _active_category(cur, "内部流转", "内部转账"):
        cur.execute("INSERT OR IGNORE INTO categories(name, direction) VALUES('内部流转','内部转账')")
    for a in (src, dst):
        if _account_closed(cur, period, a):
            return err("period_closed", message=f"{period}/{a} 已封账，请先 reopen_period")

    fp = fingerprint("内部转账", date, amount, src, dst, "内部流转", note)
    cur.execute("SELECT * FROM transactions WHERE fingerprint=?", (fp,))
    dup = cur.fetchone()
    if dup and not inp.get("confirm"):
        return err("duplicate", existing=_row(dup))
    if dup:
        n = conn.execute("SELECT COUNT(*) FROM transactions WHERE fingerprint LIKE ?",
                         (fp + "#%",)).fetchone()[0]
        fp = f"{fp}#{n + 1}"

    cur.execute("""INSERT INTO transactions
        (date, period, direction, transfer_to, category, amount, account, note,
         source, session_id, fingerprint)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (date, period, "内部转账", dst, "内部流转", amount, src, note,
         "chat", inp.get("sessionId") or "", fp))
    conn.commit()
    backup()
    w = _period_overdue_warning(conn, period)
    return ok({"id": cur.lastrowid, **({"warning": w} if w else {})})


def upsert_snapshot(conn, inp: dict) -> dict:
    try:
        period = (inp.get("period") or "").strip()
        if not PERIOD_RE.match(period):
            raise ValueError("period 非法")
        account = (inp.get("account") or "").strip()
        opening, closing = round(float(inp["opening"]), 2), round(float(inp["closing"]), 2)
    except (ValueError, TypeError, KeyError) as e:
        return err("invalid_input", message=str(e))
    cur = conn.cursor()
    if not _active_account(cur, account):
        return err("account_invalid")
    cur.execute("""INSERT INTO balance_snapshots(period, account, opening, closing, closed)
                   VALUES(?,?,?,? ,0)
                   ON CONFLICT(period, account) DO UPDATE SET
                     opening=excluded.opening, closing=excluded.closing""",
                (period, account, opening, closing))
    conn.commit()
    rec = reconcile_period(conn, period)
    acct = next((a for a in rec["data"]["accounts"] if a["account"] == account), None)
    return ok({"reconcile": acct})


def start_period(conn, period: str, overrides: dict[str, list[float]] | None = None) -> dict:
    """开新账期：为全部 active 账户生成快照，opening 默认接力上一账期 closing。
    overrides: {账户: [opening, closing]} 人工修正个别账户（如卡内实际数与接力值不符）。"""
    if not PERIOD_RE.match(period or ""):
        return err("invalid_input", message="period 非法（YYYY-MM）")
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT period FROM balance_snapshots ORDER BY period DESC LIMIT 1")
    prev_row = cur.fetchone()
    prev = prev_row[0] if prev_row else None
    if prev and period <= prev:
        return err("period_not_newer", message=f"最新账期已是 {prev}，新账期必须晚于它")
    overrides = overrides or {}
    created, chained, manual = [], [], []
    accounts = conn.execute("SELECT name FROM accounts WHERE active=1 ORDER BY name").fetchall()
    for a in accounts:
        name = a["name"]
        cur.execute("SELECT closing FROM balance_snapshots WHERE period=? AND account=?", (prev, name))
        row = cur.fetchone()
        if name in overrides:
            opening, closing = round(float(overrides[name][0]), 2), round(float(overrides[name][1]), 2)
            manual.append(name)
        elif row:
            opening = closing = round(row["closing"], 2)
            chained.append(name)
        else:
            return err("missing_prev_snapshot",
                       message=f"{name} 无上期快照可接力，请在 overrides 中提供 [opening, closing]")
        cur.execute("""INSERT OR IGNORE INTO balance_snapshots(period, account, opening, closing)
                       VALUES(?,?,?,?)""", (period, name, opening, closing))
        created.append(name)
    conn.commit()
    return ok({"period": period, "prevPeriod": prev, "created": created,
               "chainedFromPrev": chained, "manualOverrides": manual,
               "hint": "月初开账：closing 先与 opening 相同，月末抄录真实余额后 upsert-snapshot 更新"})


def reconcile_period(conn, period: str) -> dict:
    cur = conn.cursor()
    cur.execute("""SELECT s.account, s.opening, s.closing,
                          COALESCE(f.inflow,0) AS inflow, COALESCE(f.outflow,0) AS outflow
                   FROM balance_snapshots s LEFT JOIN v_period_flow f
                     ON f.period=s.period AND f.account=s.account
                   WHERE s.period=?""", (period,))
    rows = []
    for r in cur.fetchall():
        expected = round(r["opening"] + r["inflow"] - r["outflow"], 2)
        diff = round(r["closing"] - expected, 2)
        rows.append({"account": r["account"], "opening": r["opening"],
                     "inflow": r["inflow"], "outflow": r["outflow"],
                     "expectedClosing": expected, "recordedClosing": r["closing"],
                     "diff": diff})
    rows.sort(key=lambda x: x["account"])
    return ok({"accounts": rows, "balanced": all(abs(x["diff"]) <= 0.01 for x in rows)})


def close_period(conn, period: str) -> dict:
    rec = reconcile_period(conn, period)
    if not rec["ok"] or not rec["data"]["balanced"]:
        return err("not_balanced", **{"data": rec.get("data")})
    conn.execute("UPDATE balance_snapshots SET closed=1 WHERE period=?", (period,))
    conn.commit()
    cur = conn.execute("SELECT account FROM balance_snapshots WHERE period=? AND closed=1", (period,))
    return ok({"closedAccounts": [r["account"] for r in cur.fetchall()]})


def reopen_period(conn, period: str) -> dict:
    conn.execute("UPDATE balance_snapshots SET closed=0 WHERE period=?", (period,))
    conn.commit()
    cur = conn.execute("SELECT account FROM balance_snapshots WHERE period=?", (period,))
    return ok({"reopenedAccounts": [r["account"] for r in cur.fetchall()]})


# ---------- 纠错类 ----------

EDITABLE = {"date", "period", "direction", "category", "amount",
            "account", "transfer_to", "note"}

def update_transaction(conn, tid: int, patch: dict) -> dict:
    bad = set(patch) - EDITABLE
    if bad:
        return err("invalid_field", fields=sorted(bad))
    cur = conn.cursor()
    cur.execute("SELECT * FROM transactions WHERE id=?", (tid,))
    row = cur.fetchone()
    if not row:
        return err("not_found", message=f"id={tid} 不存在")
    if row["voided"]:
        return err("voided", message="该笔已作废，不能修改")
    before = _row(row)
    after = dict(before)
    for k, v in patch.items():
        after[k] = v
    try:
        date = _norm_date(after["date"]); amount = _amount(after["amount"])
        period = _period(after.get("period"), date)
        direction = after["direction"]
        if direction not in ("收入", "支出", "内部转账"):
            raise ValueError("direction 非法")
        if direction == "内部转账":
            if not after.get("transfer_to") or after["transfer_to"] == after["account"]:
                raise ValueError("内部转账需 transfer_to 且不同于 account")
        else:
            after["transfer_to"] = None
        category, account = after["category"], after["account"]
    except (ValueError, TypeError) as e:
        return err("invalid_input", message=str(e))
    if not _active_category(cur, category, direction):
        return err("category_invalid")
    if not _active_account(cur, account):
        return err("account_invalid")
    if _account_closed(cur, period, account):
        return err("period_closed", message="目标账期已封账，请先 reopen_period")

    fp = fingerprint(direction, date, amount, account, after.get("transfer_to"), category, after.get("note") or "")
    cur.execute("SELECT id FROM transactions WHERE fingerprint=? AND id!=?", (fp, tid))
    if cur.fetchone():
        return err("duplicate", message="修改后将与其他记录指纹冲突")
    cur.execute("""UPDATE transactions SET date=?, period=?, direction=?, transfer_to=?,
                   category=?, amount=?, account=?, note=?, fingerprint=? WHERE id=?""",
                (date, period, direction, after.get("transfer_to"), category,
                 amount, account, after.get("note") or "", fp, tid))
    conn.commit()
    backup()
    cur.execute("SELECT * FROM transactions WHERE id=?", (tid,))
    return ok({"before": before, "after": _row(cur.fetchone())})


def void_transaction(conn, tid: int, reason: str = "") -> dict:
    cur = conn.cursor()
    cur.execute("SELECT period, account, voided FROM transactions WHERE id=?", (tid,))
    row = cur.fetchone()
    if not row:
        return err("not_found")
    if row["voided"]:
        return err("voided", message="已作废")
    if _account_closed(cur, row["period"], row["account"]):
        return err("period_closed", message="封账账期请先 reopen_period")
    cur.execute("UPDATE transactions SET voided=1, note=note||? WHERE id=?",
                (f"  [作废:{reason}]" if reason else "  [作废]", tid))
    conn.commit()
    backup()
    return ok({"voided": True})


# ---------- 配置维护 ----------

def add_category(conn, name: str, direction: str, aliases: list[str] | None = None) -> dict:
    if direction not in ("收入", "支出", "内部转账"):
        return err("direction_invalid")
    cur = conn.cursor()
    cur.execute("SELECT active FROM categories WHERE name=? AND direction=?", (name, direction))
    row = cur.fetchone()
    if row:
        if row["active"]:
            return err("exists", message=f"分类 {name}/{direction} 已存在")
        conn.execute("UPDATE categories SET active=1 WHERE name=? AND direction=?", (name, direction))
        conn.commit()
        return ok({"revived": True})
    if direction == "内部转账" and name != "内部流转":
        return err("reserved", message="内部转账方向仅允许系统分类'内部流转'")
    conn.execute("INSERT INTO categories(name, direction, aliases) VALUES(?,?,?)",
                 (name, direction, json.dumps(aliases or [], ensure_ascii=False)))
    conn.commit()
    return ok({})


def add_account(conn, name: str, kind: str, aliases: list[str] | None = None) -> dict:
    if kind not in ("bank", "wallet"):
        return err("kind_invalid", message="kind: bank/wallet")
    cur = conn.cursor()
    cur.execute("SELECT active FROM accounts WHERE name=?", (name,))
    row = cur.fetchone()
    if row:
        if row["active"]:
            return err("exists", message=f"账户 {name} 已存在")
        conn.execute("UPDATE accounts SET active=1 WHERE name=?", (name,))
        conn.commit()
        return ok({"revived": True})
    conn.execute("INSERT INTO accounts(name, kind, aliases) VALUES(?,?,?)",
                 (name, kind, json.dumps(aliases or [], ensure_ascii=False)))
    conn.commit()
    return ok({})


def set_category_active(conn, name: str, direction: str, active: bool) -> dict:
    cur = conn.execute("UPDATE categories SET active=? WHERE name=? AND direction=?",
                       (1 if active else 0, name, direction))
    if cur.rowcount == 0:
        return err("not_found")
    conn.commit()
    return ok({})


def set_account_active(conn, name: str, active: bool) -> dict:
    cur = conn.execute("UPDATE accounts SET active=? WHERE name=?", (1 if active else 0, name))
    if cur.rowcount == 0:
        return err("not_found")
    conn.commit()
    return ok({})


def _rename_tx_account(conn, old: str, new: str) -> None:
    conn.execute("UPDATE transactions SET account=? WHERE account=?", (new, old))
    conn.execute("UPDATE transactions SET transfer_to=? WHERE transfer_to=?", (new, old))
    conn.execute("UPDATE balance_snapshots SET account=? WHERE account=?", (new, old))

def rename_category(conn, old: str, new: str, direction: str) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT aliases FROM categories WHERE name=? AND direction=?", (old, direction))
    row = cur.fetchone()
    if not row:
        return err("not_found")
    old_aliases = load_aliases(row["aliases"])
    aliases = json.dumps([a for a in ([old] + old_aliases) if a != new], ensure_ascii=False)
    cur.execute("UPDATE categories SET name=?, aliases=? WHERE name=? AND direction=?",
                (new, aliases, old, direction))
    cur.execute("UPDATE transactions SET category=? WHERE category=? AND direction=?",
                (new, old, direction))
    conn.commit()
    backup()
    return ok({"migratedTransactions": cur.rowcount, "aliases": load_aliases(aliases)})

def rename_account(conn, old: str, new: str) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT aliases FROM accounts WHERE name=?", (old,))
    row = cur.fetchone()
    if not row:
        return err("not_found")
    cur.execute("SELECT 1 FROM accounts WHERE name=?", (new,))
    if cur.fetchone():
        return err("exists", message=f"账户 {new} 已存在")
    old_aliases = load_aliases(row["aliases"])
    aliases = json.dumps([a for a in ([old] + old_aliases) if a != new], ensure_ascii=False)
    conn.execute("UPDATE accounts SET name=?, aliases=? WHERE name=?", (new, aliases, old))
    _rename_tx_account(conn, old, new)
    conn.commit()
    backup()
    return ok({"migratedTransactions": cur.rowcount})

def add_aliases(conn, target: dict, aliases: list[str], append: bool = True) -> dict:
    if not aliases:
        return err("empty_aliases")
    cur = conn.cursor()
    t = target.get("type")
    if t == "category":
        cur.execute("SELECT aliases FROM categories WHERE name=? AND direction=?",
                    (target["name"], target["direction"]))
    elif t == "account":
        cur.execute("SELECT aliases FROM accounts WHERE name=?", (target["name"],))
    else:
        return err("target_invalid")
    row = cur.fetchone()
    if not row:
        return err("not_found")
    merged = load_aliases(row["aliases"]) + [a for a in aliases if a not in load_aliases(row["aliases"])]
    final = merged if append else list(aliases)
    payload = json.dumps(final, ensure_ascii=False)
    if t == "category":
        conn.execute("UPDATE categories SET aliases=? WHERE name=? AND direction=?",
                     (payload, target["name"], target["direction"]))
    else:
        conn.execute("UPDATE accounts SET aliases=? WHERE name=?", (payload, target["name"]))
    conn.commit()
    return ok({"aliases": final})

def list_config(conn) -> dict:
    cats = [{"name": r["name"], "direction": r["direction"],
             "aliases": load_aliases(r["aliases"]), "active": bool(r["active"])}
            for r in conn.execute("SELECT * FROM categories ORDER BY direction, name")]
    accts = [{"name": r["name"], "kind": r["kind"],
              "aliases": load_aliases(r["aliases"]), "active": bool(r["active"])}
             for r in conn.execute("SELECT * FROM accounts ORDER BY kind, name")]
    return ok({"categories": cats, "accounts": accts})

def get_category_whitelist(conn) -> dict:
    expense = [r["name"] for r in conn.execute(
        "SELECT name FROM categories WHERE direction='支出' AND active=1 ORDER BY name")]
    income = [r["name"] for r in conn.execute(
        "SELECT name FROM categories WHERE direction='收入' AND active=1 ORDER BY name")]
    accounts = [r["name"] for r in conn.execute(
        "SELECT name FROM accounts WHERE active=1 ORDER BY kind DESC, name")]
    return ok({"expense": expense, "income": income, "accounts": accounts})

def match_account(conn, word: str) -> dict:
    """槽位推断辅助：按名称/别名匹配 active 账户。"""
    hits = _account_candidates(conn.cursor(), (word or "").strip())
    return ok({"word": word, "matches": hits})


# ---------- 查询类 ----------

def query_transactions(conn, f: dict | None = None) -> dict:
    f = f or {}
    # includeVoided=true 时包含作废行（留痕核查用）；默认排除
    where, args = ([] if f.get("includeVoided") else ["voided=0"]), []
    if f.get("period"):
        where.append("period=?"); args.append(f["period"])
    if f.get("dateFrom"):
        where.append("date>=?"); args.append(f["dateFrom"])
    if f.get("dateTo"):
        where.append("date<=?"); args.append(f["dateTo"])
    if f.get("category"):
        where.append("category=?"); args.append(f["category"])
    if f.get("account"):
        where.append("account=? OR transfer_to=?"); args += [f["account"], f["account"]]
    if f.get("direction"):
        where.append("direction=?"); args.append(f["direction"])
    if f.get("noteContains"):
        where.append("note LIKE ?"); args.append(f"%{f['noteContains']}%")
    limit = min(int(f.get("limit", 50)), 500)
    cond = " AND ".join(where) if where else "1=1"
    sql = f"SELECT * FROM transactions WHERE {cond} ORDER BY date DESC, id DESC LIMIT {limit}"
    rows = [_row(r) for r in conn.execute(sql, args)]
    total = conn.execute(f"SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM transactions WHERE {cond}", args).fetchone()
    return ok({"rows": rows, "total": total["c"], "sumAmount": round(total["s"], 2)})

def summary_period(conn, period: str) -> dict:
    cur = conn.cursor()
    cur.execute("""SELECT category, COUNT(*) c, SUM(amount) s FROM transactions
                   WHERE period=? AND voided=0 AND direction='支出'
                   GROUP BY category ORDER BY s DESC""", (period,))
    by_cat = [{"category": r["category"], "total": round(r["s"], 2), "count": r["c"]} for r in cur.fetchall()]
    cur.execute("""SELECT account,
                          SUM(CASE WHEN direction IN ('收入') THEN amount ELSE 0 END) inflow,
                          SUM(CASE WHEN direction IN ('支出') THEN amount ELSE 0 END) outflow
                   FROM transactions WHERE period=? AND voided=0
                     AND direction IN ('收入','支出') GROUP BY account""", (period,))
    by_acct = [{"account": r["account"], "inflow": round(r["inflow"] or 0, 2),
                "outflow": round(r["outflow"] or 0, 2)} for r in cur.fetchall()]
    net = conn.execute("""SELECT
        COALESCE(SUM(CASE WHEN direction='收入' THEN amount ELSE 0 END),0) inc,
        COALESCE(SUM(CASE WHEN direction='支出' THEN amount ELSE 0 END),0) exp
        FROM transactions WHERE period=? AND voided=0""", (period,)).fetchone()
    return ok({"byCategory": by_cat, "byAccount": by_acct,
               "netIncome": round(net["inc"] - net["exp"], 2),
               "income": round(net["inc"], 2), "expense": round(net["exp"], 2)})


# ---------- 可视化报表（t2 §3.2 report*，chart-ready JSON）----------

def _periods(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT period FROM transactions ORDER BY period")]

def report_category_breakdown(conn, inp: dict) -> dict:
    period = inp.get("period", "")
    direction = inp.get("direction", "支出")
    if direction not in ("收入", "支出"):
        return err("direction_invalid")
    top_n = int(inp.get("topN", 0) or 0)
    cur = conn.execute("""SELECT category, COUNT(*) c, SUM(amount) s FROM transactions
        WHERE period=? AND voided=0 AND direction=?
        GROUP BY category ORDER BY s DESC""", (period, direction))
    items = [{"name": r["category"], "amount": round(r["s"], 2),
              "count": r["c"]} for r in cur.fetchall()]
    total = round(sum(i["amount"] for i in items), 2)
    for i in items:
        i["pct"] = round(i["amount"] / total * 100, 1) if total else 0.0
    if top_n:
        items = items[:top_n]
    return ok({"kind": "pie", "unit": "CNY", "items": items, "total": total})

def report_trend(conn, inp: dict) -> dict:
    months = max(1, min(int(inp.get("months", 6)), 60))
    category = inp.get("category")
    periods = _periods(conn)[-months:]
    series = []
    for p in periods:
        args: list[Any] = [p, category] if category else [p]
        cat_sql = " AND category=?" if category else ""
        r = conn.execute(f"""SELECT
            COALESCE(SUM(CASE WHEN direction='收入' THEN amount ELSE 0 END),0) inc,
            COALESCE(SUM(CASE WHEN direction='支出' THEN amount ELSE 0 END),0) exp
            FROM transactions WHERE period=? AND voided=0{cat_sql}""", args).fetchone()
        series.append({"period": p, "income": round(r["inc"], 2),
                       "expense": round(r["exp"], 2),
                       "net": round(r["inc"] - r["exp"], 2)})
    return ok({"kind": "line", "series": series})

def report_account_flow(conn, period: str) -> dict:
    accounts = [r[0] for r in conn.execute(
        "SELECT account FROM balance_snapshots WHERE period=? UNION "
        "SELECT account FROM transactions WHERE period=? AND voided=0 "
        "UNION SELECT transfer_to FROM transactions WHERE period=? AND voided=0 AND direction='内部转账'",
        (period, period, period))]
    items = []
    for a in sorted(set(accounts)):
        r = conn.execute("""SELECT
            COALESCE(SUM(CASE WHEN direction='收入' AND account=:acc THEN amount ELSE 0 END),0) inc,
            COALESCE(SUM(CASE WHEN direction='支出' AND account=:acc THEN amount ELSE 0 END),0) out,
            COALESCE(SUM(CASE WHEN direction='内部转账' AND account=:acc THEN amount ELSE 0 END),0) tout,
            COALESCE(SUM(CASE WHEN direction='内部转账' AND transfer_to=:acc THEN amount ELSE 0 END),0) tin
            FROM transactions WHERE period=:p AND voided=0""",
            {"acc": a, "p": period}).fetchone()
        inflow = round(r["inc"] + r["tin"], 2)
        outflow = round(r["out"] + r["tout"], 2)
        items.append({"account": a, "inflow": inflow, "outflow": outflow,
                      "net": round(inflow - outflow, 2),
                      "transfersIn": round(r["tin"], 2),
                      "transfersOut": round(r["tout"], 2)})
    return ok({"kind": "bar", "period": period, "items": items})

def report_daily_spend(conn, period: str) -> dict:
    rows = conn.execute("""SELECT date, COUNT(*) c, SUM(amount) s FROM transactions
        WHERE period=? AND voided=0 AND direction='支出'
        GROUP BY date ORDER BY date""", (period,)).fetchall()
    days = [{"date": r["date"], "amount": round(r["s"], 2), "count": r["c"]} for r in rows]
    mx = max(days, key=lambda d: d["amount"]) if days else {"date": "", "amount": 0}
    return ok({"kind": "calendar", "period": period, "days": days, "maxDay": mx})

def report_detail(conn, f: dict | None = None) -> dict:
    f = dict(f or {})
    page = max(1, int(f.pop("page", 1)))
    page_size = min(200, int(f.pop("pageSize", 50)))
    sort_by = f.pop("sortBy", "date")
    order = "ASC" if f.pop("order", "desc") == "asc" else "DESC"
    sort_sql = "amount" if sort_by == "amount" else "date"
    base = query_transactions(conn, {**f, "limit": 100000})
    rows = base["data"]["rows"]
    rows.sort(key=lambda r: (r[sort_by] if sort_by in ("date", "amount") else r["date"]), reverse=(order == "DESC"))
    total = len(rows)
    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]
    return ok({"kind": "table", "columns": list(page_rows[0].keys()) if page_rows else [],
               "rows": page_rows, "total": total,
               "sumAmount": round(sum(r["amount"] for r in rows), 2),
               "page": page, "pageSize": page_size})

def report_period_compare(conn, period: str) -> dict:
    periods = _periods(conn)
    if period not in periods:
        return err("period_not_found", candidates=periods)
    idx = periods.index(period)
    prev = periods[idx - 1] if idx > 0 else None
    def _s(p):
        if not p:
            return {"income": 0, "expense": 0}
        r = conn.execute("""SELECT COALESCE(SUM(CASE WHEN direction='收入' THEN amount ELSE 0 END),0) inc,
                                   COALESCE(SUM(CASE WHEN direction='支出' THEN amount ELSE 0 END),0) exp
                            FROM transactions WHERE period=? AND voided=0""", (p,)).fetchone()
        return {"income": round(r["inc"], 2), "expense": round(r["exp"], 2)}
    cur_s, prev_s = _s(period), _s(prev)
    changes = []
    for r in conn.execute("""SELECT category, SUM(amount) s FROM transactions
            WHERE period=? AND voided=0 AND direction='支出' GROUP BY category""", (period,)):
        pc = conn.execute("SELECT COALESCE(SUM(amount),0) s FROM transactions WHERE period=? AND voided=0 AND direction='支出' AND category=?", (prev, r["category"])).fetchone() if prev else {"s": 0}
        delta = round(r["s"] - (pc["s"] if prev else 0), 2)
        pct = round(delta / pc["s"] * 100, 1) if prev and pc["s"] else 0.0
        changes.append({"category": r["category"], "amount": round(r["s"], 2),
                        "delta": delta, "deltaPct": pct})
    changes.sort(key=lambda c: -abs(c["delta"]))
    return ok({"period": period, "prevPeriod": prev,
               "income": cur_s["income"], "expense": cur_s["expense"],
               "netIncome": round(cur_s["income"] - cur_s["expense"], 2),
               "prev": prev_s, "topChanges": changes[:5]})
