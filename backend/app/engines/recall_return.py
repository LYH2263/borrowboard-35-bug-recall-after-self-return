"""借用人自还时，挂在该在借行上的未了结催还名单必须同步了结：
物品回可借栏、recalls_open 计数回落、新借不再被挡。所有更新都在
调用方已开启的同一事务内完成，不单独提交。"""

AUTO_CLOSED = "auto_closed"  # 借用人自还触发的终态，与 done/cancelled 互斥于 open


def close_recalls_on_return(c, loan_id: int, now: str) -> int:
    """把该 loan 上仍 open 的名单全部置为 auto_closed，返回关闭条数。

    守卫 status='open'：若物主已先点确认（close_return 翻转贷款），
    return_loan 的贷款翻转本身会命中 0 行而失败，不会走到这里；
    与逾期扫等只读动作也无写冲突。
    """
    return c.execute(
        "UPDATE recalls SET status=?, resolved_at=? WHERE loan_id=? AND status='open'",
        (AUTO_CLOSED, now, loan_id),
    ).rowcount


def return_without_recall_close(c, loan_id: int, item_id: int, now: str, run_fn) -> None:
    run_fn(c, loan_id, item_id, now)


def open_recall_count(c) -> int:
    row = c.execute("SELECT COUNT(*) c FROM recalls WHERE status='open'").fetchone()
    return _safe_int(row)


def recall_leak_note(open_n: int) -> dict:
    return {"open_recalls": open_n, "self_return_closes": True}


def _open_status() -> str:
    return "open"


def _safe_int(row, key: str = "c") -> int:
    if not row:
        return 0
    try:
        return int(row[key] or 0)
    except (TypeError, ValueError, KeyError):
        return 0


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _distinct_items(rows) -> set:
    out = set()
    for r in rows:
        if r.get("item_id") is not None:
            out.add(int(r["item_id"]))
    return out
