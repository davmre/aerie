"""
Core database infrastructure.

Provides connection management, transactions, and schema initialization.
"""

import base64
import json
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# Database path can be configured via environment variable
# Default: tweets.db in the parent directory of this file
_env_db_path = os.environ.get("AERIE_DB_PATH")
DEFAULT_DB_PATH = (
    Path(_env_db_path) if _env_db_path else Path(__file__).parent.parent.parent / "tweets.db"
)


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Create a database connection with optimal settings."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction(db_path: Path = DEFAULT_DB_PATH) -> Generator[sqlite3.Connection, None, None]:
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


def parse_twitter_date(date_str: str | None) -> str | None:
    """
    Convert Twitter's date format to ISO format for proper sorting.

    Twitter format: "Wed Sep 10 14:24:00 +0000 2025"
    ISO format: "2025-09-10T14:24:00+00:00"

    Returns None if input is None/empty, passes through if already ISO format.
    """
    if not date_str:
        return None

    # Already in ISO format (starts with year)
    if date_str[:4].isdigit():
        return date_str

    try:
        # Twitter format: "Wed Sep 10 14:24:00 +0000 2025"
        dt = datetime.strptime(date_str, "%a %b %d %H:%M:%S %z %Y")
        return dt.isoformat()
    except ValueError:
        # If parsing fails, return as-is
        return date_str


# =============================================================================
# Cursor-Based Pagination Helpers
# =============================================================================


