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
    Get a summary of spending grouped by merchant.
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
