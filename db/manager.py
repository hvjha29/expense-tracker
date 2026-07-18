"""Postgres (Supabase) database layer via asyncpg.

All tenant data lives in one database; every row is scoped by user_id.
"""

from __future__ import annotations

import asyncio
import os
import ssl
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Optional, Sequence
from uuid import UUID

import asyncpg
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# amount is always non-negative; direction is debit (spend) or credit (refund/inflow).
# Net spend = SUM(CASE WHEN direction='credit' THEN -amount ELSE amount END)
SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS transactions (
  id                BIGSERIAL PRIMARY KEY,
  user_id           TEXT NOT NULL,
  txn_date          TIMESTAMPTZ NOT NULL,
  amount            DOUBLE PRECISION NOT NULL CHECK (amount >= 0),
  direction         TEXT NOT NULL DEFAULT 'debit'
                      CHECK (direction IN ('debit', 'credit')),
  currency          TEXT DEFAULT 'INR',
  merchant_raw      TEXT NOT NULL,
  merchant_normalized TEXT,
  category          TEXT DEFAULT 'Uncategorized',
  subcategory       TEXT,
  account           TEXT,
  instrument_last4  TEXT,
  payment_method    TEXT,
  source_email_id   TEXT,
  txn_ref           TEXT,
  notes             TEXT,
  is_recurring      BOOLEAN NOT NULL DEFAULT FALSE,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  search_vector     tsvector,
  UNIQUE (user_id, source_email_id),
  UNIQUE (user_id, txn_ref)
);

