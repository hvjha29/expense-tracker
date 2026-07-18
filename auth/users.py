"""User registry for the 2-person pilot (and small multi-tenant deploys).

Config sources (first match wins for file path):
  1. USERS_CONFIG env → path to JSON file
  2. ./users.json in project root

JSON shape:
{
  "default_user_id": "alice",
  "users": [
    {
      "user_id": "alice",
      "display_name": "Alice",
      "token": "long-random-bearer-token",
      "gmail_token_env": "GMAIL_TOKEN_JSON_ALICE",   // optional
      "gmail_token_path": "data/users/alice/token.json"  // optional
    },
    {
      "user_id": "bob",
      "display_name": "Bob",
      "token": "another-long-random-bearer-token"
    }
  ]
}

AUTH_MODE:
  - static  → require bearer tokens from this registry (remote / multi-user)
  - none    → no HTTP auth; use DEFAULT_USER_ID / default_user_id (local stdio)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

BASE_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = BASE_DIR / "data" / "users"

_SAFE_USER_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass(frozen=True)
class UserConfig:
    user_id: str
    display_name: str
    token: str
    gmail_token_env: Optional[str] = None
    gmail_token_path: Optional[str] = None

    def data_dir(self) -> Path:
        """Local dir for Gmail tokens / CSV exports (ledger is Postgres)."""
        return DATA_DIR / self.user_id

    def db_path(self) -> Path:
        """Deprecated path kept for compatibility; ledger is DATABASE_URL."""
        return self.data_dir() / "expense_tracker.db"

    def resolved_gmail_token_path(self) -> Path:
        if self.gmail_token_path:
            p = Path(self.gmail_token_path)
            return p if p.is_absolute() else BASE_DIR / p
        return self.data_dir() / "token.json"


@dataclass
class UserRegistry:
    users: dict[str, UserConfig] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)  # token -> user_id
    default_user_id: Optional[str] = None
    auth_mode: str = "none"  # "none" | "static"

    def get(self, user_id: str) -> UserConfig:
        if user_id not in self.users:
            raise KeyError(f"Unknown user_id: {user_id}")
        return self.users[user_id]

    def resolve_token(self, token: str) -> Optional[UserConfig]:
        uid = self.tokens.get(token)
        return self.users.get(uid) if uid else None

    def list_user_ids(self) -> list[str]:
        return list(self.users.keys())


def _validate_user_id(user_id: str) -> str:
    if not _SAFE_USER_ID.match(user_id):
        raise ValueError(
            f"Invalid user_id '{user_id}'. Use letters, digits, _ or - only."
        )
    return user_id


def _load_json_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_user_registry(base_dir: Optional[Path] = None) -> UserRegistry:
    """Load users from JSON + env. Always returns a registry (single-user fallback)."""
    root = base_dir or BASE_DIR
    auth_mode = os.environ.get("AUTH_MODE", "none").strip().lower()
    if auth_mode not in ("none", "static"):
        raise ValueError("AUTH_MODE must be 'none' or 'static'")

    config_path = os.environ.get("USERS_CONFIG")
    candidates = []
    if config_path:
        candidates.append(Path(config_path))
    candidates.append(root / "users.json")

    raw: Optional[dict] = None
    for cand in candidates:
        if cand.is_file():
            raw = _load_json_config(cand)
            break

    registry = UserRegistry(auth_mode=auth_mode)

    if raw and raw.get("users"):
        default_id = (
            os.environ.get("DEFAULT_USER_ID")
            or raw.get("default_user_id")
            or None
        )
        for entry in raw["users"]:
            uid = _validate_user_id(entry["user_id"])
            token = (entry.get("token") or "").strip()
            if auth_mode == "static" and not token:
                raise ValueError(
                    f"User '{uid}' is missing a non-empty token (required when AUTH_MODE=static)"
                )
            cfg = UserConfig(
                user_id=uid,
                display_name=entry.get("display_name") or uid,
                token=token,
                gmail_token_env=entry.get("gmail_token_env"),
                gmail_token_path=entry.get("gmail_token_path"),
            )
            registry.users[uid] = cfg
            if token:
                if token in registry.tokens:
                    raise ValueError("Duplicate bearer tokens in users config")
                registry.tokens[token] = uid

        if default_id:
            default_id = _validate_user_id(default_id)
            if default_id not in registry.users:
                raise ValueError(f"default_user_id '{default_id}' not in users list")
            registry.default_user_id = default_id
        else:
            registry.default_user_id = next(iter(registry.users))
    else:
        # Single-user fallback for local / legacy installs
        uid = _validate_user_id(os.environ.get("DEFAULT_USER_ID", "local"))
        token = os.environ.get("AUTH_TOKEN", "").strip()
        cfg = UserConfig(
            user_id=uid,
            display_name=os.environ.get("DEFAULT_USER_NAME", uid),
            token=token,
            gmail_token_env=os.environ.get("GMAIL_TOKEN_ENV"),
            gmail_token_path=None,
        )
        registry.users[uid] = cfg
        if token:
            registry.tokens[token] = uid
        registry.default_user_id = uid

        if auth_mode == "static" and not token and not registry.tokens:
            # Allow AUTH_TOKENS=alice:tok1,bob:tok2 shorthand
            shorthand = os.environ.get("AUTH_TOKENS", "").strip()
            if shorthand:
                registry.users.clear()
                registry.tokens.clear()
                for part in shorthand.split(","):
                    part = part.strip()
                    if not part or ":" not in part:
                        continue
                    suid, stok = part.split(":", 1)
                    suid = _validate_user_id(suid.strip())
                    stok = stok.strip()
                    if not stok:
                        continue
                    scfg = UserConfig(user_id=suid, display_name=suid, token=stok)
                    registry.users[suid] = scfg
                    registry.tokens[stok] = suid
                if not registry.users:
                    raise ValueError("AUTH_MODE=static but no tokens configured")
                registry.default_user_id = next(iter(registry.users))
            else:
                raise ValueError(
                    "AUTH_MODE=static requires users.json with tokens, "
                    "or AUTH_TOKEN / AUTH_TOKENS env vars"
                )

    return registry
