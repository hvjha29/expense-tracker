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
    scope_type TEXT NOT NULL CHECK(scope_type IN ('category', 'merchant')),
    scope_value TEXT NOT NULL,
    period TEXT NOT NULL, -- 'monthly', 'yearly'
    amount_limit REAL NOT NULL,
    UNIQUE(scope_type, scope_value, period)
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
        self._initialized = False
        self._init_lock: asyncio.Lock | None = None

    def _lock(self) -> asyncio.Lock:
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        return self._init_lock

    async def initialize(self):
        """Create the DB file and schema if missing / incomplete."""
        async with self._lock():
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)) or ".", exist_ok=True)
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute("PRAGMA journal_mode=WAL;")
                cursor = await conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingestion_log'"
                )
                if await cursor.fetchone() is None:
                    await conn.executescript(SCHEMA)
                    await conn.commit()
                else:
                    await self._migrate_budgets(conn)
            self._initialized = True

    async def _migrate_budgets(self, conn):
        """Upgrade legacy category-only budgets to scope_type/scope_value."""
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='budgets'"
        )
        if await cursor.fetchone() is None:
            await conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS budgets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_type TEXT NOT NULL CHECK(scope_type IN ('category', 'merchant')),
                    scope_value TEXT NOT NULL,
                    period TEXT NOT NULL,
                    amount_limit REAL NOT NULL,
                    UNIQUE(scope_type, scope_value, period)
                );
                """
            )
            await conn.commit()
            return

        cursor = await conn.execute("PRAGMA table_info(budgets)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "scope_type" in cols:
            return

        await conn.executescript(
            """
            CREATE TABLE budgets_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_type TEXT NOT NULL CHECK(scope_type IN ('category', 'merchant')),
                scope_value TEXT NOT NULL,
                period TEXT NOT NULL,
                amount_limit REAL NOT NULL,
                UNIQUE(scope_type, scope_value, period)
            );
            INSERT INTO budgets_v2 (scope_type, scope_value, period, amount_limit)
            SELECT 'category', category, period, amount_limit FROM budgets;
            DROP TABLE budgets;
            ALTER TABLE budgets_v2 RENAME TO budgets;
            """
        )
        await conn.commit()

    async def ensure_initialized(self):
        if not self._initialized or not os.path.exists(self.db_path):
            await self.initialize()
            return
        # Recover if the file exists but schema was wiped / never applied
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingestion_log'"
            )
            if await cursor.fetchone() is None:
                self._initialized = False
        if not self._initialized:
            await self.initialize()

    async def execute(self, query, params=()):
        await self.ensure_initialized()
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute(query, params)
            await conn.commit()
            return None

    async def fetch_all(self, query, params=()):
        await self.ensure_initialized()
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def fetch_one(self, query, params=()):
        await self.ensure_initialized()
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
