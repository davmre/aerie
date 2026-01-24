"""
Configuration parsing utilities.

Shared helper functions for parsing JSON configuration and checking responses.
"""

import json
from typing import Any


def parse_json_config(config_json: str | None) -> dict[str, Any] | None:
    """
    Parse a JSON config string, returning None if empty or invalid.

    This handles the common pattern of optional JSON config fields in database
    records that may be None, empty string, or invalid JSON.

    Args:
        config_json: JSON string to parse, or None

    Returns:
        Parsed dict if valid JSON, None otherwise
    """
    if not config_json:
        return None
    try:
        result = json.loads(config_json)
        if isinstance(result, dict):
            return result
        return None
    except (json.JSONDecodeError, TypeError):
        return None


def is_error_response(response: Any) -> bool:
    """
    Check if an LLM response indicates an error.

    Error responses from providers have a "_error" key with an error type string.

    Args:
        response: Response dict from LLM provider

    Returns:
        True if this is an error response, False otherwise
    """
    return isinstance(response, dict) and response.get("_error") is not None
