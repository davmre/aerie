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
                author_blue_verified INTEGER,
                author_bio TEXT,
                author_following INTEGER,
                author_followers_count INTEGER,

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
                is_promoted INTEGER DEFAULT 0,
                quoted_tweet_id TEXT,

                -- Structured data stored as JSON
                media_json TEXT,
                urls_json TEXT,
                hashtags_json TEXT,
                mentions_json TEXT,

                -- DEPRECATED: Legacy classification fields (kept for migration compatibility)
                -- Use prompt_responses table instead for classification results
                classification_status TEXT DEFAULT 'pending',
                classification_result INTEGER,
                classification_reason TEXT,
                classified_at TEXT
            );

            -- Indexes for common queries
            CREATE INDEX IF NOT EXISTS idx_tweets_captured_at ON tweets(captured_at);
            CREATE INDEX IF NOT EXISTS idx_tweets_created_at ON tweets(created_at);
            CREATE INDEX IF NOT EXISTS idx_tweets_author ON tweets(author_username);
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

            -- Track retweets: who retweeted which original tweet
            -- The original tweet is stored in tweets table; this tracks the retweet relationship
            CREATE TABLE IF NOT EXISTS retweets (
                original_tweet_id TEXT NOT NULL,
                retweeter_user_id TEXT,
                retweeter_username TEXT NOT NULL,
                retweeter_display_name TEXT,
                retweeted_at TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                PRIMARY KEY (original_tweet_id, retweeter_username),
                FOREIGN KEY (original_tweet_id) REFERENCES tweets(id)
            );

            -- Indexes for new tables
            CREATE INDEX IF NOT EXISTS idx_prompt_responses_tweet ON prompt_responses(tweet_id);
            CREATE INDEX IF NOT EXISTS idx_retweets_original ON retweets(original_tweet_id);
            CREATE INDEX IF NOT EXISTS idx_prompt_responses_prompt ON prompt_responses(prompt_id);
            CREATE INDEX IF NOT EXISTS idx_human_labels_mode ON human_labels(mode_id);
        """)

        # Migration: Add new author columns if they don't exist
        cursor = conn.execute("PRAGMA table_info(tweets)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        new_columns = [
            ("author_blue_verified", "INTEGER"),
            ("author_bio", "TEXT"),
            ("author_following", "INTEGER"),
            ("author_followers_count", "INTEGER"),
            ("is_promoted", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_type in new_columns:
            if col_name not in existing_columns:
                conn.execute(f"ALTER TABLE tweets ADD COLUMN {col_name} {col_type}")


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
                author = tweet.get("author", {})
                text = tweet["text"]
                result = conn.execute(
                    """
                    INSERT INTO tweets (
                        id, text, created_at, captured_at,
                        author_id, author_username, author_display_name, author_verified,
                        author_blue_verified, author_bio, author_following, author_followers_count,
                        retweet_count, reply_count, like_count, quote_count,
                        reply_to_tweet_id, reply_to_user_id, reply_to_username,
                        is_retweet, is_quote, is_promoted, quoted_tweet_id,
                        media_json, urls_json, hashtags_json, mentions_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        text = CASE WHEN length(excluded.text) > length(tweets.text) THEN excluded.text ELSE tweets.text END,
                        author_username = COALESCE(excluded.author_username, tweets.author_username),
                        author_display_name = COALESCE(excluded.author_display_name, tweets.author_display_name),
                        author_bio = COALESCE(excluded.author_bio, tweets.author_bio),
                        author_following = COALESCE(excluded.author_following, tweets.author_following),
                        author_followers_count = COALESCE(excluded.author_followers_count, tweets.author_followers_count),
                        is_promoted = MAX(tweets.is_promoted, excluded.is_promoted)
                """,
                    (
                        tweet["id"],
                        text,
                        tweet.get("created_at"),
                        tweet.get("captured_at", datetime.utcnow().isoformat()),
                        author.get("id"),
                        author.get("username"),
                        author.get("display_name"),
                        1 if author.get("verified") else 0,
                        1 if author.get("blue_verified") else (0 if author.get("blue_verified") is False else None),
                        author.get("bio"),
                        1 if author.get("following") else (0 if author.get("following") is False else None),
                        author.get("followers_count"),
                        tweet.get("metrics", {}).get("retweet_count", 0),
                        tweet.get("metrics", {}).get("reply_count", 0),
                        tweet.get("metrics", {}).get("like_count", 0),
                        tweet.get("metrics", {}).get("quote_count", 0),
                        tweet.get("reply_to", {}).get("tweet_id"),
                        tweet.get("reply_to", {}).get("user_id"),
                        tweet.get("reply_to", {}).get("username"),
                        1 if tweet.get("is_retweet") else 0,
                        1 if tweet.get("is_quote") else 0,
                        1 if tweet.get("is_promoted") else 0,
                        tweet.get("quoted_tweet_id"),
                        json.dumps(tweet.get("media", [])),
                        json.dumps(tweet.get("urls", [])),
                        json.dumps(tweet.get("hashtags", [])),
                        json.dumps(tweet.get("mentions", [])),
                    ),
                )
                # rowcount is 1 for insert, 1 for update (if changes made), 0 for no-op update
                if result.rowcount > 0:
                    inserted += 1
                else:
                    duplicates += 1
            except sqlite3.IntegrityError:
                # Shouldn't happen with ON CONFLICT, but just in case
                duplicates += 1

    return {"inserted": inserted, "duplicates": duplicates}


def store_retweets(retweets: list[dict], db_path: Path = DEFAULT_DB_PATH) -> dict:
    """
    Store retweet records. Each record links a retweeter to an original tweet.
    Expected format: {
        "original_tweet_id": "...",
        "retweeter_user_id": "...",
        "retweeter_username": "...",
        "retweeter_display_name": "...",
        "retweeted_at": "...",
        "captured_at": "..."
    }
    """
    if not retweets:
        return {"inserted": 0, "duplicates": 0}

    inserted = 0
    duplicates = 0

    with transaction(db_path) as conn:
        for rt in retweets:
            try:
                conn.execute(
                    """
                    INSERT INTO retweets (
                        original_tweet_id, retweeter_user_id, retweeter_username,
                        retweeter_display_name, retweeted_at, captured_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(original_tweet_id, retweeter_username) DO NOTHING
                """,
                    (
                        rt["original_tweet_id"],
                        rt.get("retweeter_user_id"),
                        rt["retweeter_username"],
                        rt.get("retweeter_display_name"),
                        rt.get("retweeted_at"),
                        rt.get("captured_at", datetime.utcnow().isoformat()),
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                duplicates += 1

    return {"inserted": inserted, "duplicates": duplicates}


def get_retweets_for_tweet(tweet_id: str, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Get all retweet records for a given original tweet."""
    with transaction(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM retweets
            WHERE original_tweet_id = ?
            ORDER BY retweeted_at DESC
        """,
            (tweet_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_retweets_batch(tweet_ids: list[str], db_path: Path = DEFAULT_DB_PATH) -> dict[str, list[dict]]:
    """Get retweet records for multiple tweets at once."""
    if not tweet_ids:
        return {}

    with transaction(db_path) as conn:
        placeholders = ",".join("?" * len(tweet_ids))
        rows = conn.execute(
            f"""
            SELECT * FROM retweets
            WHERE original_tweet_id IN ({placeholders})
            ORDER BY retweeted_at DESC
        """,
            tweet_ids,
        ).fetchall()

        result = {}
        for row in rows:
            tid = row["original_tweet_id"]
            if tid not in result:
                result[tid] = []
            result[tid].append(dict(row))
        return result


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


def get_thread_ancestors(
    tweet_id: str, max_depth: int = 10, db_path: Path = DEFAULT_DB_PATH
) -> list[dict]:
    """
    Get thread ancestors (parent tweets by same author).
    Returns a list of ancestor tweets in chronological order (oldest first).
    Stops when:
    - No more parent tweets
    - Parent is by a different author (not a thread continuation)
    - Max depth reached
    - Parent tweet not in database
    """
    ancestors = []
    current_id = tweet_id

    with transaction(db_path) as conn:
        # First get the original tweet to know the author
        original = conn.execute(
            "SELECT author_username, reply_to_tweet_id FROM tweets WHERE id = ?",
            (current_id,)
        ).fetchone()

        if not original or not original["reply_to_tweet_id"]:
            return []

        thread_author = original["author_username"]
        current_id = original["reply_to_tweet_id"]

        for _ in range(max_depth):
            row = conn.execute(
                "SELECT * FROM tweets WHERE id = ?", (current_id,)
            ).fetchone()

            if not row:
                # Parent not in database
                break

            parent = dict(row)

            # Check if this is part of the same thread (same author)
            if parent.get("author_username") != thread_author:
                # Different author - this is a reply to someone else, not a thread
                # Still include it as context but stop climbing
                ancestors.insert(0, parent)
                break

            ancestors.insert(0, parent)

            # Move to next parent
            if not parent.get("reply_to_tweet_id"):
                break
            current_id = parent["reply_to_tweet_id"]

    return ancestors


def get_thread_context_batch(
    tweet_ids: list[str], max_depth: int = 10, db_path: Path = DEFAULT_DB_PATH
) -> dict[str, list[dict]]:
    """Get thread ancestors for multiple tweets at once."""
    result = {}
    for tid in tweet_ids:
        ancestors = get_thread_ancestors(tid, max_depth, db_path)
        if ancestors:
            result[tid] = ancestors
    return result


if __name__ == "__main__":
    # Initialize database when run directly
    init_database()
    print(f"Database initialized at {DEFAULT_DB_PATH}")
