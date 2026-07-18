"""Authentication and multi-user identity for the Expense Tracker MCP server."""

from auth.users import UserConfig, UserRegistry, load_user_registry
from auth.identity import get_current_user, build_auth_provider

__all__ = [
    "UserConfig",
    "UserRegistry",
    "load_user_registry",
    "get_current_user",
    "build_auth_provider",
]
