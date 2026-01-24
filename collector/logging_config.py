"""
Centralized logging configuration for Aerie.

Provides a consistent logging setup across all entry points (server, classifier, poller).
Logging is configured via environment variables following the AERIE_* pattern.

Environment Variables:
    AERIE_LOG_LEVEL: Minimum log level (DEBUG, INFO, WARNING, ERROR). Default: INFO
    AERIE_LOG_FILE: Optional file path for persistent logs

Logger Hierarchy:
    aerie           - Root logger for the project
    aerie.providers - LLM provider requests/responses
    aerie.classifier - Classification workflow (future)
    aerie.server    - HTTP server events (future)

Usage:
    from logging_config import setup_logging
    setup_logging()  # Call once at startup
"""

import logging
import os
import sys


def setup_logging(log_level: str | None = None) -> None:
    """
    Configure logging for the Aerie application.

    Args:
        log_level: Override log level (DEBUG, INFO, WARNING, ERROR).
                   If None, uses AERIE_LOG_LEVEL env var or defaults to INFO.
    """
    # Determine log level from argument, env var, or default
    level_str = log_level or os.environ.get("AERIE_LOG_LEVEL", "INFO")
    level = getattr(logging, level_str.upper(), logging.INFO)

    # Get optional log file path
    log_file = os.environ.get("AERIE_LOG_FILE")

    # Create root logger for aerie
    root_logger = logging.getLogger("aerie")
    root_logger.setLevel(logging.DEBUG)  # Capture all, filter at handler level

    # Clear any existing handlers (for re-initialization)
    root_logger.handlers.clear()

    # Create formatter with timestamps and module names
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler (if configured)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)  # File always gets DEBUG
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # Prevent propagation to root logger (avoids duplicate output)
    root_logger.propagate = False

    # Log initialization at DEBUG level
    root_logger.debug(f"Logging initialized: level={level_str}, file={log_file or 'none'}")


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger for a specific module.

    Args:
        name: Logger name (will be prefixed with 'aerie.')

    Returns:
        Configured logger instance
    """
    if not name.startswith("aerie."):
        name = f"aerie.{name}"
    return logging.getLogger(name)
