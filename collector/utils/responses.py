"""
API response utilities for Flask endpoints.

Provides standardized response helpers for consistent API formatting.
"""

from flask import Response, jsonify


def api_error(message: str, status: int = 400) -> tuple[Response, int]:
    """
    Create a standardized error response.

    Args:
        message: Error message to include in response
        status: HTTP status code (default: 400 Bad Request)

    Returns:
        Tuple of (Flask Response, status code)

    Example:
        >>> return api_error("Mode not found", 404)
        # Returns: ({"error": "Mode not found"}, 404)
    """
    return jsonify({"error": message}), status


def api_success(data: dict | list | None = None, status: int = 200) -> tuple[Response, int]:
    """
    Create a standardized success response.

    Args:
        data: Response data (dict or list). If None, returns {"status": "ok"}
        status: HTTP status code (default: 200 OK)

    Returns:
        Tuple of (Flask Response, status code)

    Examples:
        >>> return api_success()
        # Returns: ({"status": "ok"}, 200)

        >>> return api_success({"id": "new_mode", "name": "New Mode"})
        # Returns: ({"id": "new_mode", "name": "New Mode"}, 200)
    """
    if data is None:
        return jsonify({"status": "ok"}), status
    return jsonify(data), status


def api_created(data: dict) -> tuple[Response, int]:
    """
    Create a 201 Created response.

    Args:
        data: Response data containing created resource

    Returns:
        Tuple of (Flask Response, 201)
    """
    return jsonify(data), 201


def api_not_found(entity: str = "Resource") -> tuple[Response, int]:
    """
    Create a 404 Not Found response.

    Args:
        entity: Name of the entity that wasn't found

    Returns:
        Tuple of (Flask Response, 404)

    Example:
        >>> return api_not_found("Mode")
        # Returns: ({"error": "Mode not found"}, 404)
    """
    return jsonify({"error": f"{entity} not found"}), 404


def api_conflict(message: str) -> tuple[Response, int]:
    """
    Create a 409 Conflict response.

    Args:
        message: Conflict description

    Returns:
        Tuple of (Flask Response, 409)

    Example:
        >>> return api_conflict("Mode already exists: default")
        # Returns: ({"error": "Mode already exists: default"}, 409)
    """
    return jsonify({"error": message}), 409
