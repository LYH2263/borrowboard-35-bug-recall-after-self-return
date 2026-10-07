"""催还/自还/确认/撤回一致性测试。

只依赖 stdlib 与 app.store（sqlite3），用临时 DATA_DIR 隔离；
装了 pytest 时也能直接被收集。
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="bb-test-")
os.environ.setdefault("DATA_DIR", _tmp)

from app import seed, store  # noqa: E402
from app.engines.borrow_rules import classify_loans  # noqa: E402

seed.init_db()


def _make_loan():
    iid = None
    c = store.connect()
    cur = c.execute(
        "INSERT INTO items(title,owner,status,data_quality) VALUES (?,?,?,?)",
        ("测试物", "测试主", "available", "clean"))
    iid = cur.lastrowid
    c.commit(); c.close()
    lid = store.lend_item(iid, "测试邻居", "2099-01-01")
    return iid, lid


def _recall_status(rid):
    c = store.connect()
    st = c.execute("SELECT status FROM recalls WHERE id=?", (rid,)).fetchone()["status"]
    c.close()
    return st


def _item_status(iid):
    c = store.connect()
    st = c.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()["status"]
    c.close()
    return st


def _loan(lid):
    c = store.connect()
    row = dict(c.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone())
    c.close()
    return row


def _open_recalls(lid):
    c = store.connect()
    n = c.execute(
        "SELECT COUNT(*) c FROM recalls WHERE loan_id=? AND status='open'", (lid,)).fetchone()["c"]
    c.close()
    return n


# --- 1. 自还必须同时结清未了结工单并放回可借栏，随后可以再借 ---

def test_self_return_closes_open_recall_and_frees_item():
    iid, lid = _make_loan()
    rid = store.initiate_recall(lid, "remind")
    assert _recall_status(rid) == "open"
    assert _open_recalls(lid) == 1

    closed = store.return_loan(lid)
    assert closed == 1
    assert _recall_status(rid) == "done"
    assert _open_recalls(lid) == 0
    assert _loan(lid)["status"] == "returned"
    assert _item_status(iid) == "available"

    # 名单已了：邻居可以再借出
    lid2 = store.lend_item(iid, "邻居乙", "2099-02-01")
    assert _item_status(iid) == "on_loan"
    store.return_loan(lid2)


# --- 2. close_return 确认后，借用人重复归还/重复确认都失败，状态只有一种结果 ---

def test_confirm_close_return_then_double_ops_fail():
    iid, lid = _make_loan()
    rid = store.initiate_recall(lid, "close_return")
    effect = store.confirm_recall(rid)
    assert effect == "close_return"
    assert _loan(lid)["status"] == "returned"
    assert _item_status(iid) == "available"
    assert _recall_status(rid) == "done"

    def expect_error(fn, reason):
        try:
            fn()
        except store.DomainError as e:
            assert e.reason == reason, (e.reason, reason)
        else:
            raise AssertionError("expected DomainError " + reason)

    # 借用人再点归还：loans 已非 active，拒绝
    expect_error(lambda: store.return_loan(lid), "not_active")
    # 物主再点确认：工单已了结，拒绝
    expect_error(lambda: store.confirm_recall(rid), "recall_not_open")
    # 物主再点撤回：工单已了结，拒绝
    expect_error(lambda: store.cancel_recall(rid), "recall_not_open")


# --- 3. 自还抢先：再确认/撤回挂着的同一张工单必须失败（败方 409） ---

def test_return_then_confirm_or_cancel_loses():
    iid, lid = _make_loan()
    rid = store.initiate_recall(lid, "close_return")
    assert store.return_loan(lid) == 1

    def expect_error(fn, reason):
        try:
            fn()
        except store.DomainError as e:
            assert e.reason == reason
        else:
            raise AssertionError("expected DomainError " + reason)

    expect_error(lambda: store.confirm_recall(rid), "recall_not_open")
    expect_error(lambda: store.cancel_recall(rid), "recall_not_open")
    assert _item_status(iid) == "available"


# --- 4. 同一在借不得挂两张未了结名单；发起失败条数回到点前、应还日不变、无半条 ---

def test_duplicate_open_recall_rejected_and_atomic():
    iid, lid = _make_loan()
    before_due = _loan(lid)["due_date"]
    rid = store.initiate_recall(lid, "remind")

    try:
        store.initiate_recall(lid, "close_return")
    except store.DomainError as e:
        assert e.reason == "recall_already_open"
    else:
        raise AssertionError("second open recall must be rejected")

    assert _open_recalls(lid) == 1
    assert _loan(lid)["due_date"] == before_due  # 应还日不被改写
    assert _recall_status(rid) == "open"
    assert _item_status(iid) == "on_loan"  # 物品仍不在可借栏

    # 对已归还在借发起必须失败，且不留工单
    store.cancel_recall(rid)
    store.return_loan(lid)
    try:
        store.initiate_recall(lid, "remind")
    except store.DomainError as e:
        assert e.reason == "loan_not_active"
    else:
        raise AssertionError("recall on returned loan must be rejected")
    c = store.connect()
    n = c.execute("SELECT COUNT(*) c FROM recalls WHERE loan_id=? AND effect='remind' AND status='open'",
                  (lid,)).fetchone()["c"]
    c.close()
    assert n == 0


# --- 5. 撤回后名单条数下降，可重新发起；非法效果 400 且无半条 ---

def test_cancel_then_reinitiate_and_bad_effect():
    iid, lid = _make_loan()
    rid = store.initiate_recall(lid, "remind")
    store.cancel_recall(rid)
    assert _open_recalls(lid) == 0
    assert _recall_status(rid) == "cancelled"
    rid2 = store.initiate_recall(lid, "remind")
    assert _open_recalls(lid) == 1
    store.cancel_recall(rid2)

    try:
        store.initiate_recall(lid, "bogus")
    except store.DomainError as e:
        assert e.reason == "bad_effect" and e.status == 400
    else:
        raise AssertionError("bad effect must be rejected")
    assert _open_recalls(lid) == 0


# --- 6. 有未了结名单时直接借出必须失败（物品 on_loan，且无第二笔 active） ---

def test_lend_blocked_while_recall_open():
    iid, lid = _make_loan()
    store.initiate_recall(lid, "remind")
    try:
        store.lend_item(iid, "抢借邻居", "2099-03-01")
    except store.DomainError as e:
        assert e.reason in ("already_on_loan", "item_not_available")
    else:
        raise AssertionError("lend must be blocked while recall open")


# --- 7. 逾期分类不受影响 ---

def test_overdue_classification():
    iid, lid = _make_loan()
    L = _loan(lid)
    cls = classify_loans([{**L, "title": "x"}], "2026-10-07")
    assert len(cls["active"]) == 1
    c = store.connect()
    c.execute("UPDATE loans SET due_date=? WHERE id=?", ("2020-01-01", lid))
    c.commit(); c.close()
    L = _loan(lid)
    cls = classify_loans([{**L, "title": "x"}], "2026-10-07")
    assert len(cls["overdue"]) == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"{len(fns)} tests passed")
