# Expense Tracker MCP Server

A **Model Context Protocol (MCP)** expense tracker. It pulls bank transaction alerts from Gmail, parses them with deterministic regex (no LLM on the critical path), stores them in **Supabase Postgres** (rows scoped by `user_id`), and exposes tools so AI clients can query and manage spending in natural language.

Supports a **local single-user mode** (stdio) and a **2-person pilot multi-user mode** (HTTP + static bearer tokens).

## Features

- **Gmail ingestion** via OAuth2 (`gmail.readonly`) with duplicate-safe logging
- **Bank parsers** for HDFC and Axis transaction emails (HTML-resilient regex)
- **Supabase Postgres ledger** — durable, multi-tenant via `user_id` + full-text search
- **App-level auth** via static bearer tokens mapped to user IDs (`AUTH_MODE=static`)
- **MCP tools** for sync, search, analytics, CRUD, budgets, and CSV export
- **Airtel POC clients** (Azure OpenAI + Gradio / CLI) for local demos
- **Budget breach alerts** injected into the LLM system prompt

## Architecture

```
                    ┌── Bearer token → user_id (AUTH_MODE=static)
                    │
Client (Claude / Cursor / HTTP MCP)
                    │
                    ▼
              main.py (FastMCP)
                    │
         ┌──────────┼──────────┐
         ▼          ▼          ▼
   Supabase     Gmail token   Gmail token
   Postgres     alice         bob
   (user_id)    data/users/   data/users/
                alice/        bob/
```

| Layer | Role |
| --- | --- |
| **Auth** | `AUTH_MODE=none` (local stdio) or `static` (per-user bearer tokens) |
| **Ingestion** | Gmail API + HDFC/Axis parsers, scoped to the caller's Gmail token |
| **Storage** | Shared Supabase Postgres; every row has `user_id` |
| **MCP server** | FastMCP tools + `resource://current_month_total` |
| **LLM clients** | Claude Desktop, Gemini CLI, remote MCP clients, Airtel POC |

## Project layout

```
expense-tracker/
├── main.py                 # FastMCP server entrypoint
├── auth/
│   ├── users.py            # User registry (users.json / env)
│   └── identity.py         # Current user + StaticTokenVerifier
├── db/
│   ├── manager.py          # asyncpg pool + schema + helpers
│   └── tenant.py           # Per-user facade + Gmail wiring
├── ingestion/
│   ├── gmail_client.py
│   ├── sync.py
│   └── parsers/
├── llm_budget.py
├── airtel_ui.py / airtel_poc_client.py
├── users.example.json      # Copy → users.json (gitignored)
├── .env.example            # Includes DATABASE_URL
└── data/users/{user_id}/   # Gmail tokens + CSV exports (gitignored)
```

## Supported banks

| Bank | What is parsed |
| --- | --- |
| **HDFC** | Credit card (legacy & new), account UPI, RuPay CC UPI |
| **Axis** | Credit card POS transaction alerts |

## Prerequisites

- **Python 3.10+** (developed with 3.12)
- **Supabase project** with `DATABASE_URL` in `.env` (see `.env.example`)
- **Gmail OAuth** client (`credentials.json` or `GMAIL_CREDENTIALS_JSON`)
- **Per-user Gmail tokens** for multi-user (see below)
- **Optional:** Azure OpenAI for Airtel POC clients

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Multi-user pilot (2 people)

This is the path for the remote server after the demo: **authenticate every caller** and **isolate each person's ledger + Gmail**.

### 1. Create `users.json`

```bash
cp users.example.json users.json
# Edit tokens to long random secrets (do not commit users.json)
```

Example:

```json
{
  "default_user_id": "alice",
  "users": [
    {
      "user_id": "alice",
      "display_name": "Alice",
      "token": "long-random-token-for-alice",
      "gmail_token_path": "data/users/alice/token.json"
    },
    {
      "user_id": "bob",
      "display_name": "Bob",
      "token": "long-random-token-for-bob",
      "gmail_token_path": "data/users/bob/token.json"
    }
  ]
}
```

Or use env shorthand:

```bash
export AUTH_MODE=static
export AUTH_TOKENS="alice:long-token-1,bob:long-token-2"
```

### 2. Environment

```bash
export AUTH_MODE=static
export USERS_CONFIG=./users.json
export DEFAULT_USER_ID=alice
# Optional: one-time copy of legacy single-user files
export MIGRATE_LEGACY_DB_TO=alice
export MIGRATE_LEGACY_GMAIL_TO=alice
```

See [`.env.example`](.env.example) for the full list.

### 3. Per-user Gmail setup

Each person must complete Gmail OAuth once (or you place their `token.json`):

```text
data/users/alice/token.json
data/users/bob/token.json
```

Shared OAuth **client** secrets stay in `credentials.json` (or `GMAIL_CREDENTIALS_JSON`).  
Refresh **tokens** are per user. On headless cloud, put each token JSON in secrets, e.g. `GMAIL_TOKEN_JSON_ALICE`, and set `gmail_token_env` in `users.json`.

### 4. What isolation means

| Resource | Isolation |
| --- | --- |
| Ledger | Shared Postgres; every query filters `user_id` |
| Gmail | That user's `token.json` / env only |
| Budgets / rules | Rows with that `user_id` |
| Exports | Written under that user's local data dir |

User A **cannot** query or sync user B's data: tools always resolve the caller via `get_current_user()` from the bearer token and pass `user_id` into SQL.

### 5. Run remote HTTP (self-hosted)

```bash
export AUTH_MODE=static
export MCP_TRANSPORT=http
export MCP_PORT=8000
python main.py
```

