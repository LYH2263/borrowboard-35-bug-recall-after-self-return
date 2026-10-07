"""借用人自还时，同一事务内把挂在该在借上的未了结工单全部结清，
保证 loans 已 returned 时不会残留 open 工单（名单条、计数、可借栏一致）。"""

def close_recalls_on_return(c, loan_id: int, now: str) -> int:
    n = c.execute(
        "UPDATE recalls SET status='done', resolved_at=? "
        "WHERE loan_id=? AND status='open'",
        (now, loan_id),
    ).rowcount
    return int(n or 0)
