"""Per-user data access and Gmail isolation (2-person pilot scale).

Ledger: shared Supabase Postgres, rows scoped by user_id.
Gmail: still per-user OAuth token files under data/users/{user_id}/.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from auth.users import UserConfig, BASE_DIR
from db.manager import DatabaseManager, close_pool, ensure_schema, init_pool
from ingestion.gmail_client import GmailClient
from ingestion.sync import SyncService

# Cache tenant facades (lightweight; share the global pool)
_db_cache: dict[str, DatabaseManager] = {}


def ensure_user_data_dir(user: UserConfig) -> Path:
    """Local dir for Gmail tokens / CSV exports (not the ledger)."""
    path = user.data_dir()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Fallback for read-only environments (e.g. serverless / fastmcp.app)
        # We only strictly NEED this for CSV exports and legacy token storage.
        # If tokens are in Postgres, we can keep going.
        pass
    return path


def migrate_legacy_gmail_token_if_needed(user: UserConfig) -> None:
    legacy = BASE_DIR / "token.json"
    target = user.resolved_gmail_token_path()
    if target.exists() or not legacy.exists():
        return
    default_id = os.environ.get("DEFAULT_USER_ID") or user.user_id
    if user.user_id != default_id and user.user_id != "local":
        if os.environ.get("MIGRATE_LEGACY_GMAIL_TO") != user.user_id:
            return
    ensure_user_data_dir(user)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, target)
    except OSError:
        # Fallback for read-only filesystems
        pass


async def init_database() -> None:
    """Open the shared pool and ensure schema (call once at process start)."""
    await init_pool()
    await ensure_schema()


async def shutdown_database() -> None:
    clear_cache()
    await close_pool()


def get_db(user: UserConfig) -> DatabaseManager:
    """Return a user-scoped DatabaseManager over the shared Postgres pool."""
    if user.user_id in _db_cache:
        return _db_cache[user.user_id]
    ensure_user_data_dir(user)
    db = DatabaseManager(user.user_id)
    _db_cache[user.user_id] = db
    return db


async def get_gmail_client(user: UserConfig) -> GmailClient:
    db = get_db(user)
    
    # Check if the DB has credentials for this user
    row = await db.fetch_one("SELECT gmail_token_json FROM user_credentials WHERE user_id = $1", (user.user_id,))
    
    token_json_override = None
    if row and row["gmail_token_json"]:
        token_json_override = row["gmail_token_json"]
    else:
        # Fallback to env var or legacy logic if DB is empty
        migrate_legacy_gmail_token_if_needed(user)
        ensure_user_data_dir(user)
        token_env_name = user.gmail_token_env
        if token_env_name:
            token_json_override = os.environ.get(token_env_name)
            
    async def _on_token_refresh(new_token_json: str) -> None:
        """Save the newly refreshed token back to Postgres."""
        await db.execute(
            """
            INSERT INTO user_credentials (user_id, gmail_token_json, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET gmail_token_json = EXCLUDED.gmail_token_json,
                updated_at = NOW()
            """,
            (user.user_id, new_token_json)
        )

    return GmailClient(
        credentials_path=str(BASE_DIR / "credentials.json"),
        token_path=str(user.resolved_gmail_token_path()),
        token_json_override=token_json_override,
        on_token_refresh=_on_token_refresh,
    )


async def get_sync_service(user: UserConfig) -> SyncService:
    db = get_db(user)
    gmail = await get_gmail_client(user)
    return SyncService(db, gmail=gmail)


def clear_cache() -> None:
    _db_cache.clear()
