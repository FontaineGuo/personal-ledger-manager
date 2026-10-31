"""验收测试（t2 §4 + v1.3~v1.4 补丁）：pytest 运行于临时库。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ledger import service, exporter
from ledger.db import connect


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    for n in ("餐饮美食", "杂项", "投资"):
        service.add_category(c, n, "支出")
    for n in ("工资收入", "报销收入"):
        service.add_category(c, n, "收入")
    service.add_account(c, "招行", "bank")
    service.add_account(c, "中行", "bank", aliases=["中行卡"])
    service.add_account(c, "微信余额", "wallet", aliases=["微信"])
    yield c
    c.close()


def tx(**kw):
    base = dict(date="2026-11-05", direction="支出", category="餐饮美食",
                amount=25.0, account="微信余额", note="午餐")
    base.update(kw)
    return base


# ---- 写入与幂等 ----
def test_add_and_duplicate(conn):
    r = service.add_transaction(conn, tx())
    assert r["ok"] and r["data"]["id"] == 1
    r2 = service.add_transaction(conn, tx())
    assert not r2["ok"] and r2["reason"] == "duplicate" and r2["existing"]["id"] == 1
    r3 = service.add_transaction(conn, tx(confirm=True))
    assert r3["ok"] and r3["data"]["id"] == 2   # 确认后允许第二笔

def test_whitelist_guard(conn):
    r = service.add_transaction(conn, tx(category="不存在"))
    assert not r["ok"] and r["reason"] == "category_invalid" and "餐饮美食" in r["candidates"]
    r = service.add_transaction(conn, tx(direction="收入", category="工资收入", amount=100))
    assert r["ok"]
    r = service.add_transaction(conn, tx(account="火星银行"))
    assert not r["ok"] and r["reason"] == "account_invalid"

def test_amount_bounds(conn):
    assert not service.add_transaction(conn, tx(amount=0))["ok"]
    assert not service.add_transaction(conn, tx(amount=2_000_000))["ok"]


# ---- 内部转账 ----
def test_transfer(conn):
    r = service.add_transfer(conn, dict(date="2026-11-05", **{"from": "招行", "to": "中行"},
                                        amount=2000, note="发零用钱"))
    assert r["ok"]
    flow = service.report_account_flow(conn, "2026-11")["data"]["items"]
    d = {i["account"]: i for i in flow}
    assert d["招行"]["transfersOut"] == 2000 and d["中行"]["transfersIn"] == 2000
    # 内部转账不计入收支统计
    s = service.summary_period(conn, "2026-11")["data"]
    assert s["expense"] == 0 and s["income"] == 0

def test_transfer_same_account(conn):
    r = service.add_transfer(conn, {"from": "招行", "to": "招行", "amount": 100})
    assert not r["ok"] and r["reason"] == "same_account"


# ---- 对账与封账 ----
def test_start_period_chain(conn):
    service.add_transaction(conn, tx(amount=100))
    service.upsert_snapshot(conn, dict(period="2026-11", account="微信余额", opening=50, closing=-50))
    service.upsert_snapshot(conn, dict(period="2026-11", account="招行", opening=1000, closing=800))
    # 中行缺上期快照：先被拦截，提供 overrides 后放行
    assert service.start_period(conn, "2026-12")["reason"] == "missing_prev_snapshot"
    r = service.start_period(conn, "2026-12", overrides={"中行": [200, 200]})
    assert r["ok"] and r["data"]["manualOverrides"] == ["中行"]
    assert r["data"]["chainedFromPrev"] == ["微信余额", "招行"]  # closing 接力为 opening
    # 新账期已可记账（closing 暂与 opening 相同，不影响写入）
    assert service.add_transaction(conn, tx(amount=10, note="12月"))["ok"]
    # 无上期快照的账户需人工提供
    service.add_account(conn, "平安银行", "bank")
    r2 = service.start_period(conn, "2027-01", overrides={"平安银行": [500, 500]})
    assert r2["ok"] and r2["data"]["manualOverrides"] == ["平安银行"]
    assert not service.start_period(conn, "2026-12")["ok"]        # 不能开更早/重复账期


def test_period_overdue_warning(conn):
    from datetime import date, timedelta
    old = (date.today() - timedelta(days=40)).isoformat()
    # 账期第一笔在 40 天前 → 超期，写入成功但带 warning
    r = service.add_transaction(conn, tx(date=old, period="2026-09", note="账期起点"))
    assert r["ok"] and r["data"]["warning"]["type"] == "period_overdue"
    assert r["data"]["warning"]["daysUsed"] >= 40
    # 同账期再记 → 持续提醒
    r2 = service.add_transaction(conn, tx(date=old, period="2026-09", note="第二笔", confirm=True))
    assert r2["ok"] and r2["data"]["warning"]["period"] == "2026-09"
    # 开新账期后（快照已存在更新账期），补记旧账期不再提醒
    for a in ("招行", "中行", "微信余额"):
        service.upsert_snapshot(conn, dict(period="2026-10", account=a, opening=0, closing=0))
    r3 = service.add_transaction(conn, tx(date=old, period="2026-09", note="补记", confirm=True))
    assert r3["ok"] and "warning" not in r3["data"]
    # 新账期未超期 → 无提醒
    r4 = service.add_transaction(conn, tx(note="新账期"))
    assert r4["ok"] and "warning" not in r4["data"]


def test_reconcile_close_reopen(conn):
    service.add_transaction(conn, tx(amount=100))
    service.upsert_snapshot(conn, dict(period="2026-11", account="微信余额", opening=50, closing=0))
    rec = service.reconcile_period(conn, "2026-11")
    d = next(a for a in rec["data"]["accounts"] if a["account"] == "微信余额")
    assert d["diff"] == 50.0 and not rec["data"]["balanced"]   # diff = 记录值-期望值 = 0-(-50)
    assert not service.close_period(conn, "2026-11")["ok"]          # 不平不能封
    service.upsert_snapshot(conn, dict(period="2026-11", account="微信余额", opening=50, closing=-50))
    assert service.close_period(conn, "2026-11")["ok"]
    r = service.add_transaction(conn, tx(amount=1, note="封账后补记"))
    assert not r["ok"] and r["reason"] == "period_closed"           # 封账禁插
    assert service.reopen_period(conn, "2026-11")["ok"]             # 重开账
    assert service.add_transaction(conn, tx(amount=1, note="重开后补记"))["ok"]


# ---- 纠错 ----
def test_update_and_void(conn):
    r = service.add_transaction(conn, tx())
    tid = r["data"]["id"]
    u = service.update_transaction(conn, tid, {"category": "杂项"})
    assert u["ok"] and u["data"]["before"]["category"] == "餐饮美食" and u["data"]["after"]["category"] == "杂项"
    v = service.void_transaction(conn, tid, "记错")
    assert v["ok"]
    q = service.query_transactions(conn, {})["data"]
    assert q["total"] == 0                                          # 作废不参与查询统计
    qv = service.query_transactions(conn, {"includeVoided": True})["data"]
    assert qv["total"] == 1 and qv["rows"][0]["voided"] == 1        # 但留痕可查
    assert "[作废:记错]" in qv["rows"][0]["note"]
    assert not service.void_transaction(conn, tid)["ok"]            # 重复作废拒绝


# ---- 配置维护 ----
def test_config_lifecycle(conn):
    assert service.add_category(conn, "宠物", "支出", ["猫粮"])["ok"]
    assert service.add_category(conn, "宠物", "支出")["reason"] == "exists"
    assert service.add_transaction(conn, tx(category="宠物", amount=45, note="猫粮"))["ok"]
    assert service.set_category_active(conn, "宠物", "支出", False)["ok"]
    wl = service.get_category_whitelist(conn)["data"]
    assert "宠物" not in wl["expense"]                              # 停用退出白名单
    q = service.query_transactions(conn, {"category": "宠物"})["data"]
    assert q["total"] == 1                                          # 但历史可查
    rn = service.rename_category(conn, "宠物", "宠物开销", "支出")
    assert rn["ok"]
    q2 = service.query_transactions(conn, {"category": "宠物开销"})["data"]
    assert q2["total"] == 1                                         # 改名迁移历史
    assert service.add_account(conn, "平安银行", "bank", ["平安卡"])["ok"]
    assert service.match_account(conn, "平安卡")["data"]["matches"] == ["平安银行"]  # 别名命中
    assert service.set_account_active(conn, "成都银行", False)["reason"] == "not_found"


# ---- 报表 ----
def test_account_flow_per_account(conn):
    service.add_transaction(conn, tx(amount=100, account="微信余额", note="w1"))
    service.add_transaction(conn, tx(amount=200, account="招行", note="z1"))
    service.add_transfer(conn, {"from": "招行", "to": "中行", "amount": 500,
                                "date": "2026-11-06"})
    items = {i["account"]: i for i in service.report_account_flow(conn, "2026-11")["data"]["items"]}
    # 口径：inflow/outflow 含内部转账双边（与勾稽一致），transfersIn/Out 为注记
    assert items["微信余额"]["outflow"] == 100 and items["微信余额"]["inflow"] == 0
    assert items["招行"]["outflow"] == 700 and items["招行"]["transfersOut"] == 500
    assert items["中行"]["inflow"] == 500 and items["中行"]["transfersIn"] == 500
    assert items["中行"]["net"] == 500 and items["招行"]["net"] == -700
    # 各账户净额互不相同，杜绝"全期总额重复"回归
    nets = {i["net"] for i in items.values()}
    assert len(nets) >= 3


def test_reports(conn):
    service.add_transaction(conn, tx(amount=100, date="2026-11-01", note="a"))
    service.add_transaction(conn, tx(amount=50, date="2026-10-02", note="b"))
    service.add_transaction(conn, tx(direction="收入", category="工资收入", amount=10000))
    pie = service.report_category_breakdown(conn, {"period": "2026-11"})["data"]
    assert pie["kind"] == "pie" and abs(pie["total"] - 100) < 0.01
    tr = service.report_trend(conn, {"months": 6})["data"]
    assert [s["period"] for s in tr["series"]] == ["2026-10", "2026-11"]
    daily = service.report_daily_spend(conn, "2026-10")["data"]
    assert daily["maxDay"]["amount"] == 50
    cmp_ = service.report_period_compare(conn, "2026-11")["data"]
    assert cmp_["netIncome"] == 10000 - 100
    assert cmp_["prev"]["expense"] == 50              # 环比口径含上一账期


# ---- 导出 ----
def test_exports(conn):
    service.add_transaction(conn, tx())
    r = exporter.export_to_xlsx(conn)
    assert r["ok"] and Path(r["data"]["path"]).exists()
    r2 = exporter.export_report_to_xlsx(conn, {"kind": "summary"})
    assert r2["ok"] and Path(r2["data"]["path"]).exists()
    r3 = exporter.export_report_to_xlsx(conn, {"kind": "category"})
    assert r3["ok"]
