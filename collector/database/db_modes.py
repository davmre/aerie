"""
Mode and decision operations.

Provides CRUD for modes, human labels, and decision caching.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from database.core import DEFAULT_DB_PATH, transaction

# =============================================================================
# Mode CRUD
# =============================================================================


def create_mode(
    mode_id: str,
    name: str,
    prompt_id: str,
    extractor: str,
    prefilter: str | None = None,
    extractor_config: dict | None = None,
    prefilter_config: dict | None = None,
    description: str | None = None,
    provider: str = "anthropic",
    model_name: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Create or update a mode definition."""
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO modes
            (id, name, prompt_id, prefilter, extractor, extractor_config, prefilter_config, description, provider, model_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mode_id,
                name,
                prompt_id,
                prefilter,
                extractor,
                json.dumps(extractor_config) if extractor_config else None,
                json.dumps(prefilter_config) if prefilter_config else None,
                description,
                provider,
                model_name,
            ),
        )


def get_mode(mode_id: str, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    """Get a mode by ID."""
    with transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM modes WHERE id = ?", (mode_id,)).fetchone()
        return dict(row) if row else None


def list_modes(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """List all modes."""
    with transaction(db_path) as conn:
        rows = conn.execute("SELECT * FROM modes ORDER BY name").fetchall()
        return [dict(row) for row in rows]


def update_mode(
    mode_id: str,
    name: str | None = None,
    prompt_id: str | None = None,
    extractor: str | None = None,
    prefilter: str | None = None,
    extractor_config: dict | None = None,
    prefilter_config: dict | None = None,
    description: str | None = None,
    provider: str | None = None,
    model_name: str | None = None,
    clear_prefilter: bool = False,
    clear_extractor_config: bool = False,
    clear_prefilter_config: bool = False,
    clear_model_name: bool = False,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    """
    Update an existing mode. Only updates non-None fields.
    Use clear_* flags to explicitly set fields to NULL.
    Returns True if mode was found and updated, False otherwise.
    """
    with transaction(db_path) as conn:
        # Check mode exists
        existing = conn.execute("SELECT id FROM modes WHERE id = ?", (mode_id,)).fetchone()
        if not existing:
            return False

        updates = []
        params: list[Any] = []

        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if prompt_id is not None:
            updates.append("prompt_id = ?")
            params.append(prompt_id)
        if extractor is not None:
            updates.append("extractor = ?")
            params.append(extractor)
        if prefilter is not None or clear_prefilter:
            updates.append("prefilter = ?")
            params.append(prefilter)
        if extractor_config is not None or clear_extractor_config:
            updates.append("extractor_config = ?")
            params.append(json.dumps(extractor_config) if extractor_config else None)
        if prefilter_config is not None or clear_prefilter_config:
            updates.append("prefilter_config = ?")
            params.append(json.dumps(prefilter_config) if prefilter_config else None)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if provider is not None:
            updates.append("provider = ?")
            params.append(provider)
        if model_name is not None or clear_model_name:
            updates.append("model_name = ?")
            params.append(model_name)

        if not updates:
            return True  # Nothing to update

        params.append(mode_id)
        conn.execute(
            f"UPDATE modes SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        return True


def delete_mode(mode_id: str, force: bool = False, db_path: Path = DEFAULT_DB_PATH) -> dict:
    """
    Delete a mode.
    Returns {"status": "ok"} on success.
    Returns {"error": "...", "label_count": N} if mode has human labels and force=False.
    If force=True, also deletes associated human_labels.
    """
    with transaction(db_path) as conn:
        # Check for human labels
        label_count = conn.execute(
            "SELECT COUNT(*) FROM human_labels WHERE mode_id = ?", (mode_id,)
        ).fetchone()[0]

        if label_count > 0 and not force:
            return {
                "error": f"Mode has {label_count} human labels. Use force=True to delete.",
                "label_count": label_count,
            }

        # Delete labels if forcing
        if label_count > 0:
            conn.execute("DELETE FROM human_labels WHERE mode_id = ?", (mode_id,))

        # Delete the mode
        result = conn.execute("DELETE FROM modes WHERE id = ?", (mode_id,))
        if result.rowcount == 0:
            return {"error": "Mode not found"}

        return {"status": "ok", "deleted_labels": label_count if force else 0}


def get_mode_label_counts(db_path: Path = DEFAULT_DB_PATH) -> dict[str, int]:
    """Get human label counts for all modes."""
    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT mode_id, COUNT(*) as count FROM human_labels GROUP BY mode_id"
        ).fetchall()
        return {row["mode_id"]: row["count"] for row in rows}


# =============================================================================
# Human Labels
# =============================================================================


def add_human_label(
    tweet_id: str,
    mode_id: str,
    should_show: bool,
    notes: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Add a human label for evaluation."""
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO human_labels
            (tweet_id, mode_id, should_show, notes, labeled_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tweet_id, mode_id, 1 if should_show else 0, notes, datetime.utcnow().isoformat()),
        )


def get_human_labels(
    mode_id: str | None = None,
    limit: int = 1000,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """Get human labels, optionally filtered by mode."""
    with transaction(db_path) as conn:
        if mode_id:
            rows = conn.execute(
                """
                SELECT hl.*, t.text, t.author_username
                FROM human_labels hl
                JOIN tweets t ON hl.tweet_id = t.id
                WHERE hl.mode_id = ?
                ORDER BY hl.labeled_at DESC
                LIMIT ?
                """,
                (mode_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT hl.*, t.text, t.author_username
                FROM human_labels hl
                JOIN tweets t ON hl.tweet_id = t.id
                ORDER BY hl.labeled_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


def get_unlabeled_tweets(
    mode_id: str,
    limit: int = 100,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """Get tweets that don't have a human label for the given mode."""
    with transaction(db_path) as conn:
        rows = conn.execute(
            """
            SELECT t.* FROM tweets t
            LEFT JOIN human_labels hl
                ON t.id = hl.tweet_id AND hl.mode_id = ?
            WHERE hl.tweet_id IS NULL
            ORDER BY t.captured_at DESC
            LIMIT ?
            """,
            (mode_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


# =============================================================================
# Mode Decisions Cache
# =============================================================================


def store_mode_decision(
    tweet_id: str,
    mode_id: str,
    decision: str,
    source: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Store a cached decision for a tweet/mode pair."""
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO mode_decisions
            (tweet_id, mode_id, decision, source, computed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tweet_id, mode_id, decision, source, datetime.utcnow().isoformat()),
        )


def store_mode_decisions_batch(
    decisions: list[dict],
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    """
    Store multiple decisions at once.
    Each dict should have: tweet_id, mode_id, decision, source
    Returns the number of decisions stored.
    """
    if not decisions:
        return 0

    with transaction(db_path) as conn:
        now = datetime.utcnow().isoformat()
        conn.executemany(
            """
            INSERT OR REPLACE INTO mode_decisions
            (tweet_id, mode_id, decision, source, computed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(d["tweet_id"], d["mode_id"], d["decision"], d["source"], now) for d in decisions],
        )
        return len(decisions)


def get_mode_decision(
    tweet_id: str,
    mode_id: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> str | None:
    """Get cached decision for a tweet/mode pair. Returns 'approved', 'filtered', or None."""
    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT decision FROM mode_decisions WHERE tweet_id = ? AND mode_id = ?",
            (tweet_id, mode_id),
        ).fetchone()
        return row["decision"] if row else None


def get_mode_decisions_batch(
    tweet_ids: list[str],
    mode_id: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, str]:
    """Get cached decisions for multiple tweets. Returns dict of tweet_id -> decision."""
    if not tweet_ids:
        return {}

    with transaction(db_path) as conn:
        placeholders = ",".join("?" * len(tweet_ids))
        rows = conn.execute(
            f"""
            SELECT tweet_id, decision FROM mode_decisions
            WHERE tweet_id IN ({placeholders}) AND mode_id = ?
            """,
            (*tweet_ids, mode_id),
        ).fetchall()
        return {row["tweet_id"]: row["decision"] for row in rows}


def get_cached_decision_stats(
    mode_id: str,
    platform: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict:
    """Get stats from cached decisions for a mode, optionally filtered by platform."""
    with transaction(db_path) as conn:
        # Build platform filter
        platform_clause = ""
        platform_params: list[str] = []
        if platform:
            platform_clause = "WHERE platform = ?"
            platform_params = [platform]

        total = conn.execute(
            f"SELECT COUNT(*) FROM tweets {platform_clause}",
            platform_params,
        ).fetchone()[0]

        # Build join query with platform filter
        if platform:
            join_platform_clause = "JOIN tweets t ON md.tweet_id = t.id WHERE t.platform = ? AND"
            approved_params: list[str] = [platform, mode_id]
            filtered_params: list[str] = [platform, mode_id]
        else:
            join_platform_clause = "WHERE"
            approved_params = [mode_id]
            filtered_params = [mode_id]

        approved = conn.execute(
            f"SELECT COUNT(*) FROM mode_decisions md {join_platform_clause} md.mode_id = ? AND md.decision = 'approved'",
            approved_params,
        ).fetchone()[0]

        filtered = conn.execute(
            f"SELECT COUNT(*) FROM mode_decisions md {join_platform_clause} md.mode_id = ? AND md.decision = 'filtered'",
            filtered_params,
        ).fetchone()[0]

        pending = total - approved - filtered

        return {
            "total": total,
            "approved": approved,
            "filtered": filtered,
            "pending": pending,
        }


def invalidate_mode_decisions(
    mode_id: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    """
    Delete all cached decisions for a mode.
    Call this when mode config changes.
    Returns the number of decisions deleted.
    """
    with transaction(db_path) as conn:
        result = conn.execute(
            "DELETE FROM mode_decisions WHERE mode_id = ?",
            (mode_id,),
        )
        return result.rowcount


def invalidate_tweet_decisions(
    tweet_id: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    """
    Delete all cached decisions for a tweet.
    Call this when a tweet is reclassified.
    Returns the number of decisions deleted.
    """
    with transaction(db_path) as conn:
        result = conn.execute(
            "DELETE FROM mode_decisions WHERE tweet_id = ?",
            (tweet_id,),
        )
        return result.rowcount


def get_tweets_without_decision(
    mode_id: str,
    limit: int = 1000,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """Get tweets that don't have a cached decision for the given mode."""
    with transaction(db_path) as conn:
        rows = conn.execute(
            """
            SELECT t.* FROM tweets t
            LEFT JOIN mode_decisions md
                ON t.id = md.tweet_id AND md.mode_id = ?
            WHERE md.tweet_id IS NULL
            ORDER BY t.captured_at DESC
            LIMIT ?
            """,
            (mode_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def get_tweets_without_decision_filtered(
    mode_id: str,
    limit: int = 100,
    max_age_hours: int | None = None,
    before: str | None = None,
    platform: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """
    Get tweets that don't have a cached decision for the given mode,
    with optional time and platform filtering.

    Args:
        mode_id: Mode ID to check decisions for
        limit: Maximum number of tweets to return
        max_age_hours: Only return tweets from the last N hours (by created_at)
        before: Only return tweets created before this ISO timestamp
        platform: Filter by platform ('twitter', 'bluesky')
        db_path: Database path

    Returns:
        List of tweet dicts without decisions, ordered by created_at DESC
    """
    with transaction(db_path) as conn:
        query = """
            SELECT t.* FROM tweets t
            LEFT JOIN mode_decisions md
                ON t.id = md.tweet_id AND md.mode_id = ?
            WHERE md.tweet_id IS NULL
        """
        params: list[Any] = [mode_id]

        if max_age_hours is not None:
            # SQLite datetime arithmetic: datetime('now', '-N hours')
            query += " AND t.created_at >= datetime('now', ?)"
            params.append(f"-{max_age_hours} hours")

        if before is not None:
            query += " AND t.created_at < ?"
            params.append(before)

        if platform is not None:
            query += " AND t.platform = ?"
            params.append(platform)

        query += " ORDER BY t.created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def get_recently_classified_tweets(
    mode_id: str,
    since: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[str]:
    """
    Get tweet IDs that were classified (got a decision) after a given timestamp.

    Args:
        mode_id: Mode ID to check decisions for
        since: ISO timestamp - only return tweets classified after this time
        db_path: Database path

    Returns:
        List of tweet IDs that were classified since the given timestamp
    """
    with transaction(db_path) as conn:
        rows = conn.execute(
            """
            SELECT tweet_id FROM mode_decisions
            WHERE mode_id = ? AND computed_at > ?
            ORDER BY computed_at DESC
            """,
            (mode_id, since),
        ).fetchall()
        return [row["tweet_id"] for row in rows]
