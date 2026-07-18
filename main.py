"""
Expense Tracker MCP Server — multi-user ready (household pilot).

Auth:
  AUTH_MODE=none   → local stdio; uses DEFAULT_USER_ID
  AUTH_MODE=static → bearer tokens from users.json; per-user data isolation

Data:
  Supabase Postgres (DATABASE_URL) — rows scoped by user_id
  data/users/{user_id}/token.json  (Gmail OAuth, local files)
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan as mcp_lifespan

from auth.identity import build_auth_provider, get_current_user, get_registry
from auth.users import UserConfig
from db.manager import SIGNED_AMOUNT_SQL, TXN_SELECT_COLS
from db.tenant import get_db, get_sync_service, init_database, shutdown_database
from ingestion.amount_utils import coerce_direction, normalize_amount

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UPDATABLE_TXN_FIELDS = frozenset(
    {
        "txn_date",
        "amount",
        "direction",
        "currency",
        "merchant_raw",
        "merchant_normalized",
        "category",
        "subcategory",
        "account",
        "instrument_last4",
        "payment_method",
        "notes",
        "is_recurring",
    }
)

# Prefer non-empty / more informative values when merging duplicates
_MERGE_PREFER_NONEMPTY = (
    "txn_ref",
    "source_email_id",
    "merchant_raw",
    "merchant_normalized",
    "instrument_last4",
    "payment_method",
    "category",
    "subcategory",
    "account",
    "notes",
    "currency",
)


@mcp_lifespan
async def _startup(server):
    """Open shared Postgres pool + ensure schema before any tool runs."""
    await init_database()
    yield {}
    await shutdown_database()


auth = build_auth_provider()
mcp = FastMCP("ExpenseTracker", auth=auth, lifespan=_startup)


def _user() -> UserConfig:
    return get_current_user()


def _db():
    return get_db(_user())


def _prefer_text(primary, other) -> str | None:
    """Keep the richer non-empty string (longer / non-placeholder)."""
    p = (primary or "").strip() if primary is not None else ""
    o = (other or "").strip() if other is not None else ""
    if not p:
        return o or None
    if not o:
        return p or None
    if p.lower() in ("unknown", "uncategorized") and o.lower() not in (
        "unknown",
        "uncategorized",
    ):
        return o
    if o.lower() in ("unknown", "uncategorized"):
        return p
    return o if len(o) > len(p) else p


def _placeholders(n: int, start: int = 1) -> str:
    """Build $start, $start+1, ... for n params."""
    return ", ".join(f"${i}" for i in range(start, start + n))


def _rows_affected(status: str) -> int:
    """Parse asyncpg status like 'UPDATE 1' / 'DELETE 0' / 'INSERT 0 1'."""
    parts = (status or "").split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Remote User OAuth Onboarding
# ---------------------------------------------------------------------------
from fastapi import Request
from fastapi.responses import RedirectResponse, HTMLResponse
from google_auth_oauthlib.flow import Flow
import asyncio

OAUTH_STATES = {}
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def _get_oauth_flow(request: Request) -> Flow:
    base_url = str(request.base_url).rstrip("/")
    # Force HTTPS if behind a proxy like fastmcp.cloud
    if "fastmcp.app" in base_url or request.headers.get("x-forwarded-proto") == "https":
        base_url = base_url.replace("http://", "https://")
    redirect_uri = f"{base_url}/auth/callback"
    redirect_uri = os.environ.get("OAUTH_REDIRECT_URI", redirect_uri)
    
    return Flow.from_client_secrets_file(
        str(os.path.join(BASE_DIR, "credentials.json")),
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )

@mcp.custom_route("/auth/login", methods=["GET"])
async def oauth_login(request: Request):
    """Initiates the Google OAuth flow for a specific user_id."""
    user_id = request.query_params.get("user_id")
    if not user_id:
        return HTMLResponse("Missing user_id parameter (e.g., ?user_id=bobbie)", status_code=400)
        
    try:
        get_registry().get(user_id)
    except KeyError:
        return HTMLResponse(f"User '{user_id}' not found in registry.", status_code=403)
    
    try:
        flow = _get_oauth_flow(request)
        auth_url, state = flow.authorization_url(prompt='consent', access_type='offline')
        OAUTH_STATES[state] = user_id
        return RedirectResponse(auth_url)
    except Exception as e:
        return HTMLResponse(f"Failed to start OAuth flow: {e}", status_code=500)

@mcp.custom_route("/auth/callback", methods=["GET"])
async def oauth_callback(request: Request):
    """Handles the redirect from Google and saves the token to Supabase."""
    state = request.query_params.get("state")
    code = request.query_params.get("code")
    
    if not state or state not in OAUTH_STATES:
        return HTMLResponse("Invalid or expired OAuth state. Please try logging in again.", status_code=400)
        
    user_id = OAUTH_STATES.pop(state)
    user = get_registry().get(user_id)
    
    flow = _get_oauth_flow(request)
    
    # Ensure URL is HTTPS if running on cloud
    auth_response_url = str(request.url)
    if "fastmcp.app" in auth_response_url or request.headers.get("x-forwarded-proto") == "https":
        auth_response_url = auth_response_url.replace("http://", "https://")
        
    try:
        await asyncio.to_thread(flow.fetch_token, authorization_response=auth_response_url)
    except Exception as e:
        return HTMLResponse(f"OAuth Fetch Token Error: {e}", status_code=400)
        
    token_json = flow.credentials.to_json()
    
    db = get_db(user)
    await db.ensure_initialized()
    await db.execute(
        """
        INSERT INTO user_credentials (user_id, gmail_token_json, updated_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (user_id) DO UPDATE
        SET gmail_token_json = EXCLUDED.gmail_token_json,
            updated_at = NOW()
        """,
        (user_id, token_json)
    )
    
    return HTMLResponse(
        f"<h1>Success!</h1>"
        f"<p>Gmail account connected successfully for <b>{user_id}</b>.</p>"
        f"<p>The access token has been securely stored in the database. You can close this window and use the Expense Tracker MCP.</p>"
    )

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def whoami() -> dict:
    """Return the authenticated user for this session."""
    user = _user()
    return {
        "user_id": user.user_id,
        "display_name": user.display_name,
        "backend": "postgres",
        "data_dir": str(user.data_dir()),
    }


@mcp.tool()
async def sync_emails(days: int = 7) -> str:
    """
    Sync bank transaction emails from Gmail for the last N days (HDFC + Axis).
    Uses this user's Gmail token and ledger only.
    """
    user = _user()
    db = get_db(user)
    await db.ensure_initialized()
    sync_service = await get_sync_service(user)
    synced, errors = await sync_service.sync_emails(days=days)
    return (
        f"[{user.user_id}] Sync complete. Added {synced} new transactions. "
        f"Encountered {errors} errors."
    )


@mcp.tool()
async def add_transaction(
    txn_date: str,
    amount: float,
    merchant_raw: str,
    category: str = "Uncategorized",
    payment_method: str = "Manual",
    notes: str = None,
    direction: str = "debit",
) -> str:
    """
    Manually add a new transaction.
    txn_date format: 'YYYY-MM-DD HH:MM:SS'
    amount: absolute value (>= 0). direction: 'debit' (spend) or 'credit' (refund).
    """
    try:
        direction = coerce_direction(direction)
    except ValueError as e:
        return str(e)
    amount = normalize_amount(amount)
    user = _user()
    await get_db(user).execute(
        """
        INSERT INTO transactions
        (user_id, txn_date, amount, direction, merchant_raw, category, payment_method, notes)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        (
            user.user_id,
            txn_date,
            amount,
            direction,
            merchant_raw,
            category,
            payment_method,
            notes,
        ),
    )
    return f"Transaction added successfully ({direction} Rs. {amount:.2f})."


