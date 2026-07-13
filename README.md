# Architecture
┌─ Gemini CLI (client) ──────────────────────┐
│  natural language: "how much on food in June?"
└──────────────┬─────────────────────────────┘
               │ MCP (stdio)
┌──────────────▼─────────────────────────────┐
│  expense-mcp-server (Python, FastMCP)      │
│  ├── tools/           (MCP tool layer)     │
│  ├── ingestion/                            │
│  │   ├── gmail_client.py   (OAuth2, direct API)
│  │   ├── parsers/          (regex per bank template)
│  │   └── llm_fallback.py   (Gemini API, unrecognized only)
│  ├── core/                                 │
│  │   ├── categorizer.py    (rules → embedding match → LLM)
│  │   ├── dedup.py          (txn_ref hash + fuzzy window)
│  │   └── recurring.py      (subscription detection)
│  └── db/  SQLite (WAL mode)                │
└────────────────────────────────────────────┘

Schema (minimum): transactions(id, txn_date, amount, currency, merchant_raw, merchant_normalized, category, subcategory, account, payment_method, source_email_id UNIQUE, txn_ref UNIQUE, notes, is_recurring, created_at), categories, budgets(category, period, limit), rules(pattern, field, category), ingestion_log.
Dedup is the hardest problem: same transaction arrives via bank email + UPI app email + credit card statement. Dedup key: hash(amount, date±1d, normalized_merchant) with txn_ref as primary key when available. You've dealt with exactly this pattern in the CloudCard retry duplicate issue — same idempotency discipline applies.
# Tool List (MCP capabilities)
# Ingestion

sync_emails(since_date, account) — pull + parse + dedupe transaction emails
parse_receipt(file_path) — OCR/LLM parse of uploaded receipt image/PDF
add_transaction(...) — manual entry
import_statement(file_path, format) — CSV/XLSX/PDF bank statement import
review_unparsed() — list emails that failed deterministic parsing, for LLM/manual triage

# CRUD & correction
6. update_transaction(id, fields)
7. delete_transaction(id)
8. split_transaction(id, splits[]) — one payment, multiple categories
9. merge_duplicates(ids[])
# Categorization
10. categorize_pending() — run rules → fallback LLM on uncategorized
11. add_rule(pattern, category) — persist user corrections as rules (this is your learning loop; never re-ask the LLM for a merchant you've already corrected)
12. list_categories() / manage_category(...)
# Query & analytics
13. query_transactions(filters, free_text) — date/category/merchant/amount range + FTS
14. spending_summary(period, group_by) — month/category/payment-method aggregates
15. trend_analysis(category, window) — MoM/YoY deltas
16. top_merchants(period, n)
17. detect_recurring() — subscriptions, EMIs, SIPs (amount+merchant periodicity)
18. anomaly_report(period) — z-score or IQR on category spend vs trailing baseline

# Budgets & alerts
19. set_budget(category, period, limit)
20. budget_status(period) — spend vs limit, burn rate projection
21. upcoming_recurring(days) — forecast known charges
Export/report
22. export_data(format, period) — CSV/XLSX
23. monthly_report(month) — structured summary (feed to LLM for narrative)
MCP resources (read-only, cheap context): resource://categories, resource://current_month_summary, resource://budget_status.

# Build order
SQLite schema + add_transaction / query_transactions / spending_summary — validate MCP wiring in Gemini CLI end-to-end.
Gmail OAuth + one bank parser (whichever sends you the most alerts). Dedup logic.
Rules-based categorizer + add_rule correction loop.
Remaining parsers, LLM fallback, recurring detection.
Budgets, anomalies, reports.

# Stack
Python + FastMCP (pip install mcp) — fastest path, decorators generate schemas.
google-api-python-client + google-auth-oauthlib for Gmail (readonly scope only).
SQLite via sqlite3 stdlib, WAL mode, FTS5 for merchant search.
Gemini Flash via API for fallback parsing/categorization only — force JSON output (response_mime_type: application/json + response schema), never free text into the ledger.

My expense server calls Gmail API directly (google-api-python-client + OAuth2 refresh token). Deterministic, no LLM in the ingestion loop, cron-able.

LLM parsing of transaction emails is an anti-pattern for Indian bank/UPI alerts. HDFC/ICICI/SBI/Paytm alert emails are templated — regex/parser rules give you 100% deterministic extraction at zero cost. Use LLM parsing only as fallback for unrecognized templates. This matters for a financial ledger: you cannot tolerate hallucinated amounts.

Use SQLite (single-user, zero-ops, ACID, full-text search built in). Postgres is overkill for personal use.