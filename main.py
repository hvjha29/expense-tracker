from fastmcp import FastMCP
from db.manager import DatabaseManager
from ingestion.sync import SyncService
import datetime

# Initialize MCP server
mcp = FastMCP("ExpenseTracker")

# Initialize backend
db = DatabaseManager()
sync_service = SyncService(db)

@mcp.tool()
def sync_emails(days: int = 7) -> str:
    """
    Sync HDFC transaction emails from Gmail for the last N days.
    """
    synced, errors = sync_service.sync_hdfc_emails(days=days)
    return f"Sync complete. Added {synced} new transactions. Encounted {errors} errors."

@mcp.tool()
def add_transaction(
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
    db.execute(
        """
        INSERT INTO transactions (txn_date, amount, merchant_raw, category, payment_method, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (txn_date, amount, merchant_raw, category, payment_method, notes)
    )
    return "Transaction added successfully."

@mcp.tool()
def update_transaction(transaction_id: int, updates: dict) -> str:
    """
    Update specific fields of a transaction.
    updates: dictionary of field names and new values.
    """
    if not updates:
        return "No updates provided."
    
    set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
    values = list(updates.values())
    values.append(transaction_id)
    
    db.execute(
        f"UPDATE transactions SET {set_clause} WHERE id = ?",
        tuple(values)
    )
    return f"Transaction {transaction_id} updated successfully."

@mcp.tool()
def delete_transaction(transaction_id: int) -> str:
    """
    Delete a transaction by ID.
    """
    db.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
    return f"Transaction {transaction_id} deleted successfully."

@mcp.tool()
def merge_duplicates(transaction_ids: list[int]) -> str:
    """
    Merge multiple duplicate transactions into one.
    The first ID in the list is kept, others are deleted.
    """
    if len(transaction_ids) < 2:
        return "At least two transaction IDs are required to merge."
    
    primary_id = transaction_ids[0]
    duplicates = transaction_ids[1:]
    
    placeholder = ", ".join(["?"] * len(duplicates))
    db.execute(f"DELETE FROM transactions WHERE id IN ({placeholder})", tuple(duplicates))
    
    return f"Merged {len(duplicates)} duplicates into transaction {primary_id}."

@mcp.tool()
def set_budget(category: str, amount_limit: float, period: str = "monthly") -> str:
    """
    Set or update a budget limit for a category.
    period: 'monthly' or 'yearly'
    """
    db.execute(
        """
        INSERT INTO budgets (category, period, amount_limit)
        VALUES (?, ?, ?)
        ON CONFLICT(category, period) DO UPDATE SET amount_limit = excluded.amount_limit
        """,
        (category, period, amount_limit)
    )
    return f"Budget for {category} set to Rs. {amount_limit} ({period})."

@mcp.tool()
def budget_status(period: str = "monthly") -> list:
    """
    Check current spending against budget limits for the current period.
    """
    date_format = "%Y-%m" if period == "monthly" else "%Y"
    
    query = """
        SELECT 
            b.category, 
            b.amount_limit,
            COALESCE(SUM(t.amount), 0) as current_spend,
            (b.amount_limit - COALESCE(SUM(t.amount), 0)) as remaining
        FROM budgets b
        LEFT JOIN transactions t ON b.category = t.category 
            AND strftime(?, t.txn_date) = strftime(?, 'now')
        WHERE b.period = ?
        GROUP BY b.category
    """
    return db.fetch_all(query, (date_format, date_format, period))

@mcp.tool()
def add_rule(pattern: str, category: str, field: str = "merchant_raw") -> str:
    """
    Add a categorization rule. When a transaction matches the pattern, 
    it will be assigned the specified category.
    """
    db.execute(
        """
        INSERT INTO rules (pattern, field, category)
        VALUES (?, ?, ?)
        ON CONFLICT(pattern, field) DO UPDATE SET category = excluded.category
        """,
        (pattern, field, category)
    )
    return f"Rule added: Transactions with {field} matching '{pattern}' will be categorized as '{category}'."

@mcp.tool()
def categorize_pending() -> str:
    """
    Apply rules to all 'Uncategorized' transactions.
    """
    rules = db.fetch_all("SELECT * FROM rules")
    if not rules:
        return "No rules found. Please add some rules first using add_rule."
    
    uncategorized = db.fetch_all("SELECT id, merchant_raw, notes FROM transactions WHERE category = 'Uncategorized'")
    if not uncategorized:
        return "No uncategorized transactions found."
    
    updated_count = 0
    for txn in uncategorized:
        for rule in rules:
            field_to_check = txn.get(rule['field'])
            if field_to_check and rule['pattern'].lower() in field_to_check.lower():
                db.execute(
                    "UPDATE transactions SET category = ? WHERE id = ?",
                    (rule['category'], txn['id'])
                )
                updated_count += 1
                break
                
    return f"Categorization complete. Updated {updated_count} transactions based on rules."

@mcp.tool()
def query_transactions(filter_text: str = None, limit: int = 10) -> list:
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
        return db.fetch_all(query, (filter_text, limit))
    else:
        return db.fetch_all("SELECT * FROM transactions ORDER BY txn_date DESC LIMIT ?", (limit,))

@mcp.tool()
def spending_summary(period: str = "month") -> list:
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
    return db.fetch_all(query, (date_format, date_format))

@mcp.tool()
def top_merchants(n: int = 5, period: str = "all") -> list:
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
        return db.fetch_all(query, (n,))
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
        return db.fetch_all(query, (date_format, date_format, n))

@mcp.tool()
def anomaly_report(days_baseline: int = 90, z_threshold: float = 2.0) -> list:
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
    return db.fetch_all(stats_query, (f"-{days_baseline} days", z_threshold))

@mcp.tool()
def export_data(period: str = "all") -> str:
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
        
    rows = db.fetch_all(query, params)
    if not rows:
        return "No transactions to export."
        
    filename = f"expenses_export_{period}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    filepath = os.path.join(os.getcwd(), filename)
    
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        
    return f"Successfully exported {len(rows)} transactions to {filepath}"

@mcp.resource("resource://current_month_total")
def get_current_month_total() -> str:
    """Returns the total spend for the current month."""
    result = db.fetch_one(
        "SELECT SUM(amount) as total FROM transactions WHERE strftime('%Y-%m', txn_date) = strftime('%Y-%m', 'now')"
    )
    total = result['total'] if result and result['total'] else 0
    return f"Total spend this month: Rs. {total:.2f}"

if __name__ == "__main__":
    mcp.run()
