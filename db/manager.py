import aiosqlite
import asyncio
import os

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_date DATETIME NOT NULL,
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'INR',
    merchant_raw TEXT NOT NULL,
    merchant_normalized TEXT,
    category TEXT DEFAULT 'Uncategorized',
    subcategory TEXT,
    account TEXT,
    instrument_last4 TEXT,
    payment_method TEXT,
    source_email_id TEXT UNIQUE,
    txn_ref TEXT UNIQUE,
    notes TEXT,
    is_recurring INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    parent_category TEXT
);

CREATE TABLE IF NOT EXISTS budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    period TEXT NOT NULL, -- 'monthly', 'yearly'
    amount_limit REAL NOT NULL,
    UNIQUE(category, period)
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    field TEXT NOT NULL, -- 'merchant_raw', 'notes'
    category TEXT NOT NULL,
    UNIQUE(pattern, field)
);

CREATE TABLE IF NOT EXISTS ingestion_log (
    email_id TEXT PRIMARY KEY,
    status TEXT, -- 'parsed', 'failed', 'ignored'
    processed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    error_message TEXT
);

-- FTS5 for fast merchant search
CREATE VIRTUAL TABLE IF NOT EXISTS transactions_fts USING fts5(
    merchant_raw,
    merchant_normalized,
    category,
    content='transactions',
    content_rowid='id'
);
"""

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, 'expense_tracker.db')

class DatabaseManager:
    def __init__(self, db_path=DEFAULT_DB_PATH):
        self.db_path = db_path

    async def initialize(self):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.executescript(SCHEMA)
            await conn.commit()

    async def execute(self, query, params=()):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute(query, params)
            await conn.commit()
            return None

    async def fetch_all(self, query, params=()):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def fetch_one(self, query, params=()):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            row = await cursor.fetchone()
            return dict(row) if row else None

if __name__ == "__main__":
    db = DatabaseManager()
    asyncio.run(db.initialize())
    print(f"Database initialized at {os.path.abspath(db.db_path)}")
