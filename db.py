# Слой БД CRM
import sqlite3
from config import DB_PATH, OWNER_ID


def execute_query(query, params=(), fetchone=False, fetchall=False):
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()
        cur.execute(query, params)
        if fetchone:
            return cur.fetchone()
        if fetchall:
            return cur.fetchall()
        conn.commit()


def init_db():
    execute_query(
        "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, role TEXT, is_busy INTEGER DEFAULT 0, plan INTEGER DEFAULT 0, fio TEXT, sphere TEXT, can_receive_leads INTEGER DEFAULT 0)"
    )
    execute_query(
        """CREATE TABLE IF NOT EXISTS leads
        (phone TEXT PRIMARY KEY, name TEXT, status TEXT,
         manager_id INTEGER, sphere TEXT, comment TEXT, touches INTEGER DEFAULT 0,
         last_touch DATETIME, is_answered INTEGER DEFAULT 0,
         direction TEXT, payment REAL DEFAULT 0, debt REAL DEFAULT 0,
         service TEXT, payment_date TEXT, massage_sessions INTEGER DEFAULT 0)"""
    )
    execute_query("CREATE TABLE IF NOT EXISTS auth_queue (phone TEXT PRIMARY KEY, step INTEGER DEFAULT 0, instance_sphere TEXT)")
    execute_query("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    execute_query(
        "CREATE TABLE IF NOT EXISTS appointments (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, time TEXT, doctor TEXT, phone TEXT)"
    )
    execute_query("CREATE TABLE IF NOT EXISTS pending_reg (user_id INTEGER PRIMARY KEY, fio TEXT)")
    execute_query(
        """CREATE TABLE IF NOT EXISTS lead_events
        (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, event_type TEXT, event_data TEXT, created_at TEXT, user_id INTEGER)"""
    )
    execute_query(
        "CREATE TABLE IF NOT EXISTS reminded_24h (phone TEXT, sphere TEXT, reminded_at TEXT, PRIMARY KEY (phone, sphere))"
    )
    execute_query(
        "CREATE TABLE IF NOT EXISTS follow_up_queue (phone TEXT PRIMARY KEY, direction TEXT, created_at TEXT, last_message TEXT)"
    )
    execute_query(
        "CREATE TABLE IF NOT EXISTS chat_history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, message_id INTEGER, phone TEXT, created_at TEXT, context TEXT)"
    )
    execute_query(
        "CREATE TABLE IF NOT EXISTS chat_sessions (user_id INTEGER, phone TEXT PRIMARY KEY, last_outgoing_at TEXT, reminder_sent INTEGER DEFAULT 0)"
    )
    for sql in [
        "ALTER TABLE chat_history ADD COLUMN context TEXT",
        "ALTER TABLE chat_sessions ADD COLUMN reminder_sent INTEGER DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN direction TEXT",
        "ALTER TABLE leads ADD COLUMN payment REAL DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN debt REAL DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN service TEXT",
        "ALTER TABLE leads ADD COLUMN payment_date TEXT",
        "ALTER TABLE leads ADD COLUMN massage_sessions INTEGER DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN created_at TEXT",
        "ALTER TABLE users ADD COLUMN sphere TEXT",
        "ALTER TABLE users ADD COLUMN can_receive_leads INTEGER DEFAULT 0",
        "ALTER TABLE auth_queue ADD COLUMN instance_sphere TEXT",
        "ALTER TABLE leads ADD COLUMN is_answered INTEGER DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN source TEXT",
        "ALTER TABLE leads ADD COLUMN closed_at TEXT",
        "ALTER TABLE leads ADD COLUMN stage_id INTEGER",
    ]:
        try:
            execute_query(sql)
        except Exception:
            pass
    execute_query(
        "CREATE TABLE IF NOT EXISTS lead_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, user_id INTEGER, text TEXT, created_at TEXT)"
    )
    execute_query(
        "CREATE TABLE IF NOT EXISTS lead_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, user_id INTEGER, text TEXT, due_at TEXT, done INTEGER DEFAULT 0)"
    )
    execute_query(
        "CREATE TABLE IF NOT EXISTS funnel_stages (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, direction TEXT, sort_order INTEGER DEFAULT 0)"
    )
    try:
        execute_query(
            "INSERT OR IGNORE INTO funnel_stages (name, direction, sort_order) VALUES ('Новый', 'biz', 1), ('В работе', 'biz', 2), ('Договор', 'biz', 3), ('Оплата', 'biz', 4), ('Закрыт', 'biz', 5)"
        )
    except Exception:
        pass
    execute_query("INSERT OR REPLACE INTO users (user_id, role, fio, sphere) VALUES (?, 'owner', 'Фаридун', NULL)", (OWNER_ID,))
    execute_query("INSERT OR IGNORE INTO settings (key, value) VALUES ('leads_enabled', '1')")
