"""SQLite database operations for tweet storage and classification."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(__file__).parent.parent / "tweets.db"


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Create a database connection with optimal settings."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction(db_path: Path = DEFAULT_DB_PATH):
    """Context manager for database transactions."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database(db_path: Path = DEFAULT_DB_PATH):
    """Initialize the database schema."""
    with transaction(db_path) as conn:
        conn.executescript("""
            -- Core tweet data
            CREATE TABLE IF NOT EXISTS tweets (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                created_at TEXT,
                captured_at TEXT NOT NULL,

                -- Author info (denormalized for simplicity)
                author_id TEXT,
                author_username TEXT,
                author_display_name TEXT,
                author_verified INTEGER DEFAULT 0,

                -- Engagement metrics
                retweet_count INTEGER DEFAULT 0,
                reply_count INTEGER DEFAULT 0,
                like_count INTEGER DEFAULT 0,
                quote_count INTEGER DEFAULT 0,

                -- Threading relationships
                reply_to_tweet_id TEXT,
                reply_to_user_id TEXT,
                reply_to_username TEXT,
                is_retweet INTEGER DEFAULT 0,
                is_quote INTEGER DEFAULT 0,
                quoted_tweet_id TEXT,

                -- Structured data stored as JSON
                media_json TEXT,
                urls_json TEXT,
                hashtags_json TEXT,
                mentions_json TEXT,

                -- Classification results (filled in later by the classifier)
                classification_status TEXT DEFAULT 'pending',
                classification_result INTEGER,  -- 1 = approved, 0 = filtered
                classification_reason TEXT,
                classified_at TEXT
            );

            -- Indexes for common queries
            CREATE INDEX IF NOT EXISTS idx_tweets_captured_at ON tweets(captured_at);
            CREATE INDEX IF NOT EXISTS idx_tweets_created_at ON tweets(created_at);
            CREATE INDEX IF NOT EXISTS idx_tweets_author ON tweets(author_username);
            CREATE INDEX IF NOT EXISTS idx_tweets_classification ON tweets(classification_status);
            CREATE INDEX IF NOT EXISTS idx_tweets_reply_to ON tweets(reply_to_tweet_id);

            -- Track capture sessions for debugging/analytics
            CREATE TABLE IF NOT EXISTS capture_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                source_url TEXT,
                tweet_count INTEGER DEFAULT 0
            );

            -- Versioned prompt definitions
            CREATE TABLE IF NOT EXISTS prompts (
                id TEXT PRIMARY KEY,
                prompt_text TEXT NOT NULL,
                response_schema TEXT,
                created_at TEXT NOT NULL
            );

            -- Raw LLM responses (cached)
            CREATE TABLE IF NOT EXISTS prompt_responses (
                tweet_id TEXT NOT NULL,
                prompt_id TEXT NOT NULL,
                model TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (tweet_id, prompt_id, model),
                FOREIGN KEY (tweet_id) REFERENCES tweets(id),
                FOREIGN KEY (prompt_id) REFERENCES prompts(id)
            );

            -- Mode definitions (prompt + extractor)
            CREATE TABLE IF NOT EXISTS modes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                prompt_id TEXT NOT NULL,
                prefilter TEXT,
                extractor TEXT NOT NULL,
                description TEXT,
                FOREIGN KEY (prompt_id) REFERENCES prompts(id)
            );

            -- Human labels for evaluation
            CREATE TABLE IF NOT EXISTS human_labels (
                tweet_id TEXT NOT NULL,
                mode_id TEXT NOT NULL,
                should_show INTEGER NOT NULL,
                notes TEXT,
                labeled_at TEXT NOT NULL,
                PRIMARY KEY (tweet_id, mode_id),
                FOREIGN KEY (tweet_id) REFERENCES tweets(id),
                FOREIGN KEY (mode_id) REFERENCES modes(id)
            );

            -- Indexes for new tables
            CREATE INDEX IF NOT EXISTS idx_prompt_responses_tweet ON prompt_responses(tweet_id);
            CREATE INDEX IF NOT EXISTS idx_prompt_responses_prompt ON prompt_responses(prompt_id);
            CREATE INDEX IF NOT EXISTS idx_human_labels_mode ON human_labels(mode_id);
        """)


