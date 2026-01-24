"""
Tweet storage and retrieval operations.

Provides CRUD operations for tweets and retweets.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from database.core import DEFAULT_DB_PATH, parse_twitter_date, transaction


def store_tweets(tweets: list[dict], db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
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
    inserted_tweets: list[dict] = []

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


def store_retweets(retweets: list[dict], db_path: Path = DEFAULT_DB_PATH) -> dict[str, int]:
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

        result: dict[str, list[dict]] = {}
        for row in rows:
            tid = row["original_tweet_id"]
            if tid not in result:
                result[tid] = []
            result[tid].append(dict(row))
        return result


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
        rows = conn.execute("SELECT id FROM tweets WHERE platform = 'bluesky'").fetchall()
        return {row["id"] for row in rows}


def get_tweets_by_author(
    author_username: str,
    limit: int = 10,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """
    Get recent tweets by a specific author.

    Args:
        author_username: The author's username
        limit: Maximum number of tweets to return
        db_path: Database path

    Returns:
        List of tweet dicts, ordered by created_at DESC
    """
    with transaction(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM tweets
            WHERE author_username = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (author_username, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def get_recent_tweets_for_context(
    hours: int = 48,
    limit: int = 500,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """
    Get recent tweets for context generation, with quoted tweets attached.

    Args:
        hours: Look back this many hours
        limit: Maximum number of tweets to return
        db_path: Database path

    Returns:
        List of tweet dicts with quoted_tweet populated, ordered by created_at DESC
    """
    with transaction(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM tweets
            WHERE created_at >= datetime('now', ?)
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (f"-{hours} hours", limit),
        ).fetchall()
        tweets = [dict(row) for row in rows]

        # Collect quoted tweet IDs and fetch them
        quoted_ids = [t["quoted_tweet_id"] for t in tweets if t.get("quoted_tweet_id")]
        if quoted_ids:
            placeholders = ",".join("?" * len(quoted_ids))
            quoted_rows = conn.execute(
                f"SELECT * FROM tweets WHERE id IN ({placeholders})",
                quoted_ids,
            ).fetchall()
            quoted_by_id = {row["id"]: dict(row) for row in quoted_rows}

            # Attach quoted tweets
            for tweet in tweets:
                if tweet.get("quoted_tweet_id"):
                    tweet["quoted_tweet"] = quoted_by_id.get(tweet["quoted_tweet_id"])

        return tweets
