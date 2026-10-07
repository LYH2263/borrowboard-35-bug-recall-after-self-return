"""催还名单生命周期回归：自还关单、确认/自还撞车、双名单拦截、发起失败回滚。"""
import pytest

from app import seed, store
from app.db import connect
from app.engines import recall_return as rr
from app.store import DomainError


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    c = connect()
    c.execute("DELETE FROM recalls")
    c.execute("DELETE FROM loans")
    c.execute("DELETE FROM items")
    c.commit()
    c.close()
    return tmp_path


def mkitem(title="电钻", owner="老周"):
    c = connect()
    cur = c.execute("INSERT INTO items(title,owner,status,data_quality) VALUES (?,?,?,?)",
                    (title, owner, "available", "clean"))
    c.commit()
    iid = cur.lastrowid
    c.close()
    return iid


def q1(sql, args=()):
    c = connect()
    row = c.execute(sql, args).fetchone()
    c.close()
    return dict(row) if row else None


def qall(sql, args=()):
    c = connect()
    rows = [dict(r) for r in c.execute(sql, args)]
    c.close()
    return rows


def lend_return(iid, borrower="邻居甲", due="2026-12-31"):
    return store.lend_item(iid, borrower, due)


def expect_error(fn, reason):
    with pytest.raises(DomainError) as ei:
        fn()
    assert ei.value.reason == reason


# 1. 自还成功 → 挂着的未了结名单必须同步了结，物品放出、计数回落、可再借
def test_self_return_closes_open_recall(db):
    iid = mkitem()
    lid = lend_return(iid)
    rid = store.initiate_recall(lid, "remind")
    assert q1("SELECT status FROM items WHERE id=?", (iid,))["status"] == "on_loan"

    store.return_loan(lid)

    assert q1("SELECT status FROM loans WHERE id=?", (lid,))["status"] == "returned"
    assert q1("SELECT status FROM recalls WHERE id=?", (rid,))["status"] == "auto_closed"
    assert q1("SELECT status FROM items WHERE id=?", (iid,))["status"] == "available"
    c = connect()
    assert rr.open_recall_count(c) == 0
    c.close()
    # 新借不再被残留名单拒绝
    lid2 = store.lend_item(iid, "邻居乙", "2026-12-31")
    assert lid2 != lid
    assert q1("SELECT status FROM loans WHERE id=?", (lid2,))["status"] == "active"


# 2. 名单未了结前：即使贷款已了结、可借栏已放出该物（修前自还泄漏的脏状态），再借也必须失败
def test_lend_blocked_while_recall_open_even_if_item_available(db):
    iid = mkitem()
    lid = lend_return(iid)
    store.initiate_recall(lid, "remind")
    # 模拟“借还记录已 returned、可借栏已放出该物而名单条未消”的脏状态
    c = connect()
    c.execute("UPDATE loans SET status='returned', returned_at='2026-10-01T00:00:00+00:00' WHERE id=?", (lid,))
    c.execute("UPDATE items SET status='available' WHERE id=?", (iid,))
    c.commit(); c.close()

    expect_error(lambda: store.lend_item(iid, "邻居乙", "2026-12-31"), "recall_open")
    # 没有留下半截：仍只有原在借行（returned），物品仍 available
    rows = qall("SELECT status FROM loans WHERE item_id=?", (iid,))
    assert len(rows) == 1 and rows[0]["status"] == "returned"
    assert q1("SELECT status FROM items WHERE id=?", (iid,))["status"] == "available"
    assert q1("SELECT COUNT(*) c FROM recalls WHERE loan_id=? AND status='open'", (lid,))["c"] == 1
    # 名单撤回后新借放行
    rid = q1("SELECT id FROM recalls WHERE loan_id=?", (lid,))["id"]
    store.cancel_recall(rid)
    lid2 = store.lend_item(iid, "邻居乙", "2026-12-31")
    assert q1("SELECT status FROM loans WHERE id=?", (lid2,))["status"] == "active"


# 3a. 同一在借不得挂两张未了结名单
def test_no_two_open_recalls_on_one_loan(db):
    iid = mkitem()
    lid = lend_return(iid)
    store.initiate_recall(lid, "remind")
    expect_error(lambda: store.initiate_recall(lid, "close_return"), "recall_already_open")
    assert q1("SELECT COUNT(*) c FROM recalls WHERE loan_id=?", (lid,))["c"] == 1


