"""Test fixture helpers for creating tweets and test data."""

from datetime import datetime
from pathlib import Path

from database import store_mode_decisions_batch, store_tweets


def make_tweet(
    id: str,
    text: str = "Test tweet",
    author_username: str = "testuser",
    author_display_name: str = "Test User",
    reply_to_tweet_id: str | None = None,
    reply_to_username: str | None = None,
    quoted_tweet_id: str | None = None,
    created_at: str | None = None,
    **kwargs,
) -> dict:
    """
    Create a tweet dict with sensible defaults for testing.

    Args:
        id: Tweet ID (required)
        text: Tweet text content
        author_username: Author's @handle
        author_display_name: Author's display name
        reply_to_tweet_id: ID of tweet this is replying to (for threads)
        reply_to_username: Username of tweet being replied to
        quoted_tweet_id: ID of quoted tweet (for quote tweets)
        created_at: ISO timestamp (defaults to now)
        **kwargs: Additional fields to include

    Returns:
        Tweet dict suitable for store_tweets()
    """
    if created_at is None:
        created_at = datetime.utcnow().isoformat()

    tweet = {
        "id": id,
        "text": text,
        "author": {
            "username": author_username,
            "display_name": author_display_name,
        },
        "created_at": created_at,
    }

    # store_tweets expects reply info in a nested 'reply_to' dict
    if reply_to_tweet_id:
        tweet["reply_to"] = {
            "tweet_id": reply_to_tweet_id,
            "username": reply_to_username or author_username,
        }
    if quoted_tweet_id:
        tweet["quoted_tweet_id"] = quoted_tweet_id

    tweet.update(kwargs)
    return tweet


def make_thread(base_id: int, length: int, author: str = "testuser") -> list[dict]:
    """
    Create a thread of tweets where each replies to the previous.

    Args:
        base_id: Starting ID number (will be converted to string)
        length: Number of tweets in the thread
        author: Author username for all tweets

    Returns:
        List of tweet dicts, oldest first (A -> B -> C)
    """
    tweets = []
    for i in range(length):
        tweet_id = str(base_id + i)
        reply_to = str(base_id + i - 1) if i > 0 else None

        tweets.append(
            make_tweet(
                id=tweet_id,
                text=f"Thread tweet {i + 1}",
                author_username=author,
                reply_to_tweet_id=reply_to,
                reply_to_username=author if reply_to else None,
            )
        )

    return tweets


def approve_tweets(
    tweet_ids: list[str],
    db_path: Path,
    mode_id: str = "default",
) -> None:
    """
    Mark tweets as approved in the mode_decisions table.

    Args:
        tweet_ids: List of tweet IDs to approve
        db_path: Path to test database
        mode_id: Mode to approve for (default: "default")
    """
    decisions = [
        {
            "tweet_id": tid,
            "mode_id": mode_id,
            "decision": "approved",
            "source": "extractor",  # Must be 'prefilter' or 'extractor' per DB constraint
        }
        for tid in tweet_ids
    ]
    store_mode_decisions_batch(decisions, db_path)


def filter_tweets(
    tweet_ids: list[str],
    db_path: Path,
    mode_id: str = "default",
) -> None:
    """
    Mark tweets as filtered in the mode_decisions table.

    Args:
        tweet_ids: List of tweet IDs to filter
        db_path: Path to test database
        mode_id: Mode to filter for (default: "default")
    """
    decisions = [
        {
            "tweet_id": tid,
            "mode_id": mode_id,
            "decision": "filtered",
            "source": "extractor",  # Must be 'prefilter' or 'extractor' per DB constraint
        }
        for tid in tweet_ids
    ]
    store_mode_decisions_batch(decisions, db_path)


def create_and_store_tweets(tweets: list[dict], db_path: Path) -> None:
    """
    Store tweets in the database.

    Args:
        tweets: List of tweet dicts (from make_tweet)
        db_path: Path to test database
    """
    store_tweets(tweets, db_path)