@mcp.tool()
async def update_transaction(transaction_id: int, updates: dict) -> str:
    """
    Update specific fields of a transaction.
    updates: dictionary of field names and new values (allowlisted columns only).
    """
    if not updates:
        return "No updates provided."

    bad = [k for k in updates if k not in UPDATABLE_TXN_FIELDS]
    if bad:
        return f"Invalid fields: {bad}. Allowed: {sorted(UPDATABLE_TXN_FIELDS)}"

    cleaned = dict(updates)
    if "direction" in cleaned:
        try:
            cleaned["direction"] = coerce_direction(cleaned["direction"])
        except ValueError as e:
            return str(e)
    if "amount" in cleaned:
        cleaned["amount"] = normalize_amount(cleaned["amount"])

    user = _user()
    cols = list(cleaned.keys())
    set_clause = ", ".join(f"{c} = ${i}" for i, c in enumerate(cols, start=1))
    values = [cleaned[c] for c in cols]
    id_idx = len(values) + 1
    uid_idx = len(values) + 2
    values.extend([transaction_id, user.user_id])

    status = await get_db(user).execute(
        f"UPDATE transactions SET {set_clause} "
        f"WHERE id = ${id_idx} AND user_id = ${uid_idx}",
        tuple(values),
    )
    if _rows_affected(status) == 0:
        return f"Transaction {transaction_id} not found for this user."
    return f"Transaction {transaction_id} updated successfully."


