"""
Validation utilities for API endpoints.

Provides reusable validation functions for request data.
"""


def validate_id_format(id_string: str, entity_name: str = "ID") -> tuple[bool, str | None]:
    """
    Validate that an ID contains only alphanumeric characters and underscores.

    Args:
        id_string: The ID string to validate
        entity_name: Name of the entity for error messages (e.g., "Mode ID", "Prompt ID")

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is None.

    Examples:
        >>> validate_id_format("my_mode_1", "Mode ID")
        (True, None)
        >>> validate_id_format("my-mode", "Mode ID")
        (False, "Mode ID must contain only letters, numbers, and underscores")
    """
    if not id_string:
        return False, f"{entity_name} is required"

    if not id_string.replace("_", "").isalnum():
        return False, f"{entity_name} must contain only letters, numbers, and underscores"

    return True, None


def require_fields(data: dict, *fields: str) -> tuple[bool, str | None]:
    """
    Check that required fields are present and non-empty in a dict.

    Args:
        data: Dictionary to check
        *fields: Field names that must be present and non-empty

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is None.

    Examples:
        >>> require_fields({"id": "test", "name": "Test"}, "id", "name")
        (True, None)
        >>> require_fields({"id": "test"}, "id", "name")
        (False, "Missing required field: name")
    """
    for field in fields:
        if not data.get(field):
            return False, f"Missing required field: {field}"
    return True, None


def validate_provider(provider_name: str) -> tuple[bool, str | None]:
    """
    Validate that a provider name is recognized.

    Args:
        provider_name: Name of the LLM provider to validate

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is None.
    """
    # Import here to avoid circular imports
    from providers import PROVIDERS

    if provider_name not in PROVIDERS:
        available = ", ".join(PROVIDERS.keys())
        return False, f"Unknown provider: {provider_name}. Available: {available}"

    return True, None


def validate_extractor(extractor_name: str) -> tuple[bool, str | None]:
    """
    Validate that an extractor name is recognized.

    Args:
        extractor_name: Name of the extractor to validate

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is None.
    """
    from extractors import EXTRACTOR_SCHEMAS

    if extractor_name not in EXTRACTOR_SCHEMAS:
        return False, f"Unknown extractor: {extractor_name}"

    return True, None


def validate_prefilter(prefilter_name: str | None) -> tuple[bool, str | None]:
    """
    Validate that a prefilter name is recognized (if provided).

    Args:
        prefilter_name: Name of the prefilter to validate, or None

    Returns:
        Tuple of (is_valid, error_message). If valid or None, error_message is None.
    """
    if not prefilter_name:
        return True, None

    from prefilters import PREFILTER_SCHEMAS

    if prefilter_name not in PREFILTER_SCHEMAS:
        return False, f"Unknown prefilter: {prefilter_name}"

    return True, None
