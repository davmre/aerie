"""SQLite database operations for tweet storage and classification."""

import base64
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tweets_platform ON tweets(platform)"
        )

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

        # Migration: Add classification_batch_id to prompt_responses
        cursor = conn.execute("PRAGMA table_info(prompt_responses)")
        existing_pr_columns = {row[1] for row in cursor.fetchall()}
        if "classification_batch_id" not in existing_pr_columns:
            conn.execute("ALTER TABLE prompt_responses ADD COLUMN classification_batch_id TEXT")

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


def store_tweets(tweets: list[dict], db_path: Path = DEFAULT_DB_PATH) -> dict:
    """
    Store tweets in the database, deduplicating by ID.

    Returns:
        {
            "inserted": int - count of newly inserted tweets,
            "duplicates": int - count of duplicates/updates,
            "inserted_tweets": list[dict] - full tweet dicts that were actually inserted
        }
    """
    if not tweets:
        return {"inserted": 0, "duplicates": 0, "inserted_tweets": []}

    inserted = 0
    duplicates = 0
    inserted_tweets = []

    with transaction(db_path) as conn:
        # First, check which tweet IDs already exist (for accurate insert tracking)
        tweet_ids = [t["id"] for t in tweets]
        if tweet_ids:
            placeholders = ",".join("?" * len(tweet_ids))
            existing_rows = conn.execute(
                f"SELECT id FROM tweets WHERE id IN ({placeholders})",
                tweet_ids,
            ).fetchall()
            existing_ids = {row["id"] for row in existing_rows}
        else:
            existing_ids = set()

        for tweet in tweets:
            try:
                tweet_id = tweet["id"]
                is_new = tweet_id not in existing_ids

                author = tweet.get("author", {})
                text = tweet["text"]
                # Extract platform-specific data
                platform = tweet.get("platform", "twitter")
                platform_metadata = tweet.get("platform_metadata")
                if platform_metadata and not isinstance(platform_metadata, str):
                    platform_metadata = json.dumps(platform_metadata)

                conn.execute(
                    """
                    INSERT INTO tweets (
                        id, text, created_at, captured_at,
                        platform, platform_metadata,
                        author_id, author_username, author_display_name, author_verified,
                        author_blue_verified, author_bio, author_following, author_followers_count,
                        retweet_count, reply_count, like_count, quote_count,
                        reply_to_tweet_id, reply_to_user_id, reply_to_username,
                        is_retweet, is_quote, is_promoted, quoted_tweet_id,
                        media_json, urls_json, hashtags_json, mentions_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        text = CASE WHEN length(excluded.text) > length(tweets.text) THEN excluded.text ELSE tweets.text END,
                        platform = COALESCE(excluded.platform, tweets.platform),
                        platform_metadata = COALESCE(excluded.platform_metadata, tweets.platform_metadata),
                        author_username = COALESCE(excluded.author_username, tweets.author_username),
                        author_display_name = COALESCE(excluded.author_display_name, tweets.author_display_name),
                        author_bio = COALESCE(excluded.author_bio, tweets.author_bio),
                        author_following = COALESCE(excluded.author_following, tweets.author_following),
                        author_followers_count = COALESCE(excluded.author_followers_count, tweets.author_followers_count),
                        is_promoted = MAX(tweets.is_promoted, excluded.is_promoted)
                """,
                    (
                        tweet_id,
                        text,
                        parse_twitter_date(tweet.get("created_at")),
                        tweet.get("captured_at", datetime.utcnow().isoformat()),
                        platform,
                        platform_metadata,
                        author.get("id"),
                        author.get("username"),
                        author.get("display_name"),
                        1 if author.get("verified") else 0,
                        1
                        if author.get("blue_verified")
                        else (0 if author.get("blue_verified") is False else None),
                        author.get("bio"),
                        1
                        if author.get("following")
                        else (0 if author.get("following") is False else None),
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

                if is_new:
                    inserted += 1
                    inserted_tweets.append(tweet)
                else:
                    duplicates += 1
            except sqlite3.IntegrityError:
                # Shouldn't happen with ON CONFLICT, but just in case
                duplicates += 1

    return {"inserted": inserted, "duplicates": duplicates, "inserted_tweets": inserted_tweets}


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
                result = conn.execute(
                    """
                    INSERT INTO retweets (
                        original_tweet_id, retweeter_user_id, retweeter_username,
                        retweeter_display_name, retweeted_at, captured_at, platform
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(original_tweet_id, retweeter_username) DO NOTHING
                """,
                    (
                        rt["original_tweet_id"],
                        rt.get("retweeter_user_id"),
                        rt["retweeter_username"],
                        rt.get("retweeter_display_name"),
                        parse_twitter_date(rt.get("retweeted_at")),
                        rt.get("captured_at", datetime.utcnow().isoformat()),
                        rt.get("platform", "twitter"),
                    ),
                )
                # rowcount is 0 when DO NOTHING triggers, 1 for actual insert
                if result.rowcount > 0:
                    inserted += 1
                else:
                    duplicates += 1
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


def get_retweets_batch(
    tweet_ids: list[str], db_path: Path = DEFAULT_DB_PATH
) -> dict[str, list[dict]]:
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
        params = []

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
        params = []

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
# Prompt Responses (LLM output cache)
# =============================================================================


def store_prompt_response(
    tweet_id: str,
    prompt_id: str,
    model: str,
    response: dict | Any,
    db_path: Path = DEFAULT_DB_PATH,
    classification_batch_id: str | None = None,
) -> None:
    """Store an LLM response for a tweet/prompt pair."""
    response_json = json.dumps(response) if not isinstance(response, str) else response
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO prompt_responses
            (tweet_id, prompt_id, model, response_json, created_at, classification_batch_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tweet_id, prompt_id, model, response_json, datetime.utcnow().isoformat(), classification_batch_id),
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
        platform_params = []
        if platform:
            platform_clause = "WHERE platform = ?"
            platform_params = [platform]

        total = conn.execute(
            f"SELECT COUNT(*) FROM tweets {platform_clause}",
            platform_params,
        ).fetchone()[0]

        # Build join query with platform filter
        join_platform_clause = ""
        if platform:
            join_platform_clause = "JOIN tweets t ON md.tweet_id = t.id WHERE t.platform = ? AND"
            approved_params = [platform, mode_id]
            filtered_params = [platform, mode_id]
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
        params: list = [mode_id]

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


def get_all_tweet_ids(db_path: Path = DEFAULT_DB_PATH) -> list[str]:
    """Get all tweet IDs in the database."""
    with transaction(db_path) as conn:
        rows = conn.execute("SELECT id FROM tweets").fetchall()
        return [row["id"] for row in rows]


def get_platform_stats(db_path: Path = DEFAULT_DB_PATH) -> dict[str, int]:
    """Get tweet counts by platform."""
    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT platform, COUNT(*) as count FROM tweets GROUP BY platform"
        ).fetchall()
        return {row["platform"]: row["count"] for row in rows}


def get_bluesky_post_ids(db_path: Path = DEFAULT_DB_PATH) -> set[str]:
    """Get all Bluesky post IDs in the database for efficient existence checks."""
    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM tweets WHERE platform = 'bluesky'"
        ).fetchall()
        return {row["id"] for row in rows}