@mcp.tool()
async def delete_transaction(transaction_id: int) -> str:
    """Delete a transaction by ID (scoped to the current user's ledger)."""
    user = _user()
    status = await get_db(user).execute(
        "DELETE FROM transactions WHERE id = $1 AND user_id = $2",
        (transaction_id, user.user_id),
    )
    if _rows_affected(status) == 0:
        return f"Transaction {transaction_id} not found for this user."
    return f"Transaction {transaction_id} deleted successfully."


@mcp.tool()
async def merge_duplicates(transaction_ids: list[int]) -> str:
    """
    Merge duplicate transactions into the first ID.
    Coalesces non-empty fields (txn_ref, merchant, etc.) onto the primary row,
    then deletes the other IDs.
    """
    if len(transaction_ids) < 2:
        return "At least two transaction IDs are required to merge."

    # Dedupe while preserving order
    seen = set()
    ordered = []
    for tid in transaction_ids:
        if tid not in seen:
            seen.add(tid)
            ordered.append(tid)

    if len(ordered) < 2:
        return "At least two distinct transaction IDs are required to merge."

    primary_id = ordered[0]
    duplicates = ordered[1:]
    user = _user()
    db = get_db(user)
    uid = user.user_id

    primary = await db.fetch_one(
        f"SELECT {TXN_SELECT_COLS} FROM transactions WHERE id = $1 AND user_id = $2",
        (primary_id, uid),
    )
    if not primary:
        return f"Primary transaction {primary_id} not found."

    others = []
    for did in duplicates:
        row = await db.fetch_one(
            f"SELECT {TXN_SELECT_COLS} FROM transactions WHERE id = $1 AND user_id = $2",
            (did, uid),
        )
        if row:
            others.append(row)
    if not others:
        return "No duplicate rows found to merge."

    merged = dict(primary)
    for other in others:
        for field in _MERGE_PREFER_NONEMPTY:
            merged[field] = _prefer_text(merged.get(field), other.get(field))
        if other.get("txn_date") and (
            not merged.get("txn_date") or other["txn_date"] < merged["txn_date"]
        ):
            merged["txn_date"] = other["txn_date"]
        if merged.get("direction") != "debit" and other.get("direction") == "debit":
            merged["direction"] = "debit"
        try:
            ma = float(merged.get("amount") or 0)
            oa = float(other.get("amount") or 0)
            if oa > ma:
                merged["amount"] = oa
        except (TypeError, ValueError):
            pass

    update_fields = {
        k: merged[k]
        for k in (
            "txn_date",
            "amount",
            "direction",
            "currency",
            "merchant_raw",
            "merchant_normalized",
            "category",
            "subcategory",
            "account",
            "instrument_last4",
            "payment_method",
            "txn_ref",
            "notes",
            "is_recurring",
        )
        if k in merged
    }
    # Clear unique keys on duplicates first to avoid constraint conflicts
    ph = _placeholders(len(duplicates), start=1)
    uid_ph = f"${len(duplicates) + 1}"
    await db.execute(
        f"UPDATE transactions SET txn_ref = NULL, source_email_id = NULL "
        f"WHERE id IN ({ph}) AND user_id = {uid_ph}",
        tuple(list(duplicates) + [uid]),
    )

    cols = list(update_fields.keys())
    set_clause = ", ".join(f"{c} = ${i}" for i, c in enumerate(cols, start=1))
    id_idx = len(cols) + 1
    uid_idx = len(cols) + 2
    await db.execute(
        f"UPDATE transactions SET {set_clause} "
        f"WHERE id = ${id_idx} AND user_id = ${uid_idx}",
        tuple(list(update_fields.values()) + [primary_id, uid]),
    )
    await db.execute(
        f"DELETE FROM transactions WHERE id IN ({ph}) AND user_id = {uid_ph}",
        tuple(list(duplicates) + [uid]),
    )

    return (
        f"Merged {len(others)} duplicates into transaction {primary_id} "
        f"(coalesced merchant/ref/notes where richer on duplicates)."
    )


