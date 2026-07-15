"""Shared LLM prompt + budget-breach helpers for Airtel MCP clients."""

SYSTEM_PROMPT = """You are a helpful assistant integrated into the Airtel app for expense tracking.
Use the provided tools to assist the user.

Budgets:
- Users can set monthly/yearly budgets for a category OR a merchant via set_budget.
- Use budget_status to review all budgets; use budget_breaches for overspending only.
- Whenever budget alert context is provided, or after sync/spend/budget tool calls,
  you MUST clearly flag any breached budgets to the user (scope, spend vs limit, overage).
- Be proactive: if the user asks about spending, summaries, merchants, or categories,
  check budgets when relevant and warn on breaches.
"""


def format_breach_alert(breaches) -> str:
    """Turn breach tool output into system context the LLM must surface."""
    if not breaches:
        return ""
    if isinstance(breaches, str):
        try:
            import json
            breaches = json.loads(breaches)
        except Exception:
            return f"\n\nACTIVE BUDGET ALERTS (must flag to user):\n{breaches}\n"

    if not isinstance(breaches, list) or not breaches:
        return ""

    lines = ["\n\nACTIVE BUDGET ALERTS (you MUST flag these to the user):"]
    for b in breaches:
        if not isinstance(b, dict) or not b.get("breached", True):
            continue
        scope = f"{b.get('scope_type', '?')} '{b.get('scope_value', '?')}'"
        spend = b.get("current_spend", 0)
        limit = b.get("amount_limit", 0)
        over = (spend or 0) - (limit or 0)
        lines.append(
            f"- BREACHED {scope} ({b.get('period', 'monthly')}): "
            f"spent Rs. {spend:.2f} / limit Rs. {limit:.2f} "
            f"(over by Rs. {over:.2f}, {b.get('utilization_pct', '?')}%)"
        )
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n"


async def fetch_breach_context(session, period: str = "monthly") -> str:
    """Call MCP budget_breaches and return alert text for the system prompt."""
    import json

    try:
        result = await session.call_tool("budget_breaches", {"period": period})
        text = "\n".join(
            c.text for c in result.content if getattr(c, "type", "") == "text"
        )
        if not text.strip():
            return ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = text
        return format_breach_alert(data)
    except Exception as e:
        print(f"[Budget check skipped] {e}")
        return ""
