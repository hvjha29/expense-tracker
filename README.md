# Expense Tracker MCP Server

A sophisticated personal finance tracker designed to operate as a local **Model Context Protocol (MCP)** server. It securely ingests transaction alerts from your Gmail, parses them deterministically, and provides an intelligent SQL-backed ledger that you can query and manage using natural language via clients like Claude Desktop or Gemini CLI.

## Architecture

*   **Ingestion Pipeline:** Uses Gmail OAuth2 for direct API access to pull transaction emails.
*   **Parsing Engine:** Uses deterministic regex parsing, resilient to HTML formatting. Currently supports:
    *   **HDFC Bank:** Credit Card (Legacy & New), Account UPI, RuPay CC UPI.
    *   **Axis Bank:** Credit Card POS.
*   **Storage:** Local SQLite database (`expense_tracker.db`) using WAL mode for concurrency and FTS5 for fast merchant searches.
*   **Server Layer:** Built with `FastMCP`, exposing tools and resources directly to your LLM.

---

## 🛠 Available Tools

The MCP server exposes the following tools to your AI client:

### 1. Ingestion
*   **`sync_emails(days)`**: Fetches and parses new transaction emails from Gmail. Skips duplicates automatically.

### 2. Analytics & Querying
*   **`query_transactions(filter_text, limit)`**: Search transactions by merchant, category, or notes using Full-Text Search.
*   **`spending_summary(period)`**: Aggregates total spend and transaction count grouped by merchant (monthly or yearly).
*   **`top_merchants(n, period)`**: Returns your highest-spending merchants.
*   **`anomaly_report(days_baseline, z_threshold)`**: Detects statistically unusual spending (Z-score based) in the last 7 days compared to your historical average.

### 3. Data Management (CRUD)
*   **`add_transaction(txn_date, amount, merchant_raw, ...)`**: Manually add a transaction.
*   **`update_transaction(transaction_id, updates_dict)`**: Surgically update specific fields of a record.
*   **`delete_transaction(transaction_id)`**: Remove a transaction.
*   **`merge_duplicates(transaction_ids)`**: Merge multiple duplicate alerts into a single record.

### 4. Categorization & Budgets
*   **`add_rule(pattern, category)`**: Create a "Learning Loop." e.g., Set a rule that any merchant containing "SWIGGY" becomes "Food & Dining".
*   **`categorize_pending()`**: Applies all your rules to any currently 'Uncategorized' transactions.
*   **`set_budget(category, amount_limit, period)`**: Set a spending limit for a specific category.
*   **`budget_status(period)`**: View your current spend against your defined budgets.

### 5. Export
*   **`export_data(period)`**: Generates an Excel-compatible `.csv` file of your ledger.

---

## 🚀 How to Use

This server runs locally on your machine. Your financial data is never uploaded to the cloud.

### 1. Prerequisites
Ensure the Python environment is set up. The server requires Python 3.10+ (specifically Python 3.12 was used in setup).
```bash
pip install -r requirements.txt
```
*Note: The project requires `credentials.json` and `token.json` from Google Cloud to access Gmail.*

### 2. Connect to Claude Desktop
To interact with your tracker using Claude, add the server to your Claude configuration file located at `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "expense-tracker": {
      "command": "/opt/homebrew/bin/python3.12",
      "args": [
        "/Users/B0308529/Documents/expense-tracker/main.py"
      ],
      "env": {
        "PYTHONPATH": "/Users/B0308529/Documents/expense-tracker"
      }
    }
  }
}
```
*Restart Claude Desktop after updating the file.*

### 3. Connect to Gemini CLI
You can also automate tasks using the Gemini CLI. Add the same configuration to your `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "expense-tracker": {
      "command": "/opt/homebrew/bin/python3.12",
      "args": [
        "/Users/B0308529/Documents/expense-tracker/main.py"
      ],
      "env": {
        "PYTHONPATH": "/Users/B0308529/Documents/expense-tracker"
      }
    }
  }
}
```
Verify it is connected by running `gemini /mcp list` in your terminal.

---

## 💡 Example Prompts

Once connected to your LLM, try asking:
*   *"Sync my emails for the last 5 days."*
*   *"What was my total spend at SWIGGY this month?"*
*   *"Set a monthly budget of Rs. 5000 for Food, and tell me my current status."*
*   *"Export my transactions to an Excel file."*
*   *"Are there any anomalies in my spending this week?"*
