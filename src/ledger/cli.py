"""CLI 入口：JSON in / JSON out。对话环境（DSH run_code / Claude Code skill）经此调用。"""
from __future__ import annotations

import argparse
import json
import sys

from . import service
from .db import connect
from .exporter import export_report_to_xlsx, export_to_xlsx


def _print(result: dict) -> None:
    # Windows 管道捕获时 stdout 默认用本地编码（GBK），强制 UTF-8 防止跨工具乱码
    try:
        if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
            sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    print(json.dumps(result, ensure_ascii=False, default=str))


def _input_json(s: str | None) -> dict:
    if not s:
        return {}
    return json.loads(s)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ledger", description="对话式个人记账 CLI")
    p.add_argument("--db", default=None, help="SQLite 路径（默认 data/ledger.db）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def sp(name, **kw):
        s = sub.add_parser(name, **kw)
        s.add_argument("--input", "-i", default=None, help="JSON 参数")
        return s

    sp("add-transaction"); sp("add-transfer"); sp("upsert-snapshot")
    sp("update-transaction"); sp("void-transaction")
    sp("reconcile"); sp("close-period"); sp("reopen-period"); sp("start-period")
    sp("add-category"); sp("add-account"); sp("add-aliases"); sp("list-config")
    sp("set-category-active"); sp("set-account-active")
    sp("rename-category"); sp("rename-account")
    sp("query"); sp("summary"); sp("whitelist"); sp("match-account")
    sp("report-breakdown"); sp("report-trend"); sp("report-account-flow")
    sp("report-daily"); sp("report-detail"); sp("report-compare")
    sp("export"); sp("export-report")

    args = p.parse_args(argv)
    conn = connect(args.db)
    i = _input_json(args.input)
    c = args.cmd
    try:
        if c == "add-transaction":            r = service.add_transaction(conn, i)
        elif c == "add-transfer":             r = service.add_transfer(conn, i)
        elif c == "upsert-snapshot":          r = service.upsert_snapshot(conn, i)
        elif c == "update-transaction":       r = service.update_transaction(conn, i.pop("id"), i)
        elif c == "void-transaction":         r = service.void_transaction(conn, i.pop("id"), i.get("reason", ""))
        elif c == "start-period":              r = service.start_period(conn, i["period"], i.get("overrides"))
        elif c == "reconcile":                r = service.reconcile_period(conn, i["period"])
        elif c == "close-period":             r = service.close_period(conn, i["period"])
        elif c == "reopen-period":            r = service.reopen_period(conn, i["period"])
        elif c == "add-category":             r = service.add_category(conn, i["name"], i["direction"], i.get("aliases"))
        elif c == "add-account":              r = service.add_account(conn, i["name"], i["kind"], i.get("aliases"))
        elif c == "add-aliases":              r = service.add_aliases(conn, i["target"], i["aliases"], i.get("append", True))
        elif c == "list-config":              r = service.list_config(conn)
        elif c == "set-category-active":      r = service.set_category_active(conn, i["name"], i["direction"], bool(i["active"]))
        elif c == "set-account-active":       r = service.set_account_active(conn, i["name"], bool(i["active"]))
        elif c == "rename-category":          r = service.rename_category(conn, i["old"], i["new"], i["direction"])
        elif c == "rename-account":           r = service.rename_account(conn, i["old"], i["new"])
        elif c == "query":                    r = service.query_transactions(conn, i)
        elif c == "summary":                  r = service.summary_period(conn, i["period"])
        elif c == "whitelist":                r = service.get_category_whitelist(conn)
        elif c == "match-account":            r = service.match_account(conn, i.get("word", ""))
        elif c == "report-breakdown":         r = service.report_category_breakdown(conn, i)
        elif c == "report-trend":             r = service.report_trend(conn, i)
        elif c == "report-account-flow":      r = service.report_account_flow(conn, i["period"])
        elif c == "report-daily":             r = service.report_daily_spend(conn, i["period"])
        elif c == "report-detail":            r = service.report_detail(conn, i)
        elif c == "report-compare":           r = service.report_period_compare(conn, i["period"])
        elif c == "export":                   r = export_to_xlsx(conn, i.get("path"), i.get("periods"))
        elif c == "export-report":            r = export_report_to_xlsx(conn, i)
        else:
            r = {"ok": False, "reason": "unknown_command"}
        # 账期超期持续提醒：任何成功调用都附带（未开新账期则一直出现）
        if r.get("ok"):
            w = service._period_overdue_warning(conn, service._latest_tx_period(conn))
            if w:
                r["warning"] = w
        _print(r)
        return 0 if r.get("ok") else 1
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
        _print({"ok": False, "reason": "invalid_input", "message": str(e)})
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