def store_tweets(tweets: list[dict], db_path: Path = DEFAULT_DB_PATH) -> dict:
    """
    Store tweets in the database, deduplicating by ID.
    Returns stats about the operation.
    """
    if not tweets:
        return {"inserted": 0, "duplicates": 0}

    inserted = 0
    duplicates = 0

    with transaction(db_path) as conn:
        for tweet in tweets:
            try:
                conn.execute(
                    """
                    INSERT INTO tweets (
                        id, text, created_at, captured_at,
                        author_id, author_username, author_display_name, author_verified,
                        retweet_count, reply_count, like_count, quote_count,
                        reply_to_tweet_id, reply_to_user_id, reply_to_username,
                        is_retweet, is_quote, quoted_tweet_id,
                        media_json, urls_json, hashtags_json, mentions_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        tweet["id"],
                        tweet["text"],
                        tweet.get("created_at"),
                        tweet.get("captured_at", datetime.utcnow().isoformat()),
                        tweet.get("author", {}).get("id"),
                        tweet.get("author", {}).get("username"),
                        tweet.get("author", {}).get("display_name"),
                        1 if tweet.get("author", {}).get("verified") else 0,
                        tweet.get("metrics", {}).get("retweet_count", 0),
                        tweet.get("metrics", {}).get("reply_count", 0),
                        tweet.get("metrics", {}).get("like_count", 0),
                        tweet.get("metrics", {}).get("quote_count", 0),
                        tweet.get("reply_to", {}).get("tweet_id"),
                        tweet.get("reply_to", {}).get("user_id"),
                        tweet.get("reply_to", {}).get("username"),
                        1 if tweet.get("is_retweet") else 0,
                        1 if tweet.get("is_quote") else 0,
                        tweet.get("quoted_tweet_id"),
                        json.dumps(tweet.get("media", [])),
                        json.dumps(tweet.get("urls", [])),
                        json.dumps(tweet.get("hashtags", [])),
                        json.dumps(tweet.get("mentions", [])),
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                # Duplicate tweet ID - this is expected and fine
                duplicates += 1

    return {"inserted": inserted, "duplicates": duplicates}


def get_pending_tweets(limit: int = 100, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Get tweets that haven't been classified yet."""
    with transaction(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM tweets
            WHERE classification_status = 'pending'
            ORDER BY captured_at DESC
            LIMIT ?
        """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_approved_tweets(
    limit: int = 100, offset: int = 0, db_path: Path = DEFAULT_DB_PATH
) -> list[dict]:
    """Get tweets that passed classification."""
    with transaction(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM tweets
            WHERE classification_result = 1
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """,
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]


def update_classification(
    tweet_id: str, approved: bool, reason: str = None, db_path: Path = DEFAULT_DB_PATH
):
    """Update a tweet's classification status."""
    with transaction(db_path) as conn:
        conn.execute(
            """
            UPDATE tweets SET
                classification_status = 'completed',
                classification_result = ?,
                classification_reason = ?,
                classified_at = ?
            WHERE id = ?
        """,
            (1 if approved else 0, reason, datetime.utcnow().isoformat(), tweet_id),
        )


def approve_all_pending(db_path: Path = DEFAULT_DB_PATH) -> int:
    """Approve all pending tweets. Returns count of approved tweets."""
    with transaction(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE tweets SET
                classification_status = 'completed',
                classification_result = 1,
                classification_reason = 'auto-approved',
                classified_at = ?
            WHERE classification_status = 'pending'
        """,
            (datetime.utcnow().isoformat(),),
        )
        return cursor.rowcount


def get_all_classified_ids(db_path: Path = DEFAULT_DB_PATH) -> dict[str, str]:
    """Get all classified tweet IDs with their status. For cache pre-population."""
    with transaction(db_path) as conn:
        rows = conn.execute("""
            SELECT id, classification_result
            FROM tweets
            WHERE classification_status = 'completed'
        """).fetchall()

        return {
            row["id"]: "approved" if row["classification_result"] == 1 else "filtered"
            for row in rows
        }


def check_tweet_statuses(
    tweet_ids: list[str], db_path: Path = DEFAULT_DB_PATH
) -> dict[str, str]:
    """
    Check the classification status of multiple tweets.
    Returns a dict mapping tweet_id -> status ('approved', 'filtered', 'pending', or 'unknown').
    """
    if not tweet_ids:
        return {}

    with transaction(db_path) as conn:
        # Use IN clause with placeholders
        placeholders = ",".join("?" * len(tweet_ids))
        rows = conn.execute(
            f"""
            SELECT id, classification_status, classification_result
            FROM tweets
            WHERE id IN ({placeholders})
        """,
            tweet_ids,
        ).fetchall()

        result = {}
        for row in rows:
            tweet_id = row["id"]
            if row["classification_status"] == "completed":
                result[tweet_id] = (
                    "approved" if row["classification_result"] == 1 else "filtered"
                )
            else:
                result[tweet_id] = "pending"

        # Mark any IDs not in database as 'unknown'
        for tweet_id in tweet_ids:
            if tweet_id not in result:
                result[tweet_id] = "unknown"

        return result


def get_stats(db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Get database statistics."""
    with transaction(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM tweets WHERE classification_status = 'pending'"
        ).fetchone()[0]
        approved = conn.execute(
            "SELECT COUNT(*) FROM tweets WHERE classification_result = 1"
        ).fetchone()[0]
        filtered = conn.execute(
            "SELECT COUNT(*) FROM tweets WHERE classification_result = 0"
        ).fetchone()[0]

        return {
            "total": total,
            "pending": pending,
            "approved": approved,
            "filtered": filtered,
        }


# =============================================================================
# Prompt and Mode Management
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
        row = conn.execute(
            "SELECT * FROM prompts WHERE id = ?", (prompt_id,)
        ).fetchone()
        return dict(row) if row else None


def list_prompts(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """List all prompts."""
    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM prompts ORDER BY created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def create_mode(
    mode_id: str,
    name: str,
    prompt_id: str,
    extractor: str,
    prefilter: str | None = None,
    description: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Create or update a mode definition."""
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO modes (id, name, prompt_id, prefilter, extractor, description)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (mode_id, name, prompt_id, prefilter, extractor, description),
        )


def get_mode(mode_id: str, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    """Get a mode by ID."""
    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM modes WHERE id = ?", (mode_id,)
        ).fetchone()
        return dict(row) if row else None


def list_modes(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """List all modes."""
    with transaction(db_path) as conn:
        rows = conn.execute("SELECT * FROM modes ORDER BY name").fetchall()
        return [dict(row) for row in rows]


# =============================================================================
# Prompt Responses (LLM output cache)
# =============================================================================


def store_prompt_response(
    tweet_id: str,
    prompt_id: str,
    model: str,
    response: dict | Any,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Store an LLM response for a tweet/prompt pair."""
    response_json = json.dumps(response) if not isinstance(response, str) else response
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO prompt_responses
            (tweet_id, prompt_id, model, response_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tweet_id, prompt_id, model, response_json, datetime.utcnow().isoformat()),
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
# Mode-based Tweet Queries
# =============================================================================


def get_tweet(tweet_id: str, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    """Get a single tweet by ID."""
    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM tweets WHERE id = ?", (tweet_id,)
        ).fetchone()
        return dict(row) if row else None


def get_tweets_batch(
    tweet_ids: list[str], db_path: Path = DEFAULT_DB_PATH
) -> dict[str, dict]:
    """Get multiple tweets by ID."""
    if not tweet_ids:
        return {}
    with transaction(db_path) as conn:
        placeholders = ",".join("?" * len(tweet_ids))
        rows = conn.execute(
            f"SELECT * FROM tweets WHERE id IN ({placeholders})",
            tweet_ids,
        ).fetchall()
        return {row["id"]: dict(row) for row in rows}


if __name__ == "__main__":
    # Initialize database when run directly
    init_database()
    print(f"Database initialized at {DEFAULT_DB_PATH}")
