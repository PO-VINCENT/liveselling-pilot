"""SQLite connection helper. One connection per request to keep things simple."""
import os
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "data" / "catalog.db"
DB_PATH = Path(os.getenv("DB_PATH", DEFAULT_DB))

def get_conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con