CREATE INDEX IF NOT EXISTS idx_transactions_user_date
  ON transactions (user_id, txn_date DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_user_category
  ON transactions (user_id, category);
CREATE INDEX IF NOT EXISTS idx_transactions_search
  ON transactions USING GIN (search_vector);

CREATE TABLE IF NOT EXISTS categories (
  id               BIGSERIAL PRIMARY KEY,
  user_id          TEXT NOT NULL,
  name             TEXT NOT NULL,
  parent_category  TEXT,
  UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS budgets (
  id            BIGSERIAL PRIMARY KEY,
  user_id       TEXT NOT NULL,
  scope_type    TEXT NOT NULL CHECK (scope_type IN ('category', 'merchant')),
  scope_value   TEXT NOT NULL,
  period        TEXT NOT NULL,
  amount_limit  DOUBLE PRECISION NOT NULL,
  UNIQUE (user_id, scope_type, scope_value, period)
);

CREATE TABLE IF NOT EXISTS rules (
  id        BIGSERIAL PRIMARY KEY,
  user_id   TEXT NOT NULL,
  pattern   TEXT NOT NULL,
  field     TEXT NOT NULL,
  category  TEXT NOT NULL,
  UNIQUE (user_id, pattern, field)
);

CREATE TABLE IF NOT EXISTS ingestion_log (
  user_id        TEXT NOT NULL,
  email_id       TEXT NOT NULL,
  status         TEXT,
  processed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  error_message  TEXT,
  PRIMARY KEY (user_id, email_id)
);

CREATE TABLE IF NOT EXISTS user_credentials (
  user_id           TEXT PRIMARY KEY,
  gmail_token_json  TEXT,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Enable Row Level Security (RLS) on all tables
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE budgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingestion_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_credentials ENABLE ROW LEVEL SECURITY;

-- Create policies to restrict access based on the 'app.current_user_id' setting.
-- If the setting is not set, it defaults to an empty string, denying access.
DO $$
BEGIN
  -- transactions
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation_policy' AND tablename = 'transactions') THEN
    CREATE POLICY tenant_isolation_policy ON transactions
      USING (user_id = current_setting('app.current_user_id', true));
  END IF;

  -- categories
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation_policy' AND tablename = 'categories') THEN
    CREATE POLICY tenant_isolation_policy ON categories
      USING (user_id = current_setting('app.current_user_id', true));
  END IF;

  -- budgets
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation_policy' AND tablename = 'budgets') THEN
    CREATE POLICY tenant_isolation_policy ON budgets
      USING (user_id = current_setting('app.current_user_id', true));
  END IF;

  -- rules
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation_policy' AND tablename = 'rules') THEN
    CREATE POLICY tenant_isolation_policy ON rules
      USING (user_id = current_setting('app.current_user_id', true));
  END IF;

  -- ingestion_log
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation_policy' AND tablename = 'ingestion_log') THEN
    CREATE POLICY tenant_isolation_policy ON ingestion_log
      USING (user_id = current_setting('app.current_user_id', true));
  END IF;

  -- user_credentials
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation_policy' AND tablename = 'user_credentials') THEN
    CREATE POLICY tenant_isolation_policy ON user_credentials
      USING (user_id = current_setting('app.current_user_id', true));
  END IF;
END $$;

CREATE OR REPLACE FUNCTION transactions_search_vector_update()
RETURNS trigger AS $$
BEGIN
  NEW.search_vector :=
    to_tsvector(
      'english',
      coalesce(NEW.merchant_raw, '') || ' ' ||
      coalesce(NEW.merchant_normalized, '') || ' ' ||
      coalesce(NEW.category, '') || ' ' ||
      coalesce(NEW.notes, '')
    );
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

# Trigger DDL separate so we can DROP IF EXISTS cleanly
TRIGGER_SQL = """
DROP TRIGGER IF EXISTS transactions_search_vector_trigger ON transactions;
CREATE TRIGGER transactions_search_vector_trigger
  BEFORE INSERT OR UPDATE OF merchant_raw, merchant_normalized, category, notes
  ON transactions
  FOR EACH ROW
  EXECUTE FUNCTION transactions_search_vector_update();
"""

# Net spend expression (debits positive, credits negative)
SIGNED_AMOUNT_SQL = (
    "CASE WHEN COALESCE(direction, 'debit') = 'credit' "
    "THEN -ABS(amount) ELSE ABS(amount) END"
)

# Public transaction columns (exclude internal search_vector)
TXN_SELECT_COLS = (
    "id, user_id, txn_date, amount, direction, currency, merchant_raw, "
    "merchant_normalized, category, subcategory, account, instrument_last4, "
    "payment_method, source_email_id, txn_ref, notes, is_recurring, created_at"
)

_pool: Optional[asyncpg.Pool] = None
_pool_lock: Optional[asyncio.Lock] = None
_schema_ready = False

_CONNECT_HINT = (
    "Could not connect to Postgres via DATABASE_URL.\n"
    "Supabase tips:\n"
    "  1. Prefer the Session pooler URI (Dashboard → Connect / Database),\n"
    "     not the direct db.<ref>.supabase.co host (often IPv6-only).\n"
    "  2. Pooler username is usually postgres.<project-ref>, not just postgres.\n"
    "  3. Ensure the project is Active (not paused) on the free tier.\n"
    "  4. Copy the URI again after resetting the database password."
)


def _get_pool_lock() -> asyncio.Lock:
    global _pool_lock
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    return _pool_lock


def get_database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Add your Supabase Postgres connection "
            "string to .env (Project Settings → Database → Connection string)."
        )
    return url


def convert_placeholders(sql: str) -> str:
    """Convert SQLite-style ? placeholders to asyncpg $1, $2, ..."""
    if "?" not in sql:
        return sql
    out: list[str] = []
    n = 0
    for ch in sql:
        if ch == "?":
            n += 1
            out.append(f"${n}")
        else:
            out.append(ch)
    return "".join(out)


def _serialize_value(value: Any) -> Any:
    """Make asyncpg values JSON-friendly for MCP tool responses."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


def _row_to_dict(row: Optional[asyncpg.Record]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {k: _serialize_value(v) for k, v in dict(row).items()}


def _ssl_candidates(dsn: str) -> list[Any]:
    """SSL options to try, in order (Supabase needs TLS)."""
    lower = dsn.lower()
    if "sslmode=disable" in lower:
        return [False]
    if (
        "supabase.co" in lower
        or "pooler.supabase" in lower
        or "sslmode=require" in lower
        or "sslmode=verify" in lower
    ):
        # Default context first; many local Python builds fail Supabase pooler
        # cert chain verification and need an unverified context.
        return [
            ssl.create_default_context(),
            ssl._create_unverified_context(),  # noqa: SLF001
        ]
    return [None]


async def init_pool(
    dsn: Optional[str] = None,
    *,
    min_size: int = 1,
    max_size: int = 10,
) -> asyncpg.Pool:
    """Create the shared connection pool (idempotent)."""
    global _pool
    async with _get_pool_lock():
        if _pool is not None:
            return _pool
        url = dsn or get_database_url()
        last_err: Optional[BaseException] = None
        for ssl_arg in _ssl_candidates(url):
            kwargs: dict[str, Any] = {
                "dsn": url,
                "min_size": min_size,
                "max_size": max_size,
                "command_timeout": 60,
                # Safe with Supabase transaction-mode pooler (PgBouncer)
                "statement_cache_size": 0,
                "timeout": 30,
            }
            if ssl_arg is not None:
                kwargs["ssl"] = ssl_arg
            try:
                _pool = await asyncpg.create_pool(**kwargs)
                return _pool
            except (TimeoutError, OSError, asyncio.TimeoutError, ssl.SSLError) as e:
                last_err = e
                continue
            except Exception as e:
                # Auth / tenant errors: don't keep retrying SSL variants uselessly
                # unless this was an SSL-related failure.
                last_err = e
                if "certificate" in str(e).lower() or "ssl" in str(e).lower():
                    continue
                break
        raise RuntimeError(f"{_CONNECT_HINT}\n\nLast error: {last_err!r}") from last_err


async def close_pool() -> None:
    global _pool, _schema_ready
    async with _get_pool_lock():
        if _pool is not None:
            await _pool.close()
            _pool = None
        _schema_ready = False


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        await init_pool()
    assert _pool is not None
    return _pool


async def ensure_schema() -> None:
    """Create tables/indexes/triggers if missing (idempotent)."""
    global _schema_ready
    if _schema_ready:
        return
    async with _get_pool_lock():
        if _schema_ready:
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
            try:
                await conn.execute(TRIGGER_SQL)
            except asyncpg.PostgresError:
                # Older PG wording uses PROCEDURE instead of FUNCTION
                await conn.execute(
                    TRIGGER_SQL.replace(
                        "EXECUTE FUNCTION", "EXECUTE PROCEDURE"
                    )
                )
        _schema_ready = True


class DatabaseManager:
    """Per-user facade over the shared Postgres pool.

    Every method automatically scopes work to ``user_id``. Callers still pass
    business params only; ``user_id`` is injected by helper SQL or by the
    methods that own full queries in the app layer.
    """

    def __init__(self, user_id: str):
        if not user_id or not str(user_id).strip():
            raise ValueError("user_id is required")
        self.user_id = str(user_id).strip()

    async def initialize(self) -> None:
        """Ensure pool + schema exist (shared; safe to call per user)."""
        await init_pool()
        await ensure_schema()

    async def ensure_initialized(self) -> None:
        await self.initialize()

    async def execute(self, query: str, params: Sequence[Any] = ()) -> str:
        """Run a write (INSERT/UPDATE/DELETE). Returns asyncpg status string."""
        await self.ensure_initialized()
        pool = await get_pool()
        sql = convert_placeholders(query)
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", self.user_id)
            return await conn.execute(sql, *params)

    async def fetch_all(
        self, query: str, params: Sequence[Any] = ()
    ) -> list[dict[str, Any]]:
        await self.ensure_initialized()
        pool = await get_pool()
        sql = convert_placeholders(query)
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", self.user_id)
            rows = await conn.fetch(sql, *params)
        return [_row_to_dict(r) for r in rows]  # type: ignore[misc]

    async def fetch_one(
        self, query: str, params: Sequence[Any] = ()
    ) -> Optional[dict[str, Any]]:
        await self.ensure_initialized()
        pool = await get_pool()
        sql = convert_placeholders(query)
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", self.user_id)
            row = await conn.fetchrow(sql, *params)
        return _row_to_dict(row)

    async def fetch_val(
        self, query: str, params: Sequence[Any] = ()
    ) -> Any:
        await self.ensure_initialized()
        pool = await get_pool()
        sql = convert_placeholders(query)
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", self.user_id)
            return await conn.fetchval(sql, *params)


if __name__ == "__main__":
    async def _main() -> None:
        await init_pool()
        await ensure_schema()
        print("Postgres schema ready (Supabase / DATABASE_URL).")
        await close_pool()

    asyncio.run(_main())
