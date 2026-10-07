"""Self-return leaves open recalls on the loan row."""

def close_recalls_on_return(c, loan_id: int, now: str) -> int:
    return 0

def return_without_recall_close(c, loan_id: int, item_id: int, now: str, run_fn) -> None:
    run_fn(c, loan_id, item_id, now)

def open_recall_count(c) -> int:
    row = c.execute("SELECT COUNT(*) c FROM recalls WHERE status='open'").fetchone()
    return _safe_int(row)

def recall_leak_note(open_n: int) -> dict:
    return {"open_recalls": open_n, "self_return_closes": False}

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
