"""Session memory tools for storing and recalling persistent traveler preferences and state."""

from typing import Dict, Any
from google.adk.tools.tool_context import ToolContext


def memorize(key: str, value: str, tool_context: ToolContext) -> dict:
    """Stores a piece of travel information or user preference into session state.

    Args:
        key: The state attribute label (e.g. 'destination', 'travel_dates', 'selected_flight', 'selected_hotel', 'budget_tier').
        value: The value to store.
        tool_context: The ADK ToolContext providing access to session state.

    Returns:
        Confirmation dictionary with saved key and value.
    """
    clean_key = key.strip()
    tool_context.state[clean_key] = value

    # Automatically set chat session display name in UI if destination is specified
    if "destination" in clean_key.lower() and value and str(value).strip().lower() not in ["not set", "none"]:
        dest_title = str(value).strip().title()
        meta = dict(tool_context.state.get("__session_metadata__") or {})
        meta["displayName"] = f"Trip to {dest_title}"
        tool_context.state["__session_metadata__"] = meta

    return {
        "status": "success",
        "saved_key": clean_key,
        "value": value,
        "message": f"Saved {clean_key}: {value}"
    }


def recall(key: str, tool_context: ToolContext) -> dict:
    """Retrieves a previously stored piece of travel information or preference from session state.

    Args:
        key: The key to look up (e.g. 'destination', 'selected_flight', 'selected_hotel').
        tool_context: The ADK ToolContext providing access to session state.

    Returns:
        The stored value or a not found notice.
    """
    clean_key = key.strip()
    value = tool_context.state.get(clean_key)
    if value is None:
        return {
            "status": "not_found",
            "key": clean_key,
            "message": f"No data found for '{clean_key}' in session memory."
        }
    return {
        "status": "found",
        "key": clean_key,
        "value": value
    }
