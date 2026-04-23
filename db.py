import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "reminders.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            task TEXT NOT NULL,
            time TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def save_reminder(chat_id: int, task: str, time: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO reminders (chat_id, task, time) VALUES (?, ?, ?)",
        (chat_id, task, time)
    )
    conn.commit()
    reminder_id = cur.lastrowid
    conn.close()
    return reminder_id


def get_pending_reminders(chat_id: int) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM reminders WHERE chat_id = ? AND done = 0 ORDER BY time ASC",
        (chat_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_reminder_by_id(reminder_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_done(reminder_id: int):
    conn = get_conn()
    conn.execute("UPDATE reminders SET done = 1 WHERE id = ?", (reminder_id,))
    conn.commit()
    conn.close()
