#!/usr/bin/env python3
"""
Bluesky Poller for Aerie

Polls a Bluesky account's home timeline and stores posts in the Aerie database
for LLM classification alongside Twitter content.

Usage:
    # Set credentials via environment variables
    export BLUESKY_HANDLE="yourhandle.bsky.social"
    export BLUESKY_APP_PASSWORD="xxxx-xxxx-xxxx-xxxx"

    # Run the poller
    python bluesky_poller.py              # Poll once
    python bluesky_poller.py --watch      # Poll continuously
    python bluesky_poller.py --watch --interval 60  # Poll every 60 seconds
"""

import argparse
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from atproto import Client
from atproto_client.exceptions import UnauthorizedError

from database import (
    DEFAULT_DB_PATH,
    init_database,
    store_retweets,
    store_tweets,
)

# Platform identifier for Bluesky posts
PLATFORM = "bluesky"


def get_credentials() -> tuple[str, str]:
    """Get Bluesky credentials from environment variables."""
    handle = os.environ.get("BLUESKY_HANDLE")
    app_password = os.environ.get("BLUESKY_APP_PASSWORD")

    if not handle or not app_password:
        raise ValueError(
            "Missing credentials. Set BLUESKY_HANDLE and BLUESKY_APP_PASSWORD environment variables.\n"
            "Create an app password at: https://bsky.app/settings/app-passwords"
        )

    return handle, app_password


def create_client() -> Client:
    """Create and authenticate a Bluesky client."""
    handle, app_password = get_credentials()
    client = Client()

    try:
        client.login(handle, app_password)
        print(f"[Bluesky] Logged in as {handle}")
    except UnauthorizedError as e:
        raise ValueError(f"Authentication failed: {e}. Check your credentials.") from e

    return client


def normalize_post(feed_item: Any, client: Client) -> tuple[dict | None, dict | None]:
    """
    Normalize a Bluesky feed item to Aerie's tweet schema.

    Args:
        feed_item: A FeedViewPost from getTimeline response
        client: Authenticated Bluesky client (for resolving references)

    Returns:
        Tuple of (post_dict, repost_dict) where:
        - post_dict: Normalized post for tweets table (or None if should skip)
        - repost_dict: Repost record if this is a repost (or None)
    """
    post = feed_item.post
    reason = getattr(feed_item, "reason", None)

    # Check if this is a repost (reason will have a "by" attribute if so)
    is_repost = reason is not None and hasattr(reason, "by")

    # Extract post record
    record = post.record

    # Skip posts without text (shouldn't happen, but be safe)
    if not hasattr(record, "text"):
        return None, None

    # Build the Aerie post ID
    post_id = f"bsky:{post.uri}"

    # Extract author info
    author = post.author

    # Check for domain verification (custom domain = verified)
    # Default handles end in .bsky.social
    is_domain_verified = not author.handle.endswith(".bsky.social")

    # Extract reply info
    reply_to_id = None
    root_uri = None
    if hasattr(record, "reply") and record.reply:
        # Bluesky has both parent and root
        if record.reply.parent:
            reply_to_id = f"bsky:{record.reply.parent.uri}"
        if record.reply.root:
            root_uri = record.reply.root.uri

    # Extract quote info
    quoted_post_id = None
    is_quote = False
    if hasattr(record, "embed") and record.embed:
        embed = record.embed
        # Check for quote post (embed.record or embed.recordWithMedia)
        if hasattr(embed, "record") and embed.record:
            is_quote = True
            if hasattr(embed.record, "uri"):
                quoted_post_id = f"bsky:{embed.record.uri}"

    # Extract engagement metrics
    like_count = getattr(post, "like_count", 0) or 0
    repost_count = getattr(post, "repost_count", 0) or 0
    reply_count = getattr(post, "reply_count", 0) or 0
    quote_count = getattr(post, "quote_count", 0) or 0

    # Extract labels (content warnings, etc.)
    labels = []
    if hasattr(post, "labels") and post.labels:
        labels = [label.val for label in post.labels]

    # Extract language tags
    langs = getattr(record, "langs", None) or []

    # Build platform metadata
    platform_metadata = {
        "cid": str(post.cid),
        "root_uri": root_uri,
        "labels": labels,
        "langs": langs,
    }

    # Extract media info
    media = []
    if hasattr(record, "embed") and record.embed:
        embed = record.embed
        if hasattr(embed, "images") and embed.images:
            for img in embed.images:
                media.append({
                    "type": "image",
                    "alt": getattr(img, "alt", ""),
                })
        elif hasattr(embed, "external") and embed.external:
            media.append({
                "type": "link",
                "uri": getattr(embed.external, "uri", ""),
                "title": getattr(embed.external, "title", ""),
            })

    # Build the normalized post
    post_dict = {
        "id": post_id,
        "text": record.text,
        "created_at": record.created_at,
        "platform": PLATFORM,
        "platform_metadata": platform_metadata,
        "author": {
            "id": author.did,
            "username": author.handle,
            "display_name": getattr(author, "display_name", None) or author.handle,
            "verified": is_domain_verified,
            "bio": getattr(author, "description", None),
            "followers_count": getattr(author, "followers_count", None),
            "following": None,  # Not easily available in feed response
        },
        "metrics": {
            "like_count": like_count,
            "repost_count": repost_count,
            "reply_count": reply_count,
            "quote_count": quote_count,
        },
        "reply_to": {"tweet_id": reply_to_id} if reply_to_id else {},
        "is_retweet": False,  # The post itself isn't a retweet
        "is_quote": is_quote,
        "quoted_tweet_id": quoted_post_id,
        "media": media,
    }

    # Build repost record if this is a repost
    repost_dict = None
    if is_repost and reason is not None:
        reposter = reason.by
        repost_dict = {
            "original_tweet_id": post_id,
            "retweeter_user_id": reposter.did,
            "retweeter_username": reposter.handle,
            "retweeter_display_name": getattr(reposter, "display_name", None) or reposter.handle,
            "retweeted_at": getattr(reason, "indexed_at", datetime.utcnow().isoformat()),
            "platform": PLATFORM,
        }

    return post_dict, repost_dict