# =============================================================================
# Settings
# =============================================================================


def get_setting(key: str, db_path: Path = DEFAULT_DB_PATH) -> str | None:
    """Get a setting value by key."""
    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
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


# =============================================================================
# Mode-based Tweet Queries
# =============================================================================


def get_tweet(tweet_id: str, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    """Get a single tweet by ID."""
    with transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM tweets WHERE id = ?", (tweet_id,)).fetchone()
        return dict(row) if row else None


def get_tweets_batch(tweet_ids: list[str], db_path: Path = DEFAULT_DB_PATH) -> dict[str, dict]:
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
            "SELECT author_username, reply_to_tweet_id FROM tweets WHERE id = ?", (current_id,)
        ).fetchone()

        if not original or not original["reply_to_tweet_id"]:
            return []

        thread_author = original["author_username"]
        current_id = original["reply_to_tweet_id"]

        for _ in range(max_depth):
            row = conn.execute("SELECT * FROM tweets WHERE id = ?", (current_id,)).fetchone()

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


# =============================================================================
# Conversation Chains (for Read page)
# =============================================================================
#
# The Read page displays tweets as "conversation chains" rather than individual
# tweets. This provides a threaded view that groups related tweets together.
#
# Chain algorithm:
# - Find "roots" (approved tweets whose parent is not approved or doesn't exist)
# - From each root, follow the longest unambiguous path through replies
# - At each node, pick the child with the deepest subtree
# - If multiple children tie for max depth, stop (ambiguity) and mark them hidden
# - Hidden replies are accessible via a modal popup with drill-down navigation
#
# Scalability:
# - Paginate at the root level (not individual tweets)
# - Compute chains on-demand per root, not all at once
# - Uses BFS to find approved descendants of a single root
#
# Key functions:
# - get_conversation_roots(): Find paginated roots via SQL
# - compute_single_chain(): Build one chain from a root (scalable)
# - get_replies_for_tweet(): Get direct replies for modal view
# - compute_conversation_chains(): Original batch version (for tests)


