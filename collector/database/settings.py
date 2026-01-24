"""
Settings storage operations.

Provides key-value storage for application settings.
"""

from datetime import datetime
from pathlib import Path

from database.core import DEFAULT_DB_PATH, transaction


def get_setting(key: str, db_path: Path = DEFAULT_DB_PATH) -> str | None:
    """Get a setting value by key."""
    with transaction(db_path) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str | None, db_path: Path = DEFAULT_DB_PATH) -> None:
    """Set a setting value."""
    with transaction(db_path) as conn:
        if value is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?
                """,
                (key, value, datetime.utcnow().isoformat(), value, datetime.utcnow().isoformat()),
            )


def get_all_settings(db_path: Path = DEFAULT_DB_PATH) -> dict[str, str]:
    """Get all settings as a dictionary."""
    with transaction(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}


def delete_setting(key: str, db_path: Path = DEFAULT_DB_PATH) -> None:
    """Delete a setting."""
    with transaction(db_path) as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