@mcp.tool()
async def set_budget(
    amount_limit: float,
    category: str = None,
    merchant: str = None,
    period: str = "monthly",
) -> str:
    """
    Set or update a spending budget for a category OR a merchant.
    Provide exactly one of: category, merchant.
    period: 'monthly' or 'yearly'
    """
    if bool(category) == bool(merchant):
        return "Provide exactly one of 'category' or 'merchant'."
    if period not in ("monthly", "yearly"):
        return "period must be 'monthly' or 'yearly'."

    scope_type = "category" if category else "merchant"
    scope_value = (category or merchant).strip()
    if not scope_value:
        return "Budget target cannot be empty."

    user = _user()
    await get_db(user).execute(
        """
        INSERT INTO budgets (user_id, scope_type, scope_value, period, amount_limit)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (user_id, scope_type, scope_value, period)
        DO UPDATE SET amount_limit = EXCLUDED.amount_limit
        """,
        (user.user_id, scope_type, scope_value, period, amount_limit),
    )
    return (
        f"Budget set: {scope_type} '{scope_value}' → Rs. {amount_limit:.2f} ({period}). "
        f"Use budget_status or budget_breaches to monitor it."
    )


async def _budget_rows(period: str = "monthly") -> list:
    """Spend vs limit using net signed amounts (credits reduce spend)."""
    user = _user()
    db = get_db(user)
    await db.ensure_initialized()
    uid = user.user_id
    # Postgres period filter: current calendar month or year
    period_sql = (
        "to_char(txn_date AT TIME ZONE 'UTC', 'YYYY-MM') = "
        "to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM')"
        if period == "monthly"
        else "to_char(txn_date AT TIME ZONE 'UTC', 'YYYY') = "
        "to_char(NOW() AT TIME ZONE 'UTC', 'YYYY')"
    )
    budgets = await db.fetch_all(
        "SELECT scope_type, scope_value, period, amount_limit "
        "FROM budgets WHERE period = $1 AND user_id = $2",
        (period, uid),
    )
    rows = []
    for b in budgets:
        if b["scope_type"] == "category":
            spend_row = await db.fetch_one(
                f"""
                SELECT COALESCE(SUM({SIGNED_AMOUNT_SQL}), 0) AS current_spend
                FROM transactions
                WHERE user_id = $1
                  AND lower(category) = lower($2)
                  AND {period_sql}
                """,
                (uid, b["scope_value"]),
            )
        else:
            spend_row = await db.fetch_one(
                f"""
                SELECT COALESCE(SUM({SIGNED_AMOUNT_SQL}), 0) AS current_spend
                FROM transactions
                WHERE user_id = $1
                  AND lower(merchant_raw) LIKE '%' || lower($2) || '%'
                  AND {period_sql}
                """,
                (uid, b["scope_value"]),
            )
        current = float(spend_row["current_spend"] if spend_row else 0)
        limit = float(b["amount_limit"])
        remaining = limit - current
        rows.append(
            {
                "scope_type": b["scope_type"],
                "scope_value": b["scope_value"],
                "period": b["period"],
                "amount_limit": limit,
                "current_spend": current,
                "remaining": remaining,
                "breached": current > limit,
                "utilization_pct": round((current / limit) * 100, 1) if limit else None,
            }
        )
    return rows


@mcp.tool()
async def budget_status(period: str = "monthly") -> list:
    """Check spending vs budgets (net of refunds/credits)."""
    if period not in ("monthly", "yearly"):
        return [{"error": "period must be 'monthly' or 'yearly'."}]
    return await _budget_rows(period)


@mcp.tool()
async def budget_breaches(period: str = "monthly") -> list:
    """Return only budgets that are currently breached (net spend > limit)."""
    if period not in ("monthly", "yearly"):
        return [{"error": "period must be 'monthly' or 'yearly'."}]
    return [row for row in await _budget_rows(period) if row["breached"]]