def get_direct_replies(
    tweet_id: str, db_path: Path = DEFAULT_DB_PATH
) -> list[dict]:
    """Get all direct replies to a tweet."""
    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM tweets WHERE reply_to_tweet_id = ? ORDER BY created_at ASC",
            (tweet_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_direct_replies_batch(
    tweet_ids: list[str], db_path: Path = DEFAULT_DB_PATH
) -> dict[str, list[dict]]:
    """Get direct replies for multiple tweets at once."""
    if not tweet_ids:
        return {}

    with transaction(db_path) as conn:
        placeholders = ",".join("?" * len(tweet_ids))
        rows = conn.execute(
            f"""
            SELECT * FROM tweets
            WHERE reply_to_tweet_id IN ({placeholders})
            ORDER BY created_at ASC
            """,
            tweet_ids,
        ).fetchall()

        result: dict[str, list[dict]] = {}
        for row in rows:
            parent_id = row["reply_to_tweet_id"]
            if parent_id not in result:
                result[parent_id] = []
            result[parent_id].append(dict(row))
        return result


def get_conversation_roots(
    mode_id: str,
    sort_field: str = "created_at",
    limit: int = 20,
    offset: int = 0,
    platform: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> tuple[list[dict], int]:
    """
    Get conversation roots (tweets with no approved parent) for a mode.

    A root is an approved tweet where either:
    - It has no reply_to_tweet_id (original tweet), or
    - Its parent is not approved in this mode

    Args:
        mode_id: Mode ID for filtering approved tweets
        sort_field: Field to sort by (created_at or captured_at)
        limit: Number of roots to return
        offset: Pagination offset
        platform: Optional platform filter ('twitter', 'bluesky', or None for all)
        db_path: Database path

    Returns:
        Tuple of (list of root tweets, total count of roots)
    """
    # Validate sort field to prevent SQL injection
    if sort_field not in ("created_at", "captured_at"):
        sort_field = "created_at"

    # Build platform filter clause
    platform_clause = ""
    platform_params = []
    if platform:
        platform_clause = "AND t.platform = ?"
        platform_params = [platform]

    with transaction(db_path) as conn:
        # Count total roots first
        count_row = conn.execute(
            f"""
            SELECT COUNT(*) as cnt FROM tweets t
            JOIN mode_decisions md ON t.id = md.tweet_id
            WHERE md.mode_id = ? AND md.decision = 'approved'
              {platform_clause}
              AND (
                  t.reply_to_tweet_id IS NULL
                  OR NOT EXISTS (
                      SELECT 1 FROM mode_decisions md2
                      WHERE md2.tweet_id = t.reply_to_tweet_id
                        AND md2.mode_id = ?
                        AND md2.decision = 'approved'
                  )
              )
            """,
            (mode_id, *platform_params, mode_id),
        ).fetchone()
        total = count_row["cnt"]

        # Get paginated roots
        # Note: We use (sort_field DESC, id DESC) for consistent ordering with cursor-based pagination
        rows = conn.execute(
            f"""
            SELECT t.* FROM tweets t
            JOIN mode_decisions md ON t.id = md.tweet_id
            WHERE md.mode_id = ? AND md.decision = 'approved'
              {platform_clause}
              AND (
                  t.reply_to_tweet_id IS NULL
                  OR NOT EXISTS (
                      SELECT 1 FROM mode_decisions md2
                      WHERE md2.tweet_id = t.reply_to_tweet_id
                        AND md2.mode_id = ?
                        AND md2.decision = 'approved'
                  )
              )
            ORDER BY t.{sort_field} DESC, t.id DESC
            LIMIT ? OFFSET ?
            """,
            (mode_id, *platform_params, mode_id, limit, offset),
        ).fetchall()

        return [dict(row) for row in rows], total


def get_conversation_roots_cursor(
    mode_id: str,
    sort_field: str = "created_at",
    limit: int = 20,
    before_cursor: str | None = None,
    after_cursor: str | None = None,
    platform: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> tuple[list[dict], int, str | None, str | None]:
    """
    Get conversation roots with cursor-based pagination.

    This function supports both forward and backward pagination using cursors
    instead of offsets. This provides stable pagination even when new posts
    are added, preventing scroll position disruption.

    Args:
        mode_id: Mode ID for filtering approved tweets
        sort_field: Field to sort by (created_at or captured_at)
        limit: Number of roots to return
        before_cursor: Load posts older than this cursor (for scrolling down)
        after_cursor: Load posts newer than this cursor (for checking new posts)
        platform: Optional platform filter ('twitter', 'bluesky', or None for all)
        db_path: Database path

    Returns:
        Tuple of (list of root tweets, total count, oldest_cursor, newest_cursor)
        The oldest_cursor can be used to load more older posts.
        The newest_cursor can be used to check for new posts above.
    """
    # Validate sort field to prevent SQL injection
    if sort_field not in ("created_at", "captured_at"):
        sort_field = "created_at"

    # Build platform filter clause
    platform_clause = ""
    platform_params: list[str] = []
    if platform:
        platform_clause = "AND t.platform = ?"
        platform_params = [platform]

    with transaction(db_path) as conn:
        # Base query for conversation roots
        base_where = f"""
            md.mode_id = ? AND md.decision = 'approved'
            {platform_clause}
            AND (
                t.reply_to_tweet_id IS NULL
                OR NOT EXISTS (
                    SELECT 1 FROM mode_decisions md2
                    WHERE md2.tweet_id = t.reply_to_tweet_id
                      AND md2.mode_id = ?
                      AND md2.decision = 'approved'
                )
            )
        """

        # Count total roots (without cursor filtering)
        count_row = conn.execute(
            f"""
            SELECT COUNT(*) as cnt FROM tweets t
            JOIN mode_decisions md ON t.id = md.tweet_id
            WHERE {base_where}
            """,
            (mode_id, *platform_params, mode_id),
        ).fetchone()
        total = count_row["cnt"]

        # Build cursor clause
        cursor_clause = ""
        cursor_params: list[str] = []

        if before_cursor:
            # Load posts older than cursor (scrolling down)
            decoded = decode_cursor(before_cursor)
            if decoded:
                ts, tid = decoded
                cursor_clause = f"AND (t.{sort_field} < ? OR (t.{sort_field} = ? AND t.id < ?))"
                cursor_params = [ts, ts, tid]
        elif after_cursor:
            # Load posts newer than cursor (checking for new posts)
            decoded = decode_cursor(after_cursor)
            if decoded:
                ts, tid = decoded
                cursor_clause = f"AND (t.{sort_field} > ? OR (t.{sort_field} = ? AND t.id > ?))"
                cursor_params = [ts, ts, tid]

        # Get paginated roots
        rows = conn.execute(
            f"""
            SELECT t.* FROM tweets t
            JOIN mode_decisions md ON t.id = md.tweet_id
            WHERE {base_where}
              {cursor_clause}
            ORDER BY t.{sort_field} DESC, t.id DESC
            LIMIT ?
            """,
            (mode_id, *platform_params, mode_id, *cursor_params, limit),
        ).fetchall()

        roots = [dict(row) for row in rows]

        # Generate cursors for the returned results
        oldest_cursor = None
        newest_cursor = None

        if roots:
            # Newest cursor points to the first (most recent) item
            first_root = roots[0]
            newest_cursor = encode_cursor(
                first_root.get(sort_field) or "",
                first_root["id"]
            )

            # Oldest cursor points to the last (oldest) item
            last_root = roots[-1]
            oldest_cursor = encode_cursor(
                last_root.get(sort_field) or "",
                last_root["id"]
            )

        return roots, total, oldest_cursor, newest_cursor


def count_conversation_roots_after_cursor(
    mode_id: str,
    after_cursor: str,
    sort_field: str = "created_at",
    platform: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    """
    Count conversation roots newer than a cursor (for "N new posts" banner).

    Args:
        mode_id: Mode ID for filtering approved tweets
        after_cursor: Count posts newer than this cursor
        sort_field: Field to sort by (created_at or captured_at)
        platform: Optional platform filter

    Returns:
        Count of new conversation roots
    """
    decoded = decode_cursor(after_cursor)
    if not decoded:
        return 0

    ts, tid = decoded

    # Validate sort field
    if sort_field not in ("created_at", "captured_at"):
        sort_field = "created_at"

    # Build platform filter
    platform_clause = ""
    platform_params: list[str] = []
    if platform:
        platform_clause = "AND t.platform = ?"
        platform_params = [platform]

    with transaction(db_path) as conn:
        count_row = conn.execute(
            f"""
            SELECT COUNT(*) as cnt FROM tweets t
            JOIN mode_decisions md ON t.id = md.tweet_id
            WHERE md.mode_id = ? AND md.decision = 'approved'
              {platform_clause}
              AND (
                  t.reply_to_tweet_id IS NULL
                  OR NOT EXISTS (
                      SELECT 1 FROM mode_decisions md2
                      WHERE md2.tweet_id = t.reply_to_tweet_id
                        AND md2.mode_id = ?
                        AND md2.decision = 'approved'
                  )
              )
              AND (t.{sort_field} > ? OR (t.{sort_field} = ? AND t.id > ?))
            """,
            (mode_id, *platform_params, mode_id, ts, ts, tid),
        ).fetchone()

        return count_row["cnt"]


def get_approved_descendants(
    root_id: str,
    mode_id: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, dict]:
    """
    Get all approved descendants of a tweet (for computing a single chain).

    Uses a recursive approach to find all tweets in the subtree rooted at root_id
    that are approved in the given mode.

    Returns:
        Dict mapping tweet_id -> tweet dict for all approved descendants
        (including the root itself if approved)
    """
    with transaction(db_path) as conn:
        # Use iterative BFS to find all descendants
        # Start with the root
        root_row = conn.execute(
            "SELECT * FROM tweets WHERE id = ?", (root_id,)
        ).fetchone()
        if not root_row:
            return {}

        result = {root_id: dict(root_row)}
        queue = [root_id]
        visited = {root_id}

        while queue:
            current_ids = queue[:]
            queue = []

            # Find approved children of current batch
            placeholders = ",".join("?" * len(current_ids))
            rows = conn.execute(
                f"""
                SELECT t.* FROM tweets t
                JOIN mode_decisions md ON t.id = md.tweet_id
                WHERE t.reply_to_tweet_id IN ({placeholders})
                  AND md.mode_id = ?
                  AND md.decision = 'approved'
                """,
                (*current_ids, mode_id),
            ).fetchall()

            for row in rows:
                tid = row["id"]
                if tid not in visited:
                    visited.add(tid)
                    result[tid] = dict(row)
                    queue.append(tid)

        return result


def compute_single_chain(
    root_id: str,
    mode_id: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict:
    """
    Compute a single conversation chain starting from a root tweet.

    This is the scalable version that only loads the subtree for one chain,
    rather than all approved tweets.

    Args:
        root_id: ID of the root tweet (must be a conversation root)
        mode_id: Mode ID for filtering approved tweets
        db_path: Database path

    Returns:
        Chain entry with:
        - chain: list of tweets in chronological order
        - hidden_replies: dict mapping tweet_id -> count of hidden siblings
    """
    # Get all approved descendants of this root
    tweets_by_id = get_approved_descendants(root_id, mode_id, db_path)

    if not tweets_by_id:
        return {"chain": [], "hidden_replies": {}}

    tweet_set = set(tweets_by_id.keys())

    # Build parent -> children mapping
    children_by_parent: dict[str, list[str]] = {}
    for tid, tweet in tweets_by_id.items():
        parent_id = tweet.get("reply_to_tweet_id")
        if parent_id and parent_id in tweet_set:
            if parent_id not in children_by_parent:
                children_by_parent[parent_id] = []
            children_by_parent[parent_id].append(tid)

    # Sort children by created_at for deterministic ordering
    for parent_id in children_by_parent:
        children_by_parent[parent_id].sort(
            key=lambda tid: tweets_by_id[tid].get("created_at") or ""
        )

    # Compute max depth from each node (memoized)
    max_depth_cache: dict[str, int] = {}

    def get_max_depth(tid: str) -> int:
        if tid in max_depth_cache:
            return max_depth_cache[tid]

        children = children_by_parent.get(tid, [])
        if not children:
            max_depth_cache[tid] = 1
            return 1

        child_depths = [get_max_depth(c) for c in children]
        max_depth_cache[tid] = 1 + max(child_depths)
        return max_depth_cache[tid]

    # Precompute all depths
    for tid in tweets_by_id:
        get_max_depth(tid)

    # Build the chain starting from root
    chain_tweets = []
    hidden_replies: dict[str, int] = {}
    current_id: str | None = root_id

    while current_id:
        chain_tweets.append(tweets_by_id[current_id])

        children = children_by_parent.get(current_id, [])
        if not children:
            break

        # Get depths of all children
        child_depths = [(c, get_max_depth(c)) for c in children]

        # Find max depth
        max_child_depth = max(d for _, d in child_depths)

        # Find children with max depth
        best_children = [c for c, d in child_depths if d == max_child_depth]

        # Count hidden siblings
        hidden_count = len(children) - 1
        if hidden_count > 0:
            hidden_replies[current_id] = hidden_count

        if len(best_children) == 1:
            # Unambiguous - continue with this child
            current_id = best_children[0]
        else:
            # Tie - stop the chain here
            if len(best_children) > 1:
                hidden_replies[current_id] = len(children)
            break

    return {
        "chain": chain_tweets,
        "hidden_replies": hidden_replies,
    }


def compute_conversation_chains(
    tweet_ids: list[str],
    mode_id: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """
    Compute canonical conversation chains from a set of approved tweets.

    For each conversation, finds the longest unambiguous chain:
    - At each node, pick the child with the longest continuation
    - If multiple children have equal max depth, stop (ambiguity)

    Returns a list of chain entries, each containing:
    - chain: list of tweets in chronological order
    - hidden_replies: dict mapping tweet_id -> count of hidden siblings

    Args:
        tweet_ids: List of approved tweet IDs to consider
        mode_id: Mode ID for filtering approved tweets
        db_path: Database path
    """
    if not tweet_ids:
        return []

    # Build the reply graph for approved tweets only
    tweet_set = set(tweet_ids)

    with transaction(db_path) as conn:
        # Get all tweets in one query
        placeholders = ",".join("?" * len(tweet_ids))
        rows = conn.execute(
            f"SELECT * FROM tweets WHERE id IN ({placeholders})",
            tweet_ids,
        ).fetchall()
        tweets_by_id = {row["id"]: dict(row) for row in rows}

    # Build parent -> children mapping (only for approved tweets)
    children_by_parent: dict[str, list[str]] = {}
    for tid, tweet in tweets_by_id.items():
        parent_id = tweet.get("reply_to_tweet_id")
        if parent_id and parent_id in tweet_set:
            if parent_id not in children_by_parent:
                children_by_parent[parent_id] = []
            children_by_parent[parent_id].append(tid)

    # Sort children by created_at for deterministic ordering
    for parent_id in children_by_parent:
        children_by_parent[parent_id].sort(
            key=lambda tid: tweets_by_id[tid].get("created_at") or ""
        )

    # Compute max depth from each node (memoized)
    max_depth_cache: dict[str, int] = {}

    def get_max_depth(tid: str) -> int:
        if tid in max_depth_cache:
            return max_depth_cache[tid]

        children = children_by_parent.get(tid, [])
        if not children:
            max_depth_cache[tid] = 1
            return 1

        child_depths = [get_max_depth(c) for c in children]
        max_depth_cache[tid] = 1 + max(child_depths)
        return max_depth_cache[tid]

    # Precompute all depths
    for tid in tweet_ids:
        get_max_depth(tid)

    # Find roots: tweets whose parent is not in the approved set
    roots = []
    for tid in tweet_ids:
        tweet = tweets_by_id[tid]
        parent_id = tweet.get("reply_to_tweet_id")
        if not parent_id or parent_id not in tweet_set:
            roots.append(tid)

    # Track which tweets are already part of a chain
    used_in_chain: set[str] = set()

    # Build chains from each root
    chains = []
    for root_id in roots:
        if root_id in used_in_chain:
            continue

        chain_tweets = []
        hidden_replies: dict[str, int] = {}
        current_id = root_id

        while current_id and current_id not in used_in_chain:
            chain_tweets.append(tweets_by_id[current_id])
            used_in_chain.add(current_id)

            children = children_by_parent.get(current_id, [])
            if not children:
                break

            # Get depths of all children
            child_depths = [(c, get_max_depth(c)) for c in children]

            # Find max depth
            max_child_depth = max(d for _, d in child_depths)

            # Find children with max depth
            best_children = [c for c, d in child_depths if d == max_child_depth]

            # Count hidden siblings
            hidden_count = len(children) - 1
            if hidden_count > 0:
                hidden_replies[current_id] = hidden_count

            if len(best_children) == 1:
                # Unambiguous - continue with this child
                current_id = best_children[0]
            else:
                # Tie - stop the chain here
                # All children become hidden (they'll be roots of their own chains or expandable)
                if len(best_children) > 1:
                    hidden_replies[current_id] = len(children)
                break

        if chain_tweets:
            chains.append({
                "chain": chain_tweets,
                "hidden_replies": hidden_replies,
            })

    # Sort chains by the created_at of their first tweet (newest first for display)
    chains.sort(
        key=lambda c: c["chain"][0].get("created_at") or "",
        reverse=True,
    )

    return chains


def get_replies_for_tweet(
    tweet_id: str,
    mode_id: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict:
    """
    Get a tweet and its approved direct replies for the modal view.

    Returns:
    {
        "tweet": {...},  # The parent tweet
        "replies": [     # Direct replies (approved only)
            {"tweet": {...}, "reply_count": N},  # reply_count = approved replies to this reply
            ...
        ]
    }
    """
    with transaction(db_path) as conn:
        # Get the parent tweet
        row = conn.execute("SELECT * FROM tweets WHERE id = ?", (tweet_id,)).fetchone()
        if not row:
            return {"tweet": None, "replies": []}

        parent_tweet = dict(row)

        # Get direct replies that are approved in this mode
        replies_rows = conn.execute(
            """
            SELECT t.* FROM tweets t
            JOIN mode_decisions md ON t.id = md.tweet_id
            WHERE t.reply_to_tweet_id = ?
            AND md.mode_id = ?
            AND md.decision = 'approved'
            ORDER BY t.created_at ASC
            """,
            (tweet_id, mode_id),
        ).fetchall()

        replies = []
        reply_ids = [r["id"] for r in replies_rows]

        # Get reply counts for each reply (how many approved replies does each reply have)
        reply_counts: dict[str, int] = {}
        if reply_ids:
            placeholders = ",".join("?" * len(reply_ids))
            count_rows = conn.execute(
                f"""
                SELECT t.reply_to_tweet_id, COUNT(*) as cnt
                FROM tweets t
                JOIN mode_decisions md ON t.id = md.tweet_id
                WHERE t.reply_to_tweet_id IN ({placeholders})
                AND md.mode_id = ?
                AND md.decision = 'approved'
                GROUP BY t.reply_to_tweet_id
                """,
                (*reply_ids, mode_id),
            ).fetchall()
            for cr in count_rows:
                reply_counts[cr["reply_to_tweet_id"]] = cr["cnt"]

        for r in replies_rows:
            reply_dict = dict(r)
            replies.append({
                "tweet": reply_dict,
                "reply_count": reply_counts.get(r["id"], 0),
            })

        return {
            "tweet": parent_tweet,
            "replies": replies,
        }


def assemble_classification_chains(
    unclassified_tweets: list[dict],
    prompt_id: str,
    model: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """
    Assemble conversation chains for classification.

    Groups unclassified tweets into thread chains that should be classified together.
    For each chain, includes context from classified ancestors if present.

    Args:
        unclassified_tweets: List of tweet dicts without classifications
        prompt_id: Prompt ID (used to check which tweets are classified)
        model: Model name (used to check which tweets are classified)
        db_path: Database path

    Returns:
        List of chain dicts, each containing:
        - tweets: List of tweets in the chain (chronological order)
        - unclassified_ids: Set of tweet IDs that need classification
    """
    if not unclassified_tweets:
        return []

    # Build lookup for quick access
    tweets_by_id = {t["id"]: t for t in unclassified_tweets}
    unclassified_ids = set(tweets_by_id.keys())

    # Find thread roots for each unclassified tweet
    # Root = first unclassified tweet in the chain (or actual root if top-level)
    roots_map: dict[str, str] = {}  # tweet_id -> root_id

    def find_root(tweet_id: str, visited: set[str] | None = None) -> str:
        """Find the root of the thread this tweet belongs to."""
        if visited is None:
            visited = set()

        if tweet_id in visited:
            # Circular reference - treat current as root
            return tweet_id

        if tweet_id in roots_map:
            return roots_map[tweet_id]

        visited.add(tweet_id)

        tweet = tweets_by_id.get(tweet_id)
        if not tweet:
            roots_map[tweet_id] = tweet_id
            return tweet_id

        # Handle both database format (reply_to_tweet_id) and make_tweet format (reply_to.tweet_id)
        parent_id = tweet.get("reply_to_tweet_id")
        if not parent_id and "reply_to" in tweet:
            parent_id = tweet.get("reply_to", {}).get("tweet_id")

        # If no parent, this is the root
        if not parent_id:
            roots_map[tweet_id] = tweet_id
            return tweet_id

        # If parent is also unclassified, recurse
        if parent_id in unclassified_ids:
            root = find_root(parent_id, visited)
            roots_map[tweet_id] = root
            return root

        # Parent is classified or not in our set - this tweet is the root
        roots_map[tweet_id] = tweet_id
        return tweet_id

    # Group tweets by their root
    chains_by_root: dict[str, list[str]] = {}
    for tweet_id in unclassified_ids:
        root_id = find_root(tweet_id)
        if root_id not in chains_by_root:
            chains_by_root[root_id] = []
        chains_by_root[root_id].append(tweet_id)

    # Build chains with context
    chains = []

    with transaction(db_path) as conn:
        for root_id, tweet_ids in chains_by_root.items():
            # Sort tweets chronologically
            chain_tweets = [tweets_by_id[tid] for tid in tweet_ids]
            chain_tweets.sort(key=lambda t: t.get("created_at") or "")

            # Check if we need to fetch classified ancestors for context
            root_tweet = tweets_by_id[root_id]
            ancestor_context = []

            # Handle both database format and make_tweet format
            parent_id = root_tweet.get("reply_to_tweet_id")
            if not parent_id and "reply_to" in root_tweet:
                parent_id = root_tweet.get("reply_to", {}).get("tweet_id")

            if parent_id:
                # This thread has classified ancestors - fetch them for context
                current_parent_id = parent_id
                visited = set()

                while current_parent_id and current_parent_id not in visited:
                    visited.add(current_parent_id)

                    # Fetch parent tweet
                    parent_row = conn.execute(
                        "SELECT * FROM tweets WHERE id = ?",
                        (current_parent_id,)
                    ).fetchone()

                    if parent_row:
                        parent_dict = dict(parent_row)
                        ancestor_context.insert(0, parent_dict)  # Prepend (build oldest-first)
                        current_parent_id = parent_dict.get("reply_to_tweet_id")
                    else:
                        break

            # Combine ancestors + unclassified tweets
            full_chain = ancestor_context + chain_tweets

            # Enrich tweets with quoted tweet content
            quoted_ids = [
                t.get("quoted_tweet_id")
                for t in full_chain
                if t.get("quoted_tweet_id")
            ]
            if quoted_ids:
                placeholders = ",".join("?" * len(quoted_ids))
                quoted_rows = conn.execute(
                    f"SELECT * FROM tweets WHERE id IN ({placeholders})",
                    quoted_ids
                ).fetchall()
                quoted_by_id = {row["id"]: dict(row) for row in quoted_rows}

                for tweet in full_chain:
                    qid = tweet.get("quoted_tweet_id")
                    if qid and qid in quoted_by_id:
                        tweet["quoted_tweet"] = quoted_by_id[qid]

            chains.append({
                "tweets": full_chain,
                "unclassified_ids": set(tweet_ids),
            })

    return chains


if __name__ == "__main__":
    # Initialize database when run directly
    init_database()
    print(f"Database initialized at {DEFAULT_DB_PATH}")
