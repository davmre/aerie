"""
Centralized configuration management.

Provides a unified API for accessing configuration with a fallback chain:
1. Environment variables (highest priority)
2. Database settings
3. Default values (lowest priority)

Usage:
    from config import Config

    # Get a single value
    api_key = Config.get("anthropic_api_key")

    # Get with explicit default
    batch_size = Config.get("classification_batch_size", default=10)

    # Get typed values
    enabled = Config.get_bool("classification_enabled", default=True)
    timeout = Config.get_float("http_timeout", default=5.0)
    limit = Config.get_int("fetch_limit", default=50)

    # Access grouped defaults
    from config import ClassificationConfig, PollingConfig
    batch_size = ClassificationConfig.BATCH_SIZE
"""

import os
from pathlib import Path
from typing import Any, ClassVar

# Lazy import to avoid circular dependencies
_settings_module = None


def _get_settings_module():
    """Lazily import database.settings to avoid circular imports."""
    global _settings_module
    if _settings_module is None:
        from database import settings

        _settings_module = settings
    return _settings_module


# =============================================================================
# Environment Variable Mappings
# =============================================================================

# Maps config keys to environment variable names
ENV_VAR_MAP: dict[str, str] = {
    # Database
    "db_path": "AERIE_DB_PATH",
    # Logging
    "log_level": "AERIE_LOG_LEVEL",
    "log_file": "AERIE_LOG_FILE",
    "log_truncate": "AERIE_LOG_TRUNCATE",
    # LLM API Keys
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
    # Bluesky credentials
    "bluesky_handle": "BLUESKY_HANDLE",
    "bluesky_password": "BLUESKY_APP_PASSWORD",
}

# Maps config keys to database setting keys (when different from config key)
DB_SETTING_MAP: dict[str, str] = {
    "bluesky_password": "bluesky_password",  # env uses APP_PASSWORD, db uses password
}


# =============================================================================
# Default Values
# =============================================================================


class ClassificationConfig:
    """Classification worker configuration defaults."""

    ENABLED: bool = True
    BATCH_SIZE: int = 10
    BATCH_TIMEOUT: float = 0.2  # seconds to wait for batch to fill
    DEFAULT_PROVIDER: str = "anthropic"
    DEFAULT_MODEL: str | None = None  # None = use provider's default
    DEFAULT_PROMPT_ID: str = "binary_filter_v1"


class PollingConfig:
    """Bluesky/Twitter polling configuration defaults."""

    BLUESKY_INTERVAL: int = 120  # seconds between polls
    BLUESKY_FETCH_LIMIT: int = 50  # posts per fetch
    THREAD_CONTEXT_DEPTH: int = 50  # max depth for thread context
    RATE_LIMIT_SLEEP: float = 0.1  # seconds between API calls


class ContextConfig:
    """Context updater configuration defaults."""

    DEFAULT_HOURS: int = 48  # hours of posts to include
    OUTPUT_TOKEN_LIMIT: int = 2000  # max tokens for generated context
    INPUT_TOKEN_LIMIT: int = 50000  # max tokens for input posts
    MAX_AUTHOR_POSTS: int = 10  # max posts per author in context
    MAX_SIMILAR_POSTS: int = 5  # max similar posts to include
    CHARS_PER_TOKEN: int = 4  # estimated chars per token


class QueueConfig:
    """Classification queue configuration defaults."""

    MAX_SIZE: int = 1000  # max items in queue
    MAX_AGE_SECONDS: float = 300.0  # drop items older than this
    MIN_BATCH_WAIT: float = 0.05  # minimum wait per job when batching


class HttpConfig:
    """HTTP client configuration defaults."""

    HEALTH_CHECK_TIMEOUT: float = 5.0
    COLLECTION_TIMEOUT: float = 10.0
    STATUS_CHECK_TIMEOUT: float = 5.0


class LLMConfig:
    """LLM provider configuration defaults."""

    # Default models per provider
    ANTHROPIC_DEFAULT_MODEL: str = "claude-sonnet-4-5"
    GEMINI_DEFAULT_MODEL: str = "gemini-2.5-flash-preview-05-20"

    # Max tokens for API calls
    SINGLE_REQUEST_MAX_TOKENS: int = 500
    BATCH_TOKENS_PER_ITEM: int = 100

    # Available models (for UI dropdowns)
    ANTHROPIC_MODELS: ClassVar[list[str]] = [
        "claude-sonnet-4-5",
        "claude-sonnet-4-20250514",
        "claude-haiku-4-20250514",
        "claude-3-5-haiku-20241022",
    ]
    GEMINI_MODELS: ClassVar[list[str]] = [
        "gemini-2.5-flash-preview-05-20",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-flash",
    ]


