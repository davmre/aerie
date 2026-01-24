"""
Context management operations.

Provides operations for situational context snapshots used in classification.
"""

import json
from datetime import datetime
from pathlib import Path

from database.core import DEFAULT_DB_PATH, transaction
from database.settings import get_setting, set_setting


def create_context(
    text: str,
    token_count: int | None = None,
    metadata: dict | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> str:
    """
    Create a new context snapshot.

    Args:
        text: The freeform context text
        token_count: Estimated token count (for budget tracking)
        metadata: Optional JSON metadata (e.g., {source: "auto"|"manual"})
        db_path: Database path

    Returns:
        The generated context ID (e.g., "ctx_2026-01-23T10:30:00Z")
    """
    now = datetime.utcnow().isoformat() + "Z"
    context_id = f"ctx_{now}"
    metadata_json = json.dumps(metadata) if metadata else None

    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT INTO contexts (id, text, created_at, token_count, metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
            (context_id, text, now, token_count, metadata_json),
        )

    return context_id


def get_context(context_id: str, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    """
    Get a context by ID.

    Returns:
        Context dict with id, text, created_at, token_count, metadata (parsed as dict),
        or None if not found.
    """
    with transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM contexts WHERE id = ?", (context_id,)).fetchone()
        if not row:
            return None

        result = dict(row)
        if result.get("metadata"):
            result["metadata"] = json.loads(result["metadata"])
        return result


def get_current_context(db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    """
    Get the current active context.

    Returns:
        The current context dict, or None if no context is set.
    """
    context_id = get_setting("current_context_id", db_path=db_path)
    if not context_id:
        return None
    return get_context(context_id, db_path=db_path)


def set_current_context(context_id: str | None, db_path: Path = DEFAULT_DB_PATH) -> None:
    """
    Set the current context ID.

    Args:
        context_id: The context ID to set as current, or None to clear
        db_path: Database path
    """
    set_setting("current_context_id", context_id, db_path=db_path)


def list_contexts(
    limit: int = 20,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """
    List contexts, most recent first.

    Args:
        limit: Maximum number of contexts to return
        db_path: Database path

    Returns:
        List of context dicts with parsed metadata
    """
    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM contexts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

        results = []
        for row in rows:
            context = dict(row)
            if context.get("metadata"):
                context["metadata"] = json.loads(context["metadata"])
            results.append(context)
        return results


def update_context_text(
    context_id: str,
    text: str,
    token_count: int | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    """
    Update the text of an existing context (for manual edits).

    Args:
        context_id: The context ID to update
        text: New context text
        token_count: Updated token count (optional)
        db_path: Database path

    Returns:
        True if context was found and updated, False otherwise.
    """
    with transaction(db_path) as conn:
        if token_count is not None:
            result = conn.execute(
                "UPDATE contexts SET text = ?, token_count = ? WHERE id = ?",
                (text, token_count, context_id),
            )
        else:
            result = conn.execute(
                "UPDATE contexts SET text = ? WHERE id = ?",
                (text, context_id),
            )
        return result.rowcount > 0
