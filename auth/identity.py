"""Resolve the authenticated caller and build FastMCP auth providers."""

from __future__ import annotations

import os
from typing import Optional

from auth.users import UserConfig, UserRegistry, load_user_registry

# Module-level registry (loaded once at import / server start)
_REGISTRY: Optional[UserRegistry] = None


def get_registry() -> UserRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = load_user_registry()
    return _REGISTRY


def reload_registry() -> UserRegistry:
    global _REGISTRY
    _REGISTRY = load_user_registry()
    return _REGISTRY


def build_auth_provider():
    """
    Return a FastMCP auth provider, or None for local unauthenticated stdio.

    AUTH_MODE=static → StaticTokenVerifier with per-user bearer tokens.
    AUTH_MODE=none   → no auth (local Claude Desktop / Gemini stdio).
    """
    registry = get_registry()
    if registry.auth_mode != "static":
        return None

    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    tokens = {}
    for cfg in registry.users.values():
        if not cfg.token:
            continue
        tokens[cfg.token] = {
            "client_id": cfg.user_id,
            "sub": cfg.user_id,
            "name": cfg.display_name,
            "scopes": ["expense:read", "expense:write"],
        }

    if not tokens:
        raise RuntimeError("AUTH_MODE=static but no tokens defined for any user")

    return StaticTokenVerifier(
        tokens=tokens,
        required_scopes=["expense:read"],
    )


def get_current_user() -> UserConfig:
    """
    Resolve the current user for this tool call.

    - HTTP + AUTH_MODE=static: from bearer token claims (client_id / sub)
    - AUTH_MODE=none (stdio): DEFAULT_USER_ID / registry default
    """
    registry = get_registry()

    if registry.auth_mode == "static":
        try:
            from fastmcp.server.dependencies import get_access_token

            token = get_access_token()
        except Exception:
            token = None

        if token is None:
            raise PermissionError(
                "Authentication required. Pass Authorization: Bearer <token>."
            )

        claims = getattr(token, "claims", None) or {}
        user_id = (
            claims.get("client_id")
            or claims.get("sub")
            or getattr(token, "client_id", None)
        )
        if not user_id or user_id not in registry.users:
            # Fallback: match raw token string if available
            raw = getattr(token, "token", None) or claims.get("token")
            if raw:
                cfg = registry.resolve_token(raw)
                if cfg:
                    return cfg
            raise PermissionError(f"Token is not mapped to a known user (got {user_id!r})")
        return registry.get(user_id)

    # Local / unauthenticated mode
    env_default = os.environ.get("DEFAULT_USER_ID")
    if env_default and env_default in registry.users:
        return registry.get(env_default)
    if registry.default_user_id:
        return registry.get(registry.default_user_id)
    raise RuntimeError("No default user configured")