class LoggingConfig:
    """Logging configuration defaults."""

    DEFAULT_LEVEL: str = "INFO"
    TRUNCATE_ENABLED: bool = True
    TRUNCATE_LENGTH_SHORT: int = 200
    TRUNCATE_LENGTH_LONG: int = 500


# =============================================================================
# Configuration Access API
# =============================================================================


class Config:
    """
    Centralized configuration access with fallback chain.

    Priority order:
    1. Environment variables (if mapped)
    2. Database settings (if available)
    3. Provided default value
    """

    @classmethod
    def get(
        cls,
        key: str,
        default: Any = None,
        *,
        db_path: Path | None = None,
    ) -> Any:
        """
        Get a configuration value with fallback chain.

        Args:
            key: Configuration key (e.g., "anthropic_api_key")
            default: Default value if not found in env or database
            db_path: Optional database path override

        Returns:
            Configuration value from env, database, or default
        """
        # 1. Check environment variable
        env_var = ENV_VAR_MAP.get(key)
        if env_var:
            env_value = os.environ.get(env_var)
            if env_value is not None:
                return env_value

        # 2. Check database settings
        try:
            settings = _get_settings_module()
            db_key = DB_SETTING_MAP.get(key, key)
            if db_path:
                db_value = settings.get_setting(db_key, db_path=db_path)
            else:
                db_value = settings.get_setting(db_key)
            if db_value is not None:
                return db_value
        except Exception:
            # Database might not be initialized or accessible
            pass

        # 3. Return default
        return default

    @classmethod
    def get_int(
        cls,
        key: str,
        default: int = 0,
        *,
        db_path: Path | None = None,
    ) -> int:
        """Get a configuration value as an integer."""
        value = cls.get(key, default=default, db_path=db_path)
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    @classmethod
    def get_float(
        cls,
        key: str,
        default: float = 0.0,
        *,
        db_path: Path | None = None,
    ) -> float:
        """Get a configuration value as a float."""
        value = cls.get(key, default=default, db_path=db_path)
        if isinstance(value, float):
            return value
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    @classmethod
    def get_bool(
        cls,
        key: str,
        default: bool = False,
        *,
        db_path: Path | None = None,
    ) -> bool:
        """Get a configuration value as a boolean."""
        value = cls.get(key, default=default, db_path=db_path)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return bool(value)

    @classmethod
    def get_path(
        cls,
        key: str,
        default: Path | None = None,
        *,
        db_path: Path | None = None,
    ) -> Path | None:
        """Get a configuration value as a Path."""
        value = cls.get(key, default=None, db_path=db_path)
        if value is None:
            return default
        if isinstance(value, Path):
            return value
        return Path(value)


# =============================================================================
# Convenience Functions
# =============================================================================


def get_api_key(provider: str, db_path: Path | None = None) -> str | None:
    """
    Get API key for an LLM provider.

    Checks environment variable first, then database settings.

    Args:
        provider: Provider name ("anthropic" or "gemini")
        db_path: Optional database path override

    Returns:
        API key string or None if not configured
    """
    key_name = f"{provider}_api_key"
    return Config.get(key_name, db_path=db_path)


def get_bluesky_credentials(db_path: Path | None = None) -> tuple[str | None, str | None]:
    """
    Get Bluesky handle and app password.

    Checks environment variables first, then database settings.

    Returns:
        Tuple of (handle, password), either may be None
    """
    handle = Config.get("bluesky_handle", db_path=db_path)
    password = Config.get("bluesky_password", db_path=db_path)
    return handle, password


def get_default_db_path() -> Path:
    """
    Get the default database path.

    Checks AERIE_DB_PATH environment variable, falls back to
    tweets.db in the project root.
    """
    env_path = os.environ.get("AERIE_DB_PATH")
    if env_path:
        return Path(env_path)
    # Default: tweets.db in parent of collector directory
    return Path(__file__).parent.parent / "tweets.db"