@mcp.tool()
async def add_rule(pattern: str, category: str, field: str = "merchant_raw") -> str:
    """Add a categorization rule (pattern match on field → category)."""
    user = _user()
    await get_db(user).execute(
        """
        INSERT INTO rules (user_id, pattern, field, category)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id, pattern, field)
        DO UPDATE SET category = EXCLUDED.category
        """,
        (user.user_id, pattern, field, category),
    )
    return (
        f"Rule added: Transactions with {field} matching '{pattern}' "
        f"will be categorized as '{category}'."
    )


@mcp.tool()
async def categorize_pending() -> str:
    """Apply rules to all 'Uncategorized' transactions for the current user."""
    user = _user()
    db = get_db(user)
    uid = user.user_id
    rules = await db.fetch_all(
        "SELECT * FROM rules WHERE user_id = $1", (uid,)
    )
    if not rules:
        return "No rules found. Please add some rules first using add_rule."

    uncategorized = await db.fetch_all(
        "SELECT id, merchant_raw, notes FROM transactions "
        "WHERE user_id = $1 AND category = 'Uncategorized'",
        (uid,),
    )
    if not uncategorized:
        return "No uncategorized transactions found."

    updated_count = 0
    for txn in uncategorized:
        for rule in rules:
            field_to_check = txn.get(rule["field"])
            if field_to_check and rule["pattern"].lower() in field_to_check.lower():
                await db.execute(
                    "UPDATE transactions SET category = $1 "
                    "WHERE id = $2 AND user_id = $3",
                    (rule["category"], txn["id"], uid),
                )
                updated_count += 1
                break

    return f"Categorization complete. Updated {updated_count} transactions based on rules."


@mcp.tool()
async def query_transactions(filter_text: str = None, limit: int = 10) -> list:
    """Search transactions by merchant, category, or notes (Postgres full-text)."""
    user = _user()
    db = get_db(user)
    uid = user.user_id
    if filter_text:
        query = f"""
            SELECT {TXN_SELECT_COLS}
            FROM transactions
            WHERE user_id = $1
              AND search_vector @@ plainto_tsquery('english', $2)
            ORDER BY txn_date DESC
            LIMIT $3
        """
        return await db.fetch_all(query, (uid, filter_text, limit))
    return await db.fetch_all(
        f"SELECT {TXN_SELECT_COLS} FROM transactions "
        "WHERE user_id = $1 ORDER BY txn_date DESC LIMIT $2",
        (uid, limit),
    )


@mcp.tool()
async def spending_summary(period: str = "month") -> list:
    """
    Net spend by merchant for the current period (credits reduce totals).
    period: 'month' or 'year'
    """
    user = _user()
    period_sql = (
        "to_char(txn_date AT TIME ZONE 'UTC', 'YYYY-MM') = "
        "to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM')"
        if period == "month"
        else "to_char(txn_date AT TIME ZONE 'UTC', 'YYYY') = "
        "to_char(NOW() AT TIME ZONE 'UTC', 'YYYY')"
    )
    query = f"""
        SELECT merchant_raw,
               SUM({SIGNED_AMOUNT_SQL}) as total_spend,
               COUNT(*) as txn_count
        FROM transactions
        WHERE user_id = $1 AND {period_sql}
        GROUP BY merchant_raw
        ORDER BY total_spend DESC
    """
    return await get_db(user).fetch_all(query, (user.user_id,))


@mcp.tool()
async def top_merchants(n: int = 5, period: str = "all") -> list:
    """Top N merchants by net spend. period: 'month', 'year', or 'all'."""
    user = _user()
    db = get_db(user)
    uid = user.user_id
    if period == "all":
        query = f"""
            SELECT merchant_raw,
                   SUM({SIGNED_AMOUNT_SQL}) as total_spend,
                   COUNT(*) as txn_count
            FROM transactions
            WHERE user_id = $1
            GROUP BY merchant_raw
            ORDER BY total_spend DESC
            LIMIT $2
        """
        return await db.fetch_all(query, (uid, n))
    period_sql = (
        "to_char(txn_date AT TIME ZONE 'UTC', 'YYYY-MM') = "
        "to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM')"
        if period == "month"
        else "to_char(txn_date AT TIME ZONE 'UTC', 'YYYY') = "
        "to_char(NOW() AT TIME ZONE 'UTC', 'YYYY')"
    )
    query = f"""
        SELECT merchant_raw,
               SUM({SIGNED_AMOUNT_SQL}) as total_spend,
               COUNT(*) as txn_count
        FROM transactions
        WHERE user_id = $1 AND {period_sql}
        GROUP BY merchant_raw
        ORDER BY total_spend DESC
        LIMIT $2
    """
    return await db.fetch_all(query, (uid, n))