# 3b. 撤回后可重新发起
def test_cancel_allows_reinitiate(db):
    iid = mkitem()
    lid = lend_return(iid)
    rid = store.initiate_recall(lid, "remind")
    store.cancel_recall(rid)
    rid2 = store.initiate_recall(lid, "close_return")
    assert rid2 != rid
    assert q1("SELECT status FROM recalls WHERE id=?", (rid2,))["status"] == "open"


# 4a. confirm（当场结还）先到：贷款 returned，再自还失败，名单不挂两张
def test_confirm_close_return_wins(db):
    iid = mkitem()
    lid = lend_return(iid)
    rid = store.initiate_recall(lid, "close_return")
    assert store.confirm_recall(rid) == "close_return"
    assert q1("SELECT status FROM loans WHERE id=?", (lid,))["status"] == "returned"
    assert q1("SELECT status FROM items WHERE id=?", (iid,))["status"] == "available"
    assert q1("SELECT status FROM recalls WHERE id=?", (rid,))["status"] == "done"
    # 撞车的自还必须失败，不得产生第二种结局
    expect_error(lambda: store.return_loan(lid), "not_active")
    c = connect()
    assert rr.open_recall_count(c) == 0
    c.close()


# 4b. 自还先到：工单已 auto_closed，重复确认报错但贷款结局唯一、计数为 0
def test_self_return_wins_then_confirm_rejected(db):
    iid = mkitem()
    lid = lend_return(iid)
    rid = store.initiate_recall(lid, "close_return")
    store.return_loan(lid)
    expect_error(lambda: store.confirm_recall(rid), "recall_not_open")
    assert q1("SELECT status FROM loans WHERE id=?", (lid,))["status"] == "returned"
    assert q1("SELECT status FROM recalls WHERE id=?", (rid,))["status"] == "auto_closed"
    assert q1("SELECT COUNT(*) c FROM recalls WHERE status='open'")["c"] == 0


# 4c. 历史脏状态：贷款已 returned 而工单仍 open，确认必须把工单了结、不再卡死
def test_confirm_repairs_returned_loan_with_stuck_open_recall(db):
    iid = mkitem()
    lid = lend_return(iid)
    rid = store.initiate_recall(lid, "close_return")
    c = connect()
    c.execute("UPDATE loans SET status='returned', returned_at='2026-10-01T00:00:00+00:00' WHERE id=?", (lid,))
    c.execute("UPDATE items SET status='available' WHERE id=?", (iid,))
    c.commit(); c.close()

    assert store.confirm_recall(rid) == "close_return"
    assert q1("SELECT status FROM recalls WHERE id=?", (rid,))["status"] == "done"
    assert q1("SELECT COUNT(*) c FROM recalls WHERE status='open'")["c"] == 0


# 5. 只催确认：在借行与物品状态不动；之后自还正常关单
def test_remind_confirm_keeps_loan_active(db):
    iid = mkitem()
    lid = lend_return(iid)
    rid = store.initiate_recall(lid, "remind")
    assert store.confirm_recall(rid) == "remind"
    assert q1("SELECT status FROM loans WHERE id=?", (lid,))["status"] == "active"
    assert q1("SELECT status FROM items WHERE id=?", (iid,))["status"] == "on_loan"
    # done 工单不在未了结名单，自还不报错
    store.return_loan(lid)
    assert q1("SELECT COUNT(*) c FROM recalls WHERE status='open'")["c"] == 0


# 6. 发起失败：条数回到点前，应还日不被空名单改写，不留半截名单
def test_failed_initiate_changes_nothing(db):
    iid = mkitem()
    lid = lend_return(iid, due="2026-11-11")
    before = qall("SELECT * FROM recalls")
    store.return_loan(lid)

    expect_error(lambda: store.initiate_recall(lid, "remind"), "loan_not_active")
    assert qall("SELECT * FROM recalls") == before
    assert q1("SELECT due_date FROM loans WHERE id=?", (lid,))["due_date"] == "2026-11-11"

    # 非法效果同样不落库
    lid2 = lend_return(mkitem(title="梯子"))
    expect_error(lambda: store.initiate_recall(lid2, "bogus"), "bad_effect")
    assert q1("SELECT COUNT(*) c FROM recalls WHERE loan_id=?", (lid2,))["c"] == 0


# 7. 普通在借（无名单）归还不受影响
def test_plain_return_unaffected(db):
    iid = mkitem()
    lid = lend_return(iid)
    store.return_loan(lid)
    assert q1("SELECT status FROM loans WHERE id=?", (lid,))["status"] == "returned"
    assert q1("SELECT status FROM items WHERE id=?", (iid,))["status"] == "available"
    assert q1("SELECT COUNT(*) c FROM recalls")["c"] == 0