Clients send:

```http
Authorization: Bearer long-random-token-for-alice
```

### 6. FastMCP Cloud / Horizon

1. Keep **platform authentication enabled** (Horizon OAuth / bearer).
2. Also configure **app auth** secrets on the deployment:
   - `AUTH_MODE=static`
   - `DATABASE_URL` (Supabase connection string)
   - `users.json` contents or `AUTH_TOKENS=...`
   - Per-user `GMAIL_TOKEN_JSON_*` (never commit these)
3. Give **person 1** and **person 2** only their own app bearer token (from `users.json`).
4. Call `whoami` after connect to confirm the right `user_id`.

> **Note:** Platform auth proves “allowed to hit the deployment.”  
> App tokens prove “you are alice vs bob” and select the ledger rows.  
> For a 2-person pilot, static app tokens are intentional and simple. Later you can swap `StaticTokenVerifier` for JWT/OIDC and map `email` → `user_id`.

### 7. Migrating old local SQLite data

Local `expense_tracker.db` / `data/users/*/expense_tracker.db` files are **no longer read** by the app. Export CSV from the old files if needed, then re-import via `add_transaction` / `sync_emails`, or load into Supabase with a one-off SQL script.

Gmail tokens still live under `data/users/{user_id}/token.json` (or env overrides).

---

## Local single-user (Claude Desktop / Gemini)

No bearer tokens required:

```bash
export AUTH_MODE=none
export DEFAULT_USER_ID=alice   # optional; default is "local"
python main.py                 # stdio
```

Claude Desktop config:

```json
{
  "mcpServers": {
    "expense-tracker": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/expense-tracker/main.py"],
      "env": {
        "PYTHONPATH": "/path/to/expense-tracker",
        "AUTH_MODE": "none",
        "DEFAULT_USER_ID": "alice"
      }
    }
  }
}
```

Same block works in `~/.gemini/settings.json`.

---

## Gmail setup (shared client app)

1. Google Cloud project → enable **Gmail API** → OAuth client (Desktop).
2. `credentials.json` in project root (or `GMAIL_CREDENTIALS_JSON`).
3. Redirect URI: `http://localhost:8080`.
4. Scope: `https://www.googleapis.com/auth/gmail.readonly`.
5. Each user runs OAuth once; token stored under their `data/users/{id}/` path.

| Variable | Purpose |
| --- | --- |
| `GMAIL_CREDENTIALS_JSON` | Shared OAuth client config |
| `GMAIL_TOKEN_JSON` | Legacy single-user token |
| `GMAIL_TOKEN_JSON_<USER>` | Per-user token when set via `gmail_token_env` in `users.json` |

---

## Airtel POC clients

Local stdio only (`AUTH_MODE=none`). They spawn `main.py` and call tools with Azure OpenAI.

```bash
python airtel_ui.py          # http://127.0.0.1:7860
python airtel_poc_client.py "What are my top 3 merchants?"
```

---

## MCP tools

| Area | Tools |
| --- | --- |
| **Identity** | `whoami` |
| **Ingestion** | `sync_emails(days)` |
| **Search / analytics** | `query_transactions`, `spending_summary`, `top_merchants`, `anomaly_report` |
| **CRUD** | `add_transaction`, `update_transaction` (allowlisted fields), `delete_transaction`, `merge_duplicates` |
| **Rules / budgets** | `add_rule`, `categorize_pending`, `set_budget`, `budget_status`, `budget_breaches` |
| **Export** | `export_data` → CSV under the user's data dir |
| **Resource** | `resource://current_month_total` |

## Data model (Supabase Postgres)

- **`transactions`**, **`rules`**, **`budgets`**, **`categories`**, **`ingestion_log`**
- Every tenant table includes **`user_id`**
- Search: `search_vector` (`tsvector`) + GIN index (replaces SQLite FTS5)
- Budgets: `scope_type` = `category` \| `merchant`
- Schema is applied automatically on startup via `db/manager.py` (idempotent)

## Example prompts

- *“Who am I connected as?”* → `whoami`
- *“Sync my emails for the last 5 days.”*
- *“What was my total spend at SWIGGY this month?”*
- *“Set a monthly budget of Rs. 5000 for Food.”*
- *“Have I breached any budgets?”*
- *“Export my transactions.”*

## Security notes

- **Never commit** `users.json`, `credentials.json`, `token.json`, `.env`, or `data/`.
- **Never expose** `DATABASE_URL` or the DB password in client-side code.
- **Remote deploys must use `AUTH_MODE=static`** (or equivalent JWT later). An open URL with a shared ledger is unsafe.
- Horizon/platform auth alone is not enough for two people sharing one server — you need **per-user tokens + `user_id` scoping** (this repo).
- Gmail scope is read-only.
- `update_transaction` only accepts allowlisted column names.
- For leadership demos, prefer synthetic data or local stdio over live Gmail on a public host.

## Scaling beyond 2 users (later)

| Need | Direction |
| --- | --- |
| More users / concurrent writes | Already on Postgres + `user_id`; add connection pooling limits / read replicas as needed |
| Real SSO | Replace `StaticTokenVerifier` with JWT/OIDC; map `email` → `user_id` |
| Gmail at scale | Hosted OAuth connect flow per user; store refresh tokens in a secret manager |
| Tool permissions | Scopes: read-only vs sync/export for some tokens |
| API-level isolation | Optional Supabase RLS policies if using the REST API (not required for direct `asyncpg`) |

The tenant interface (`db/tenant.py` + `get_current_user()`) remains the seam for auth/storage evolution.

## License

MIT — see [LICENSE](LICENSE).