@mcp.tool()
async def anomaly_report(days_baseline: int = 90, z_threshold: float = 2.0) -> list:
    """
    Detect high-spend (debit) outliers in the last 7 days vs category baseline.
    Uses absolute amount for debit rows only.
    """
    user = _user()
    stats_query = f"""
        WITH stats AS (
            SELECT
                category,
                AVG(amount) as avg_amt,
                SQRT(AVG(amount * amount) - (AVG(amount) * AVG(amount))) as std_amt
            FROM transactions
            WHERE user_id = $1
              AND txn_date >= NOW() - make_interval(days => $2)
              AND COALESCE(direction, 'debit') = 'debit'
            GROUP BY category
        )
        SELECT
            t.id, t.txn_date, t.amount, t.direction, t.merchant_raw, t.category,
            s.avg_amt, s.std_amt,
            (t.amount - s.avg_amt) / NULLIF(s.std_amt, 0) as z_score
        FROM transactions t
        JOIN stats s ON t.category = s.category
        WHERE t.user_id = $1
          AND t.txn_date >= NOW() - INTERVAL '7 days'
          AND COALESCE(t.direction, 'debit') = 'debit'
          AND (t.amount - s.avg_amt) / NULLIF(s.std_amt, 0) > $3
        ORDER BY z_score DESC
    """
    return await get_db(user).fetch_all(
        stats_query, (user.user_id, days_baseline, z_threshold)
    )


@mcp.tool()
async def export_data(period: str = "all") -> str:
    """Export the current user's transactions to CSV under their data dir."""
    import csv
    from datetime import datetime

    user = _user()
    db = get_db(user)
    uid = user.user_id

    cols = (
        "txn_date, amount, direction, merchant_raw, category, payment_method, "
        "instrument_last4, notes"
    )
    if period == "all":
        query = (
            f"SELECT {cols} FROM transactions "
            "WHERE user_id = $1 ORDER BY txn_date DESC"
        )
        params: tuple = (uid,)
    else:
        period_sql = (
            "to_char(txn_date AT TIME ZONE 'UTC', 'YYYY-MM') = "
            "to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM')"
            if period == "month"
            else "to_char(txn_date AT TIME ZONE 'UTC', 'YYYY') = "
            "to_char(NOW() AT TIME ZONE 'UTC', 'YYYY')"
        )
        query = (
            f"SELECT {cols} FROM transactions "
            f"WHERE user_id = $1 AND {period_sql} ORDER BY txn_date DESC"
        )
        params = (uid,)

    rows = await db.fetch_all(query, params)
    if not rows:
        return "No transactions to export."

    # Serialize dates for CSV
    for row in rows:
        if row.get("txn_date") is not None:
            row["txn_date"] = str(row["txn_date"])

    export_dir = user.data_dir()
    export_dir.mkdir(parents=True, exist_ok=True)
    filename = f"expenses_export_{period}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    filepath = os.path.join(export_dir, filename)

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    return f"Successfully exported {len(rows)} transactions to {filepath}"


@mcp.resource("resource://current_month_total")
async def get_current_month_total() -> str:
    """Net spend for the current month (credits reduce the total)."""
    user = _user()
    result = await get_db(user).fetch_one(
        f"""
        SELECT COALESCE(SUM({SIGNED_AMOUNT_SQL}), 0) as total
        FROM transactions
        WHERE user_id = $1
          AND to_char(txn_date AT TIME ZONE 'UTC', 'YYYY-MM') =
              to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM')
        """,
        (user.user_id,),
    )
    total = result["total"] if result and result["total"] is not None else 0
    return f"[{user.user_id}] Net spend this month: Rs. {float(total):.2f}"


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http", "sse"):
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8000"))
        mcp.run(
            transport=transport if transport != "http" else "http",
            host=host,
            port=port,
        )
    else:
        mcp.run()
