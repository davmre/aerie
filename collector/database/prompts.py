"""
Prompt and LLM response operations.

Provides CRUD for prompts and caching for LLM responses.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from database.core import DEFAULT_DB_PATH, transaction

# =============================================================================
# Prompt CRUD
# =============================================================================


def create_prompt(
    prompt_id: str,
    prompt_text: str,
    response_schema: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Create or update a prompt definition."""
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO prompts (id, prompt_text, response_schema, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (prompt_id, prompt_text, response_schema, datetime.utcnow().isoformat()),
        )


def get_prompt(prompt_id: str, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    """Get a prompt by ID."""
    with transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
        return dict(row) if row else None


def list_prompts(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """List all prompts."""
    with transaction(db_path) as conn:
        rows = conn.execute("SELECT * FROM prompts ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]


def update_prompt(
    prompt_id: str,
    prompt_text: str | None = None,
    response_schema: str | None = None,
    clear_response_schema: bool = False,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    """
    Update an existing prompt. Only updates non-None fields.
    Use clear_response_schema=True to set response_schema to NULL.
    Returns True if prompt was found and updated, False otherwise.
    """
    with transaction(db_path) as conn:
        # Check prompt exists
        existing = conn.execute("SELECT id FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
        if not existing:
            return False

        updates = []
        params: list[str | None] = []

        if prompt_text is not None:
            updates.append("prompt_text = ?")
            params.append(prompt_text)
        if response_schema is not None or clear_response_schema:
            updates.append("response_schema = ?")
            params.append(response_schema)

        if not updates:
            return True  # Nothing to update

        params.append(prompt_id)
        conn.execute(
            f"UPDATE prompts SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        return True


def delete_prompt(prompt_id: str, force: bool = False, db_path: Path = DEFAULT_DB_PATH) -> dict:
    """
    Delete a prompt.
    Returns {"status": "ok"} on success.
    Returns {"error": "...", "mode_count": N} if prompt is used by modes and force=False.
    If force=True, deletion proceeds (modes will have dangling references).
    """
    with transaction(db_path) as conn:
        # Check for dependent modes
        mode_count = conn.execute(
            "SELECT COUNT(*) FROM modes WHERE prompt_id = ?", (prompt_id,)
        ).fetchone()[0]

        if mode_count > 0 and not force:
            return {
                "error": f"Prompt is used by {mode_count} mode(s). Use force=True to delete anyway.",
                "mode_count": mode_count,
            }

        # Delete the prompt
        result = conn.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
        if result.rowcount == 0:
            return {"error": "Prompt not found"}

        return {"status": "ok"}


def count_modes_using_prompt(prompt_id: str, db_path: Path = DEFAULT_DB_PATH) -> int:
    """Count how many modes reference this prompt."""
    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM modes WHERE prompt_id = ?", (prompt_id,)
        ).fetchone()
        return row[0]


# =============================================================================
# Prompt Responses (LLM output cache)
# =============================================================================


def store_prompt_response(
    tweet_id: str,
    prompt_id: str,
    model: str,
    response: dict | Any,
    db_path: Path = DEFAULT_DB_PATH,
    classification_batch_id: str | None = None,
    context_id: str | None = None,
) -> None:
    """Store an LLM response for a tweet/prompt pair."""
    response_json = json.dumps(response) if not isinstance(response, str) else response
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO prompt_responses
            (tweet_id, prompt_id, model, response_json, created_at, classification_batch_id, context_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tweet_id,
                prompt_id,
                model,
                response_json,
                datetime.utcnow().isoformat(),
                classification_batch_id,
                context_id,
            ),
        )


def get_prompt_response(
    tweet_id: str,
    prompt_id: str,
    model: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict | None:
    """
    Get a cached LLM response.
    If model is None, returns the most recent response for any model.
    """
    with transaction(db_path) as conn:
        if model:
            row = conn.execute(
                """
                SELECT * FROM prompt_responses
                WHERE tweet_id = ? AND prompt_id = ? AND model = ?
                """,
                (tweet_id, prompt_id, model),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM prompt_responses
                WHERE tweet_id = ? AND prompt_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (tweet_id, prompt_id),
            ).fetchone()

        if row:
            result = dict(row)
            result["response"] = json.loads(result["response_json"])
            return result
        return None


def get_prompt_responses_batch(
    tweet_ids: list[str],
    prompt_id: str,
    model: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, dict]:
    """
    Get cached LLM responses for multiple tweets.
    Returns dict mapping tweet_id -> response dict.
    """
    if not tweet_ids:
        return {}

    with transaction(db_path) as conn:
        placeholders = ",".join("?" * len(tweet_ids))
        if model:
            rows = conn.execute(
                f"""
                SELECT * FROM prompt_responses
                WHERE tweet_id IN ({placeholders}) AND prompt_id = ? AND model = ?
                """,
                (*tweet_ids, prompt_id, model),
            ).fetchall()
        else:
            # Get most recent response per tweet
            rows = conn.execute(
                f"""
                SELECT pr.* FROM prompt_responses pr
                INNER JOIN (
                    SELECT tweet_id, MAX(created_at) as max_created
                    FROM prompt_responses
                    WHERE tweet_id IN ({placeholders}) AND prompt_id = ?
                    GROUP BY tweet_id
                ) latest ON pr.tweet_id = latest.tweet_id
                    AND pr.created_at = latest.max_created
                    AND pr.prompt_id = ?
                """,
                (*tweet_ids, prompt_id, prompt_id),
            ).fetchall()

        result = {}
        for row in rows:
            r = dict(row)
            r["response"] = json.loads(r["response_json"])
            result[r["tweet_id"]] = r
        return result


def get_tweets_without_response(
    prompt_id: str,
    model: str | None = None,
    limit: int = 100,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """Get tweets that don't have a response for the given prompt."""
    with transaction(db_path) as conn:
        if model:
            rows = conn.execute(
                """
                SELECT t.* FROM tweets t
                LEFT JOIN prompt_responses pr
                    ON t.id = pr.tweet_id
                    AND pr.prompt_id = ?
                    AND pr.model = ?
                WHERE pr.tweet_id IS NULL
                ORDER BY t.captured_at DESC
                LIMIT ?
                """,
                (prompt_id, model, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT t.* FROM tweets t
                LEFT JOIN prompt_responses pr
                    ON t.id = pr.tweet_id
                    AND pr.prompt_id = ?
                WHERE pr.tweet_id IS NULL
                ORDER BY t.captured_at DESC
                LIMIT ?
                """,
                (prompt_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]


# =============================================================================
# Stats
# =============================================================================


def get_stats(prompt_id: str = "binary_filter_v1", db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Get database statistics based on prompt responses."""
    with transaction(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]

        # Count tweets with responses
        approved = conn.execute(
            """
            SELECT COUNT(*) FROM prompt_responses
            WHERE prompt_id = ? AND json_extract(response_json, '$.approved') = 1
            """,
            (prompt_id,),
        ).fetchone()[0]

        filtered = conn.execute(
            """
            SELECT COUNT(*) FROM prompt_responses
            WHERE prompt_id = ? AND json_extract(response_json, '$.approved') = 0
            """,
            (prompt_id,),
        ).fetchone()[0]

        # Pending = total - classified
        pending = total - approved - filtered

        return {
            "total": total,
            "pending": pending,
            "approved": approved,
            "filtered": filtered,
        }