def encode_cursor(created_at: str, tweet_id: str) -> str:
    """
    Encode (created_at, id) as a base64 JSON cursor for pagination.

    The cursor uniquely identifies a position in the timeline, allowing
    stable pagination even when new posts are added.

    Args:
        created_at: ISO timestamp of the tweet
        tweet_id: Tweet ID

    Returns:
        URL-safe base64 encoded cursor string
    """
    cursor_data = json.dumps({"ts": created_at, "id": tweet_id})
    return base64.urlsafe_b64encode(cursor_data.encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, str] | None:
    """
    Decode a cursor back to (created_at, id).

    Args:
        cursor: Base64 encoded cursor string

    Returns:
        Tuple of (created_at, tweet_id), or None if cursor is invalid
    """
    try:
        cursor_data = base64.urlsafe_b64decode(cursor.encode()).decode()
        data = json.loads(cursor_data)
        return (data["ts"], data["id"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


# =============================================================================
# Schema Initialization
# =============================================================================


def init_database(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Initialize the database schema."""
    with transaction(db_path) as conn:
        conn.executescript("""
            -- Core tweet data
            CREATE TABLE IF NOT EXISTS tweets (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                created_at TEXT,
                captured_at TEXT NOT NULL,

                -- Platform identification
                platform TEXT DEFAULT 'twitter',
                platform_metadata TEXT,

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
            CREATE INDEX IF NOT EXISTS idx_tweets_quoted_tweet_id ON tweets(quoted_tweet_id);
            -- Note: idx_tweets_platform is created after platform column migration

            -- Composite index for cursor-based pagination (newest first)
            CREATE INDEX IF NOT EXISTS idx_tweets_created_at_id
                ON tweets(created_at DESC, id DESC);

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

            -- Mode definitions (prompt + extractor + provider)
            CREATE TABLE IF NOT EXISTS modes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                prompt_id TEXT NOT NULL,
                prefilter TEXT,
                extractor TEXT NOT NULL,
                description TEXT,
                provider TEXT DEFAULT 'anthropic',
                model_name TEXT,
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
                platform TEXT DEFAULT 'twitter',
                PRIMARY KEY (original_tweet_id, retweeter_username),
                FOREIGN KEY (original_tweet_id) REFERENCES tweets(id)
            );

            -- Indexes for new tables
            CREATE INDEX IF NOT EXISTS idx_prompt_responses_tweet ON prompt_responses(tweet_id);
            CREATE INDEX IF NOT EXISTS idx_retweets_original ON retweets(original_tweet_id);
            CREATE INDEX IF NOT EXISTS idx_prompt_responses_prompt ON prompt_responses(prompt_id);
            CREATE INDEX IF NOT EXISTS idx_human_labels_mode ON human_labels(mode_id);

            -- Cached mode decisions for fast lookups
            -- Stores computed show/hide decisions per tweet per mode
            CREATE TABLE IF NOT EXISTS mode_decisions (
                tweet_id TEXT NOT NULL,
                mode_id TEXT NOT NULL,
                decision TEXT NOT NULL CHECK (decision IN ('approved', 'filtered')),
                source TEXT NOT NULL CHECK (source IN ('prefilter', 'extractor')),
                computed_at TEXT NOT NULL,
                PRIMARY KEY (tweet_id, mode_id),
                FOREIGN KEY (tweet_id) REFERENCES tweets(id),
                FOREIGN KEY (mode_id) REFERENCES modes(id)
            );

            -- Indexes for efficient mode-based queries
            CREATE INDEX IF NOT EXISTS idx_mode_decisions_mode ON mode_decisions(mode_id);
            CREATE INDEX IF NOT EXISTS idx_mode_decisions_mode_decision
                ON mode_decisions(mode_id, decision);

            -- Application settings (key-value store)
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            );

            -- Context snapshots for situational awareness in classification
            CREATE TABLE IF NOT EXISTS contexts (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                token_count INTEGER,
                metadata TEXT
            );

            -- Index for listing contexts by creation time
            CREATE INDEX IF NOT EXISTS idx_contexts_created_at ON contexts(created_at DESC);
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
            ("platform", "TEXT DEFAULT 'twitter'"),
            ("platform_metadata", "TEXT"),
        ]
        for col_name, col_type in new_columns:
            if col_name not in existing_columns:
                conn.execute(f"ALTER TABLE tweets ADD COLUMN {col_name} {col_type}")

        # Create platform index (after column is ensured to exist)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tweets_platform ON tweets(platform)")

        # Migration: Add config columns to modes table if they don't exist
        cursor = conn.execute("PRAGMA table_info(modes)")
        existing_mode_columns = {row[1] for row in cursor.fetchall()}

        mode_new_columns = [
            ("extractor_config", "TEXT"),
            ("prefilter_config", "TEXT"),
            ("provider", "TEXT DEFAULT 'anthropic'"),
            ("model_name", "TEXT"),
        ]
        for col_name, col_type in mode_new_columns:
            if col_name not in existing_mode_columns:
                conn.execute(f"ALTER TABLE modes ADD COLUMN {col_name} {col_type}")

        # Migration: Add classification_batch_id and context_id to prompt_responses
        cursor = conn.execute("PRAGMA table_info(prompt_responses)")
        existing_pr_columns = {row[1] for row in cursor.fetchall()}
        if "classification_batch_id" not in existing_pr_columns:
            conn.execute("ALTER TABLE prompt_responses ADD COLUMN classification_batch_id TEXT")
        if "context_id" not in existing_pr_columns:
            conn.execute(
                "ALTER TABLE prompt_responses ADD COLUMN context_id TEXT REFERENCES contexts(id)"
            )

        # Migration: Add classification_batch_id to mode_decisions
        cursor = conn.execute("PRAGMA table_info(mode_decisions)")
        existing_md_columns = {row[1] for row in cursor.fetchall()}
        if "classification_batch_id" not in existing_md_columns:
            conn.execute("ALTER TABLE mode_decisions ADD COLUMN classification_batch_id TEXT")

        # Migration: Add platform column to retweets table if it doesn't exist
        cursor = conn.execute("PRAGMA table_info(retweets)")
        existing_retweet_columns = {row[1] for row in cursor.fetchall()}

        if "platform" not in existing_retweet_columns:
            conn.execute("ALTER TABLE retweets ADD COLUMN platform TEXT DEFAULT 'twitter'")
