"""SQLite database operations for tweet storage."""

import sqlite3
import json
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime

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
                conn.execute("""
                    INSERT INTO tweets (
                        id, text, created_at, captured_at,
                        author_id, author_username, author_display_name, author_verified,
                        retweet_count, reply_count, like_count, quote_count,
                        reply_to_tweet_id, reply_to_user_id, reply_to_username,
                        is_retweet, is_quote, quoted_tweet_id,
                        media_json, urls_json, hashtags_json, mentions_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
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
                ))
                inserted += 1
            except sqlite3.IntegrityError:
                # Duplicate tweet ID - this is expected and fine
                duplicates += 1

    return {"inserted": inserted, "duplicates": duplicates}


def get_pending_tweets(limit: int = 100, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Get tweets that haven't been classified yet."""
    with transaction(db_path) as conn:
        rows = conn.execute("""
            SELECT * FROM tweets
            WHERE classification_status = 'pending'
            ORDER BY captured_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(row) for row in rows]


def get_approved_tweets(limit: int = 100, offset: int = 0, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Get tweets that passed classification."""
    with transaction(db_path) as conn:
        rows = conn.execute("""
            SELECT * FROM tweets
            WHERE classification_result = 1
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
        return [dict(row) for row in rows]


def update_classification(tweet_id: str, approved: bool, reason: str = None,
                          db_path: Path = DEFAULT_DB_PATH):
    """Update a tweet's classification status."""
    with transaction(db_path) as conn:
        conn.execute("""
            UPDATE tweets SET
                classification_status = 'completed',
                classification_result = ?,
                classification_reason = ?,
                classified_at = ?
            WHERE id = ?
        """, (1 if approved else 0, reason, datetime.utcnow().isoformat(), tweet_id))


def approve_all_pending(db_path: Path = DEFAULT_DB_PATH) -> int:
    """Approve all pending tweets. Returns count of approved tweets."""
    with transaction(db_path) as conn:
        cursor = conn.execute("""
            UPDATE tweets SET
                classification_status = 'completed',
                classification_result = 1,
                classification_reason = 'auto-approved',
                classified_at = ?
            WHERE classification_status = 'pending'
        """, (datetime.utcnow().isoformat(),))
        return cursor.rowcount


def check_tweet_statuses(tweet_ids: list[str], db_path: Path = DEFAULT_DB_PATH) -> dict[str, str]:
    """
    Check the classification status of multiple tweets.
    Returns a dict mapping tweet_id -> status ('approved', 'filtered', 'pending', or 'unknown').
    """
    if not tweet_ids:
        return {}

    with transaction(db_path) as conn:
        # Use IN clause with placeholders
        placeholders = ','.join('?' * len(tweet_ids))
        rows = conn.execute(f"""
            SELECT id, classification_status, classification_result
            FROM tweets
            WHERE id IN ({placeholders})
        """, tweet_ids).fetchall()

        result = {}
        for row in rows:
            tweet_id = row['id']
            if row['classification_status'] == 'completed':
                result[tweet_id] = 'approved' if row['classification_result'] == 1 else 'filtered'
            else:
                result[tweet_id] = 'pending'

        # Mark any IDs not in database as 'unknown'
        for tweet_id in tweet_ids:
            if tweet_id not in result:
                result[tweet_id] = 'unknown'

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


if __name__ == "__main__":
    # Initialize database when run directly
    init_database()
    print(f"Database initialized at {DEFAULT_DB_PATH}")
