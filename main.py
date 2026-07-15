from fastmcp import FastMCP
from db.manager import DatabaseManager
from ingestion.sync import SyncService
import datetime
import os
import asyncio

# Initialize MCP server
mcp = FastMCP("ExpenseTracker")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Initialize backend (schema is also ensured lazily on first DB use)
db = DatabaseManager(os.path.join(BASE_DIR, 'expense_tracker.db'))
try:
    # Avoid fire-and-forget create_task — sync can race an empty DB.
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.run(db.initialize())

sync_service = SyncService(db)

@mcp.tool()
async def sync_emails(days: int = 7) -> str:
    """
    Sync HDFC transaction emails from Gmail for the last N days.
    """
    await db.ensure_initialized()
    synced, errors = await sync_service.sync_hdfc_emails(days=days)
    return f"Sync complete. Added {synced} new transactions. Encounted {errors} errors."

@mcp.tool()
async def add_transaction(
    txn_date: str, 
    amount: float, 
    merchant_raw: str, 
    category: str = "Uncategorized", 
    payment_method: str = "Manual", 
    notes: str = None
) -> str:
    """
    Manually add a new transaction.
    txn_date format: 'YYYY-MM-DD HH:MM:SS'
    """
    await db.execute(
        """
        INSERT INTO transactions (txn_date, amount, merchant_raw, category, payment_method, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (txn_date, amount, merchant_raw, category, payment_method, notes)
    )
    return "Transaction added successfully."

@mcp.tool()
async def update_transaction(transaction_id: int, updates: dict) -> str:
    """
    Update specific fields of a transaction.
    updates: dictionary of field names and new values.
    """
    if not updates:
        return "No updates provided."
    
    set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
    values = list(updates.values())
    values.append(transaction_id)
    
    await db.execute(
        f"UPDATE transactions SET {set_clause} WHERE id = ?",
        tuple(values)
    )
    return f"Transaction {transaction_id} updated successfully."

@mcp.tool()
async def delete_transaction(transaction_id: int) -> str:
    """
    Delete a transaction by ID.
    """
    await db.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
    return f"Transaction {transaction_id} deleted successfully."

@mcp.tool()
async def merge_duplicates(transaction_ids: list[int]) -> str:
    """
    Merge multiple duplicate transactions into one.
    The first ID in the list is kept, others are deleted.
    """
    if len(transaction_ids) < 2:
        return "At least two transaction IDs are required to merge."
    
    primary_id = transaction_ids[0]
    duplicates = transaction_ids[1:]
    
    placeholder = ", ".join(["?"] * len(duplicates))
    await db.execute(f"DELETE FROM transactions WHERE id IN ({placeholder})", tuple(duplicates))
    
    return f"Merged {len(duplicates)} duplicates into transaction {primary_id}."

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
    Merchant budgets match any transaction whose merchant name contains the given text (case-insensitive).
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

    await db.execute(
        """
        INSERT INTO budgets (scope_type, scope_value, period, amount_limit)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(scope_type, scope_value, period)
        DO UPDATE SET amount_limit = excluded.amount_limit
        """,
        (scope_type, scope_value, period, amount_limit),
    )
    return (
        f"Budget set: {scope_type} '{scope_value}' → Rs. {amount_limit:.2f} ({period}). "
        f"Use budget_status or budget_breaches to monitor it."
    )

async def _budget_rows(period: str = "monthly") -> list:
    """Compute spend vs limit for all budgets in the period."""
    await db.ensure_initialized()
    date_format = "%Y-%m" if period == "monthly" else "%Y"
    budgets = await db.fetch_all(
        "SELECT scope_type, scope_value, period, amount_limit FROM budgets WHERE period = ?",
        (period,),
    )
    rows = []
    for b in budgets:
        if b["scope_type"] == "category":
            spend_row = await db.fetch_one(
                """
                SELECT COALESCE(SUM(amount), 0) AS current_spend
                FROM transactions
                WHERE lower(category) = lower(?)
                  AND strftime(?, txn_date) = strftime(?, 'now')
                """,
                (b["scope_value"], date_format, date_format),
            )
        else:
            spend_row = await db.fetch_one(
                """
                SELECT COALESCE(SUM(amount), 0) AS current_spend
                FROM transactions
                WHERE lower(merchant_raw) LIKE '%' || lower(?) || '%'
                  AND strftime(?, txn_date) = strftime(?, 'now')
                """,
                (b["scope_value"], date_format, date_format),
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
    """
    Check current spending against all category and merchant budgets for the period.
    Each row includes breached=true/false and utilization_pct.
    """
    if period not in ("monthly", "yearly"):
        return [{"error": "period must be 'monthly' or 'yearly'."}]
    return await _budget_rows(period)

@mcp.tool()
async def budget_breaches(period: str = "monthly") -> list:
    """
    Return only budgets that are currently breached (spend > limit).
    Call this to alert the user about overspending.
    """
    if period not in ("monthly", "yearly"):
        return [{"error": "period must be 'monthly' or 'yearly'."}]
    return [row for row in await _budget_rows(period) if row["breached"]]

@mcp.tool()
async def add_rule(pattern: str, category: str, field: str = "merchant_raw") -> str:
    """
    Add a categorization rule. When a transaction matches the pattern, 
    it will be assigned the specified category.
    """
    await db.execute(
        """
        INSERT INTO rules (pattern, field, category)
        VALUES (?, ?, ?)
        ON CONFLICT(pattern, field) DO UPDATE SET category = excluded.category
        """,
        (pattern, field, category)
    )
    return f"Rule added: Transactions with {field} matching '{pattern}' will be categorized as '{category}'."

@mcp.tool()
async def categorize_pending() -> str:
    """
    Apply rules to all 'Uncategorized' transactions.
    """
    rules = await db.fetch_all("SELECT * FROM rules")
    if not rules:
        return "No rules found. Please add some rules first using add_rule."
    
    uncategorized = await db.fetch_all("SELECT id, merchant_raw, notes FROM transactions WHERE category = 'Uncategorized'")
    if not uncategorized:
        return "No uncategorized transactions found."
    
    updated_count = 0
    for txn in uncategorized:
        for rule in rules:
            field_to_check = txn.get(rule['field'])
            if field_to_check and rule['pattern'].lower() in field_to_check.lower():
                await db.execute(
                    "UPDATE transactions SET category = ? WHERE id = ?",
                    (rule['category'], txn['id'])
                )
                updated_count += 1
                break
                
    return f"Categorization complete. Updated {updated_count} transactions based on rules."

@mcp.tool()
async def query_transactions(filter_text: str = None, limit: int = 10) -> list:
    """
    Search transactions by merchant, category, or notes.
    """
    if filter_text:
        # Use FTS5 for search
        query = """
            SELECT t.* FROM transactions t
            JOIN transactions_fts f ON t.id = f.rowid
            WHERE transactions_fts MATCH ?
            ORDER BY t.txn_date DESC
            LIMIT ?
        """
        return await db.fetch_all(query, (filter_text, limit))
    else:
        return await db.fetch_all("SELECT * FROM transactions ORDER BY txn_date DESC LIMIT ?", (limit,))

@mcp.tool()
async def spending_summary(period: str = "month") -> list:
    """
    Get a summary of spending grouped by merchant for the current period.
    period: 'month' or 'year'
    """
    date_format = "%Y-%m" if period == "month" else "%Y"
    query = """
        SELECT merchant_raw, SUM(amount) as total_spend, COUNT(*) as txn_count
        FROM transactions
        WHERE strftime(?, txn_date) = strftime(?, 'now')
        GROUP BY merchant_raw
        ORDER BY total_spend DESC
    """
    return await db.fetch_all(query, (date_format, date_format))

@mcp.tool()
async def top_merchants(n: int = 5, period: str = "all") -> list:
    """
    Get the top N merchants by total spending.
    period: 'month', 'year', or 'all'
    """
    if period == "all":
        query = """
            SELECT merchant_raw, SUM(amount) as total_spend, COUNT(*) as txn_count
            FROM transactions
            GROUP BY merchant_raw
            ORDER BY total_spend DESC
            LIMIT ?
        """
        return await db.fetch_all(query, (n,))
    else:
        date_format = "%Y-%m" if period == "month" else "%Y"
        query = """
            SELECT merchant_raw, SUM(amount) as total_spend, COUNT(*) as txn_count
            FROM transactions
            WHERE strftime(?, txn_date) = strftime(?, 'now')
            GROUP BY merchant_raw
            ORDER BY total_spend DESC
            LIMIT ?
        """
        return await db.fetch_all(query, (date_format, date_format, n))

@mcp.tool()
async def anomaly_report(days_baseline: int = 90, z_threshold: float = 2.0) -> list:
    """
    Detect transactions in the last 7 days that are statistically higher than average.
    Uses Z-score (std deviations from mean) over the specified baseline.
    """
    # Calculate stats per category over baseline
    stats_query = """
        WITH stats AS (
            SELECT 
                category,
                AVG(amount) as avg_amt,
                -- Simple stddev approx: sqrt(avg(x^2) - avg(x)^2)
                SQRT(AVG(amount * amount) - (AVG(amount) * AVG(amount))) as std_amt
            FROM transactions
            WHERE txn_date >= date('now', ?)
            GROUP BY category
        )
        SELECT 
            t.id, t.txn_date, t.amount, t.merchant_raw, t.category,
            s.avg_amt, s.std_amt,
            (t.amount - s.avg_amt) / NULLIF(s.std_amt, 0) as z_score
        FROM transactions t
        JOIN stats s ON t.category = s.category
        WHERE t.txn_date >= date('now', '-7 days')
          AND (t.amount - s.avg_amt) / NULLIF(s.std_amt, 0) > ?
        ORDER BY z_score DESC
    """
    return await db.fetch_all(stats_query, (f"-{days_baseline} days", z_threshold))

@mcp.tool()
async def export_data(period: str = "all") -> str:
    """
    Export transactions to an Excel-compatible CSV file.
    period: 'month', 'year', or 'all'
    """
    import csv
    import os
    from datetime import datetime
    
    if period == "all":
        query = "SELECT txn_date, amount, merchant_raw, category, payment_method, instrument_last4, notes FROM transactions ORDER BY txn_date DESC"
        params = ()
    else:
        date_format = "%Y-%m" if period == "month" else "%Y"
        query = "SELECT txn_date, amount, merchant_raw, category, payment_method, instrument_last4, notes FROM transactions WHERE strftime(?, txn_date) = strftime(?, 'now') ORDER BY txn_date DESC"
        params = (date_format, date_format)
        
    rows = await db.fetch_all(query, params)
    if not rows:
        return "No transactions to export."
        
    filename = f"expenses_export_{period}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    filepath = os.path.join(BASE_DIR, filename)
    
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        
    return f"Successfully exported {len(rows)} transactions to {filepath}"

@mcp.resource("resource://current_month_total")
async def get_current_month_total() -> str:
    """Returns the total spend for the current month."""
    result = await db.fetch_one(
        "SELECT SUM(amount) as total FROM transactions WHERE strftime('%Y-%m', txn_date) = strftime('%Y-%m', 'now')"
    )
    total = result['total'] if result and result['total'] else 0
    return f"Total spend this month: Rs. {total:.2f}"

if __name__ == "__main__":
    mcp.run()