def fetch_timeline(
    client: Client,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[Any], str | None]:
    """
    Fetch the home timeline from Bluesky.

    Args:
        client: Authenticated Bluesky client
        limit: Maximum number of posts to fetch (max 100)
        cursor: Pagination cursor from previous request

    Returns:
        Tuple of (list of FeedViewPost items, next cursor)
    """
    response = client.get_timeline(limit=min(limit, 100), cursor=cursor)
    return response.feed, getattr(response, "cursor", None)


def poll_and_store(
    client: Client,
    db_path: Path = DEFAULT_DB_PATH,
    limit: int = 50,
    verbose: bool = False,
) -> dict:
    """
    Poll Bluesky timeline and store posts in the database.

    Args:
        client: Authenticated Bluesky client
        db_path: Path to the database
        limit: Maximum number of posts to fetch
        verbose: Print detailed output

    Returns:
        Stats dict with counts of inserted/duplicates
    """
    feed, _ = fetch_timeline(client, limit=limit)

    posts = []
    reposts = []
    skipped = 0

    for item in feed:
        try:
            post_dict, repost_dict = normalize_post(item, client)

            if post_dict:
                posts.append(post_dict)
                if verbose:
                    author = post_dict["author"]["username"]
                    text_preview = post_dict["text"][:50].replace("\n", " ")
                    print(f"  @{author}: {text_preview}...")

            if repost_dict:
                reposts.append(repost_dict)

        except Exception as e:
            if verbose:
                print(f"  [Warning] Failed to normalize post: {e}")
            skipped += 1

    # Store in database
    post_result = store_tweets(posts, db_path) if posts else {"inserted": 0, "duplicates": 0}
    repost_result = store_retweets(reposts, db_path) if reposts else {"inserted": 0, "duplicates": 0}

    return {
        "posts_fetched": len(feed),
        "posts_inserted": post_result["inserted"],
        "posts_duplicates": post_result["duplicates"],
        "reposts_inserted": repost_result["inserted"],
        "reposts_duplicates": repost_result["duplicates"],
        "skipped": skipped,
    }


def run_poller(
    db_path: Path = DEFAULT_DB_PATH,
    watch: bool = False,
    interval: int = 120,
    limit: int = 50,
    verbose: bool = False,
):
    """
    Run the Bluesky poller.

    Args:
        db_path: Path to the database
        watch: If True, poll continuously
        interval: Seconds between polls (when watching)
        limit: Maximum posts per poll
        verbose: Print detailed output
    """
    # Initialize database (ensures schema is up to date)
    init_database(db_path)

    # Create authenticated client
    client = create_client()

    def do_poll():
        print(f"[Bluesky] Polling timeline (limit={limit})...")
        stats = poll_and_store(client, db_path, limit=limit, verbose=verbose)
        print(
            f"[Bluesky] Fetched {stats['posts_fetched']} posts: "
            f"{stats['posts_inserted']} new, {stats['posts_duplicates']} existing"
        )
        if stats["reposts_inserted"] > 0:
            print(f"[Bluesky] Recorded {stats['reposts_inserted']} reposts")
        if stats["skipped"] > 0:
            print(f"[Bluesky] Skipped {stats['skipped']} posts (normalization errors)")
        return stats

    if watch:
        print(f"[Bluesky] Starting continuous polling (interval={interval}s)")
        print("[Bluesky] Press Ctrl+C to stop")
        try:
            while True:
                do_poll()
                print(f"[Bluesky] Sleeping {interval}s...")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[Bluesky] Stopped")
    else:
        do_poll()


def main():
    parser = argparse.ArgumentParser(
        description="Poll Bluesky timeline and store posts in Aerie database"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Path to database (default: ../tweets.db)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Poll continuously instead of once",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=120,
        help="Seconds between polls when watching (default: 120)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum posts to fetch per poll (default: 50, max: 100)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print detailed output",
    )

    args = parser.parse_args()

    try:
        run_poller(
            db_path=args.db,
            watch=args.watch,
            interval=args.interval,
            limit=args.limit,
            verbose=args.verbose,
        )
    except ValueError as e:
        print(f"[Error] {e}")
        return 1
    except Exception as e:
        print(f"[Error] Unexpected error: {e}")
        raise


if __name__ == "__main__":
    main()
