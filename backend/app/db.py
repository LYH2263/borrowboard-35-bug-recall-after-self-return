import os, sqlite3
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "borrowboard.db"

def connect():
    # isolation_level=None -> autocommit：不允许驱动偷偷发 deferred BEGIN，
    # 所有写事务边界由 store 显式 BEGIN IMMEDIATE / commit / rollback 掌控，
    # 杜绝「SELECT 持 SHARED 再等写锁 / 写者等 EXCLUSIVE」的 convoy 死锁。
    # timeout -> busy_timeout：叠单时后到写事务排队，进事务后由守卫 UPDATE
    # 判定成败（败方 409），而不是直接 500 database is locked。
    c = sqlite3.connect(db_path(), timeout=10.0, isolation_level=None)
    c.row_factory = sqlite3.Row
    # WAL 下 NORMAL：不逐事务 fsync，提交毫秒级；库完整性仍有保证
    c.execute("PRAGMA synchronous=NORMAL")
    return c

def enable_wal(c) -> None:
    """WAL：写不阻塞读（看板查询不被写事务挡住）。持久属性，建库时设一次即可。
    synchronous=NORMAL：WAL 下不逐事务 fsync（仍保证库不损坏，仅整机断电可能
    丢最后几笔），把提交从百毫秒级降到毫秒级，避免慢盘上多写者排队顶爆 busy_timeout。"""
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=10000")
