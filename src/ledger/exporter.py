"""导出层：exportToXlsx（三表同构）与 exportReportToXlsx（报表 Excel 版）。依据 docs/t2 §3.3。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .result import err, ok
from . import service
from .db import DEFAULT_DB, _configured_db

HEADER_FONT = Font(bold=True)


def _export_dir(conn=None) -> Path:
    """导出目录跟随 db 实际位置（云路径部署时 export 也在网盘侧）。
    优先取 conn 实际连接的库文件（测试/多库场景不污染配置目录），无 conn 时回退配置解析。"""
    if conn is not None:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row and row[2]:
            return Path(row[2]).parent / "export"
    db_path = _configured_db() or DEFAULT_DB
    return db_path.parent / "export"


def _autosize(ws) -> None:
    for col in ws.columns:
        width = max((len(str(c.value or "")) + sum(1 for ch in str(c.value or "") if ord(ch) > 127) for c in col), default=8)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(width + 2, 50)


def export_to_xlsx(conn, path: str | None = None, periods: list[str] | None = None) -> dict:
    export_dir = _export_dir(conn)
    export_dir.mkdir(parents=True, exist_ok=True)
    path = Path(path) if path else export_dir / f"账本导出-{datetime.now():%Y%m%d-%H%M}.xlsx"
    wb = Workbook()

    # 交易流水
    ws = wb.active
    ws.title = "交易流水"
    ws.append(["日期", "账期", "方向", "分类", "金额", "账户", "转入账户", "备注", "来源", "作废"])
    for c in ws[1]:
        c.font = HEADER_FONT
    sql = "SELECT date, period, direction, category, amount, account, transfer_to, note, source, voided FROM transactions"
    args: list = []
    if periods:
        sql += f" WHERE period IN ({','.join('?' * len(periods))})"
        args = list(periods)
    for r in conn.execute(sql + " ORDER BY date, id", args):
        ws.append([r["date"], r["period"], r["direction"], r["category"], r["amount"],
                   r["account"], r["transfer_to"] or "", r["note"], r["source"],
                   "是" if r["voided"] else ""])
    tx_count = ws.max_row - 1

    # 余额快照
    ws2 = wb.create_sheet("余额快照")
    ws2.append(["账期", "账户", "月初余额", "月末余额", "已封账"])
    for c in ws2[1]:
        c.font = HEADER_FONT
    for r in conn.execute("SELECT period, account, opening, closing, closed FROM balance_snapshots ORDER BY period, account"):
        ws2.append([r["period"], r["account"], r["opening"], r["closing"], "是" if r["closed"] else ""])
    # 合计行
    ws2.append(["合计", "", "", f"=SUM(D2:D{ws2.max_row})", ""])

    # 配置表（与旧账本同构：三列白名单 + 别名注记 sheet）
    ws3 = wb.create_sheet("配置表")
    cats = list(conn.execute("SELECT name, direction FROM categories WHERE active=1 ORDER BY direction, name"))
    accts = list(conn.execute("SELECT name FROM accounts WHERE active=1 ORDER BY kind DESC, name"))
    ws3.append(["支出分类", "收入分类", "账户列表"])
    for c in ws3[1]:
        c.font = HEADER_FONT
    expense = [c["name"] for c in cats if c["direction"] == "支出"]
    income = [c["name"] for c in cats if c["direction"] == "收入"]
    for i in range(max(len(expense), len(income), len(accts))):
        ws3.append([expense[i] if i < len(expense) else "",
                    income[i] if i < len(income) else "",
                    accts[i]["name"] if i < len(accts) else ""])

    ws4 = wb.create_sheet("别名注记")
    ws4.append(["类型", "名称", "方向", "别名"])
    for c in ws4[1]:
        c.font = HEADER_FONT
    for r in conn.execute("SELECT name, direction, aliases FROM categories WHERE active=1"):
        if r["aliases"] not in ("[]", ""):
            ws4.append(["分类", r["name"], r["direction"], ", ".join(__import__("json").loads(r["aliases"]))])
    for r in conn.execute("SELECT name, aliases FROM accounts WHERE active=1"):
        if r["aliases"] not in ("[]", ""):
            ws4.append(["账户", r["name"], "", ", ".join(__import__("json").loads(r["aliases"]))])

    for w in (ws, ws2, ws3, ws4):
        _autosize(w)
    wb.save(path)
    return ok({"path": str(path), "txCount": tx_count})


def export_report_to_xlsx(conn, report: dict, path: str | None = None) -> dict:
    """report: {kind: summary|category|trend|accountFlow|detail, periods?, category?}"""
    export_dir = _export_dir(conn)
    export_dir.mkdir(parents=True, exist_ok=True)
    kind = report.get("kind")
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    path = Path(path) if path else export_dir / f"报表-{kind}-{stamp}.xlsx"
    periods = report.get("periods") or sorted({r[0] for r in conn.execute("SELECT DISTINCT period FROM transactions")})
    wb = Workbook()

    if kind == "summary":
        ws = wb.active; ws.title = "月度汇总"
        ws.append(["账期", "收入", "支出", "结余"])
        for c in ws[1]: c.font = HEADER_FONT
        for p in periods:
            s = service.summary_period(conn, p)["data"]
            ws.append([p, s["income"], s["expense"], s["netIncome"]])
        ws.append(["合计", f"=SUM(B2:B{ws.max_row})", f"=SUM(C2:C{ws.max_row})", f"=SUM(D2:D{ws.max_row})"])
    elif kind == "category":
        ws = wb.active; ws.title = "分类透视"
        ws.append(["账期", "分类", "金额", "笔数"])
        for c in ws[1]: c.font = HEADER_FONT
        for p in periods:
            for i in service.report_category_breakdown(conn, {"period": p})["data"]["items"]:
                ws.append([p, i["name"], i["amount"], i["count"]])
        ws.append(["合计", "", f"=SUM(C2:C{ws.max_row})", f"=SUM(D2:D{ws.max_row})"])
    elif kind == "trend":
        ws = wb.active; ws.title = "趋势"
        ws.append(["账期", "收入", "支出", "结余"])
        for c in ws[1]: c.font = HEADER_FONT
        t = service.report_trend(conn, {"months": 60})["data"]
        for s in t["series"]:
            if not periods or s["period"] in periods:
                ws.append([s["period"], s["income"], s["expense"], s["net"]])
    elif kind == "accountFlow":
        ws = wb.active; ws.title = "账户流水"
        ws.append(["账期", "账户", "流入", "流出", "净额", "转入(内部)", "转出(内部)"])
        for c in ws[1]: c.font = HEADER_FONT
        for p in periods:
            for i in service.report_account_flow(conn, p)["data"]["items"]:
                ws.append([p, i["account"], i["inflow"], i["outflow"], i["net"],
                           i["transfersIn"], i["transfersOut"]])
    elif kind == "detail":
        ws = wb.active; ws.title = "明细"
        d = service.report_detail(conn, {"period": periods[0] if len(periods) == 1 else None,
                                         "pageSize": 200, **({"category": report["category"]} if report.get("category") else {})})["data"]
        cols = ["id", "date", "period", "direction", "category", "amount",
                "account", "transfer_to", "note", "source"]
        ws.append(cols)
        for c in ws[1]: c.font = HEADER_FONT
        for r in d["rows"]:
            ws.append([r.get(c, "") for c in cols])
        ws.append(["", "", "", "", "合计", d["sumAmount"], "", "", "", ""])
    else:
        return err("kind_invalid", message="kind: summary|category|trend|accountFlow|detail")

    _autosize(ws)
    wb.save(path)
    return ok({"path": str(path), "sheets": wb.sheetnames})
