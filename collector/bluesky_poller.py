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
from atproto_client.exceptions import RequestException, UnauthorizedError

from database import (
    DEFAULT_DB_PATH,
    get_bluesky_post_ids,
    get_setting,
    init_database,
    store_retweets,
    store_tweets,
)

# Platform identifier for Bluesky posts
PLATFORM = "bluesky"


def get_credentials(db_path: Path = DEFAULT_DB_PATH) -> tuple[str, str]:
    """
    Get Bluesky credentials from environment variables or database settings.

    Priority: Environment variables > Database settings
    """
    # Try environment variables first
    handle = os.environ.get("BLUESKY_HANDLE")
    app_password = os.environ.get("BLUESKY_APP_PASSWORD")

    # Fall back to database settings
    if not handle:
        handle = get_setting("bluesky_handle", db_path)
    if not app_password:
        app_password = get_setting("bluesky_password", db_path)

    if not handle or not app_password:
        raise ValueError(
            "Missing credentials. Either:\n"
            "  1. Set BLUESKY_HANDLE and BLUESKY_APP_PASSWORD environment variables, or\n"
            "  2. Configure credentials in the web UI at /ui/settings\n"
            "Create an app password at: https://bsky.app/settings/app-passwords"
        )

    return handle, app_password


class AuthenticationError(Exception):
    """Non-recoverable authentication error. Do not retry."""
    pass


def create_client(db_path: Path = DEFAULT_DB_PATH) -> Client:
    """Create and authenticate a Bluesky client."""
    handle, app_password = get_credentials(db_path)
    client = Client()

    try:
        client.login(handle, app_password)
        print(f"[Bluesky] Logged in as {handle}")
    except UnauthorizedError as e:
        raise AuthenticationError(f"Authentication failed: {e}. Check your credentials.") from e
    except RequestException as e:
        if e.response and e.response.status_code == 429:
            reset_time = e.response.headers.get("ratelimit-reset", "unknown")
            raise AuthenticationError(
                f"Rate limited during authentication. "
                f"Too many failed login attempts. Reset at timestamp: {reset_time}. "
                f"Fix your credentials and wait for the rate limit to reset before retrying."
            ) from e
        raise

    return client


def normalize_post(feed_item: Any, client: Client) -> tuple[dict | None, dict | None, dict | None]:
    """
    Normalize a Bluesky feed item to Aerie's tweet schema.

    Args:
        feed_item: A FeedViewPost from getTimeline response
        client: Authenticated Bluesky client (for resolving references)

    Returns:
        Tuple of (post_dict, repost_dict, quoted_post_dict) where:
        - post_dict: Normalized post for tweets table (or None if should skip)
        - repost_dict: Repost record if this is a repost (or None)
        - quoted_post_dict: Quoted post for tweets table (or None if not a quote)
    """
    post = feed_item.post
    reason = getattr(feed_item, "reason", None)

    # Check if this is a repost (reason will have a "by" attribute if so)
    is_repost = reason is not None and hasattr(reason, "by")

    # Extract post record
    record = post.record

    # Skip posts without text (shouldn't happen, but be safe)
    if not hasattr(record, "text"):
        return None, None, None

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

    # Extract quote info and quoted post content
    quoted_post_id = None
    quoted_post_dict = None
    is_quote = False

    # Check for quote embed in the post view (resolved data)
    post_embed = getattr(post, "embed", None)
    if post_embed:
        # Handle app.bsky.embed.record (pure quote)
        quoted_record = getattr(post_embed, "record", None)
        # Handle app.bsky.embed.recordWithMedia (quote with media)
        if not quoted_record and hasattr(post_embed, "record"):
            quoted_record = post_embed.record

        if quoted_record and hasattr(quoted_record, "uri"):
            is_quote = True
            quoted_post_id = f"bsky:{quoted_record.uri}"

            # Extract the quoted post's content to store separately
            # The record view contains author and value (the actual post record)
            quoted_author = getattr(quoted_record, "author", None)
            quoted_value = getattr(quoted_record, "value", None)

            if quoted_author and quoted_value and hasattr(quoted_value, "text"):
                quoted_is_domain_verified = not quoted_author.handle.endswith(".bsky.social")

                # Extract quoted post's metrics if available
                quoted_like_count = getattr(quoted_record, "like_count", 0) or 0
                quoted_repost_count = getattr(quoted_record, "repost_count", 0) or 0
                quoted_reply_count = getattr(quoted_record, "reply_count", 0) or 0

                # Extract quoted post's reply info
                quoted_reply_to_id = None
                quoted_root_uri = None
                if hasattr(quoted_value, "reply") and quoted_value.reply:
                    if quoted_value.reply.parent:
                        quoted_reply_to_id = f"bsky:{quoted_value.reply.parent.uri}"
                    if quoted_value.reply.root:
                        quoted_root_uri = quoted_value.reply.root.uri

                # Extract URLs from quoted post facets
                quoted_urls = []
                if hasattr(quoted_value, "facets") and quoted_value.facets:
                    quoted_text_bytes = quoted_value.text.encode("utf-8")
                    for facet in quoted_value.facets:
                        features = getattr(facet, "features", []) or []
                        for feature in features:
                            feature_type = getattr(feature, "py_type", None) or getattr(feature, "$type", "")
                            if "link" in str(feature_type).lower():
                                uri = getattr(feature, "uri", None)
                                if uri:
                                    index = getattr(facet, "index", None)
                                    if index:
                                        byte_start = getattr(index, "byte_start", 0)
                                        byte_end = getattr(index, "byte_end", len(quoted_text_bytes))
                                        display_text = quoted_text_bytes[byte_start:byte_end].decode("utf-8", errors="replace")
                                    else:
                                        display_text = uri
                                    quoted_urls.append({
                                        "url": display_text,
                                        "expanded_url": uri,
                                        "display_url": display_text,
                                    })

                # Extract media from quoted post's embeds (resolved views)
                quoted_media = []
                quoted_embeds = getattr(quoted_record, "embeds", None) or []
                for embed in quoted_embeds:
                    # Handle images embed
                    if hasattr(embed, "images") and embed.images:
                        for img in embed.images:
                            thumb = getattr(img, "thumb", None)
                            fullsize = getattr(img, "fullsize", None)
                            if thumb or fullsize:
                                quoted_media.append({
                                    "type": "photo",
                                    "url": thumb or fullsize,
                                    "expanded_url": fullsize or thumb,
                                    "alt": getattr(img, "alt", ""),
                                })

                # Build platform metadata for quoted post
                quoted_cid = getattr(quoted_record, "cid", None)
                quoted_platform_metadata = {
                    "cid": str(quoted_cid) if quoted_cid else None,
                    "root_uri": quoted_root_uri,
                    "labels": [],
                    "langs": getattr(quoted_value, "langs", None) or [],
                }

                quoted_post_dict = {
                    "id": quoted_post_id,
                    "text": quoted_value.text,
                    "created_at": getattr(quoted_value, "created_at", None),
                    "platform": PLATFORM,
                    "platform_metadata": quoted_platform_metadata,
                    "author": {
                        "id": quoted_author.did,
                        "username": quoted_author.handle,
                        "display_name": getattr(quoted_author, "display_name", None) or quoted_author.handle,
                        "verified": quoted_is_domain_verified,
                        "bio": getattr(quoted_author, "description", None),
                        "followers_count": getattr(quoted_author, "followers_count", None),
                        "following": None,
                    },
                    "metrics": {
                        "like_count": quoted_like_count,
                        "repost_count": quoted_repost_count,
                        "reply_count": quoted_reply_count,
                        "quote_count": 0,
                    },
                    "reply_to": {"tweet_id": quoted_reply_to_id} if quoted_reply_to_id else {},
                    "is_retweet": False,
                    "is_quote": False,
                    "quoted_tweet_id": None,
                    "media": quoted_media,
                    "urls": quoted_urls,
                }

    # Extract URL facets (Bluesky's way of annotating links in text)
    urls = []
    if hasattr(record, "facets") and record.facets:
        text_bytes = record.text.encode("utf-8")
        for facet in record.facets:
            # Check if this facet is a link
            features = getattr(facet, "features", []) or []
            for feature in features:
                feature_type = getattr(feature, "py_type", None) or getattr(feature, "$type", "")
                if "link" in str(feature_type).lower():
                    uri = getattr(feature, "uri", None)
                    if uri:
                        # Extract the display text using byte offsets
                        index = getattr(facet, "index", None)
                        if index:
                            byte_start = getattr(index, "byte_start", 0)
                            byte_end = getattr(index, "byte_end", len(text_bytes))
                            display_text = text_bytes[byte_start:byte_end].decode("utf-8", errors="replace")
                        else:
                            display_text = uri

                        urls.append({
                            "url": display_text,  # The truncated URL shown in text
                            "expanded_url": uri,  # The actual full URL
                            "display_url": display_text,
                        })

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

    # Extract media info from resolved embed view (post.embed has URLs, record.embed has blob refs)
    media = []
    if post_embed:
        # Handle images (app.bsky.embed.images#view)
        if hasattr(post_embed, "images") and post_embed.images:
            for img in post_embed.images:
                # post.embed images have thumb and fullsize URLs
                thumb = getattr(img, "thumb", None)
                fullsize = getattr(img, "fullsize", None)
                if thumb or fullsize:
                    media.append({
                        "type": "photo",  # Match Twitter's format for renderMedia()
                        "url": thumb or fullsize,  # Display URL (thumbnail)
                        "expanded_url": fullsize or thumb,  # Click opens fullsize
                        "alt": getattr(img, "alt", ""),
                    })
        # Handle recordWithMedia (quote with images) - images are in media.images
        elif hasattr(post_embed, "media") and post_embed.media:
            embed_media = post_embed.media
            if hasattr(embed_media, "images") and embed_media.images:
                for img in embed_media.images:
                    thumb = getattr(img, "thumb", None)
                    fullsize = getattr(img, "fullsize", None)
                    if thumb or fullsize:
                        media.append({
                            "type": "photo",
                            "url": thumb or fullsize,
                            "expanded_url": fullsize or thumb,
                            "alt": getattr(img, "alt", ""),
                        })
        # Handle external links (app.bsky.embed.external#view)
        elif hasattr(post_embed, "external") and post_embed.external:
            external = post_embed.external
            media.append({
                "type": "link",
                "uri": getattr(external, "uri", ""),
                "title": getattr(external, "title", ""),
                "thumb": getattr(external, "thumb", ""),  # External embeds can have thumbnails
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
        "urls": urls,
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

    return post_dict, repost_dict, quoted_post_dict


def normalize_thread_view_post(thread_post: Any) -> dict | None:
    """
    Normalize a ThreadViewPost (from get_post_thread) to Aerie's tweet schema.

    This is similar to normalize_post() but for thread context posts which
    have a simpler structure (no repost handling needed).

    Args:
        thread_post: A ThreadViewPost from get_post_thread response

    Returns:
        Normalized post dict for tweets table, or None if should skip
    """
    # Handle blocked/notFound posts
    if hasattr(thread_post, "py_type"):
        type_str = str(thread_post.py_type)
        if "notFoundPost" in type_str or "blockedPost" in type_str:
            return None

    # thread_post.post is a PostView
    post = getattr(thread_post, "post", None)
    if not post:
        return None

    record = getattr(post, "record", None)
    if not record or not hasattr(record, "text"):
        return None

    # Build the Aerie post ID
    post_id = f"bsky:{post.uri}"

    # Extract author info
    author = post.author
    is_domain_verified = not author.handle.endswith(".bsky.social")

    # Extract reply info
    reply_to_id = None
    root_uri = None
    if hasattr(record, "reply") and record.reply:
        if record.reply.parent:
            reply_to_id = f"bsky:{record.reply.parent.uri}"
        if record.reply.root:
            root_uri = record.reply.root.uri

    # Extract quote info (simplified - we don't recursively fetch quoted posts)
    quoted_post_id = None
    is_quote = False
    post_embed = getattr(post, "embed", None)
    if post_embed:
        quoted_record = getattr(post_embed, "record", None)
        if quoted_record and hasattr(quoted_record, "uri"):
            is_quote = True
            quoted_post_id = f"bsky:{quoted_record.uri}"

    # Extract URL facets
    urls = []
    if hasattr(record, "facets") and record.facets:
        text_bytes = record.text.encode("utf-8")
        for facet in record.facets:
            features = getattr(facet, "features", []) or []
            for feature in features:
                feature_type = getattr(feature, "py_type", None) or getattr(feature, "$type", "")
                if "link" in str(feature_type).lower():
                    uri = getattr(feature, "uri", None)
                    if uri:
                        index = getattr(facet, "index", None)
                        if index:
                            byte_start = getattr(index, "byte_start", 0)
                            byte_end = getattr(index, "byte_end", len(text_bytes))
                            display_text = text_bytes[byte_start:byte_end].decode("utf-8", errors="replace")
                        else:
                            display_text = uri
                        urls.append({
                            "url": display_text,
                            "expanded_url": uri,
                            "display_url": display_text,
                        })

    # Extract engagement metrics
    like_count = getattr(post, "like_count", 0) or 0
    repost_count = getattr(post, "repost_count", 0) or 0
    reply_count = getattr(post, "reply_count", 0) or 0
    quote_count = getattr(post, "quote_count", 0) or 0

    # Extract labels
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
    if post_embed:
        if hasattr(post_embed, "images") and post_embed.images:
            for img in post_embed.images:
                thumb = getattr(img, "thumb", None)
                fullsize = getattr(img, "fullsize", None)
                if thumb or fullsize:
                    media.append({
                        "type": "photo",
                        "url": thumb or fullsize,
                        "expanded_url": fullsize or thumb,
                        "alt": getattr(img, "alt", ""),
                    })
        elif hasattr(post_embed, "media") and post_embed.media:
            embed_media = post_embed.media
            if hasattr(embed_media, "images") and embed_media.images:
                for img in embed_media.images:
                    thumb = getattr(img, "thumb", None)
                    fullsize = getattr(img, "fullsize", None)
                    if thumb or fullsize:
                        media.append({
                            "type": "photo",
                            "url": thumb or fullsize,
                            "expanded_url": fullsize or thumb,
                            "alt": getattr(img, "alt", ""),
                        })
        elif hasattr(post_embed, "external") and post_embed.external:
            external = post_embed.external
            media.append({
                "type": "link",
                "uri": getattr(external, "uri", ""),
                "title": getattr(external, "title", ""),
                "thumb": getattr(external, "thumb", ""),
            })

    return {
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
            "following": None,
        },
        "metrics": {
            "like_count": like_count,
            "repost_count": repost_count,
            "reply_count": reply_count,
            "quote_count": quote_count,
        },
        "reply_to": {"tweet_id": reply_to_id} if reply_to_id else {},
        "is_retweet": False,
        "is_quote": is_quote,
        "quoted_tweet_id": quoted_post_id,
        "media": media,
        "urls": urls,
    }


def fetch_thread_context(
    client: Client,
    reply_uri: str,
    existing_ids: set[str],
    max_depth: int = 50,
    verbose: bool = False,
) -> list[dict]:
    """
    Fetch the parent chain for a reply post.

    Uses get_post_thread() to fetch the entire parent chain in one API call,
    then walks up the chain until we reach a post we already have.

    Args:
        client: Authenticated Bluesky client
        reply_uri: URI of the reply post (with or without 'bsky:' prefix)
        existing_ids: Set of post IDs we already have (for deduplication)
        max_depth: Maximum parent depth to fetch (passed to API)
        verbose: Print debug output

    Returns:
        List of parent posts in chronological order (oldest first)
    """
    # Strip bsky: prefix if present
    uri = reply_uri[5:] if reply_uri.startswith("bsky:") else reply_uri

    try:
        response = client.get_post_thread(uri=uri, parent_height=max_depth)
    except Exception as e:
        if verbose:
            print(f"    [Warning] Failed to fetch thread for {uri}: {e}")
        return []

    thread = getattr(response, "thread", None)
    if not thread:
        return []

    # First, include the post we queried (the immediate parent of our reply)
    # This is thread itself, not thread.parent
    parents = []

    # Check if the queried post should be included
    thread_post = getattr(thread, "post", None)
    if thread_post:
        thread_post_id = f"bsky:{thread_post.uri}"
        if thread_post_id not in existing_ids:
            normalized = normalize_thread_view_post(thread)
            if normalized:
                parents.append(normalized)
                existing_ids.add(thread_post_id)

    # Walk up thread.parent chain, collecting ancestor posts
    current = getattr(thread, "parent", None)

    while current:
        # Check for blocked/notFound posts
        if hasattr(current, "py_type"):
            type_str = str(current.py_type)
            if "notFoundPost" in type_str or "blockedPost" in type_str:
                break

        post = getattr(current, "post", None)
        if not post:
            break

        post_id = f"bsky:{post.uri}"

        # Stop if we already have this post
        if post_id in existing_ids:
            break

        normalized = normalize_thread_view_post(current)
        if normalized:
            parents.insert(0, normalized)  # Prepend for chronological order
            existing_ids.add(post_id)  # Mark as seen to avoid re-fetching

        # Move to next parent
        current = getattr(current, "parent", None)

    return parents


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
    fetch_threads: bool = True,
) -> dict:
    """
    Poll Bluesky timeline and store posts in the database.

    Args:
        client: Authenticated Bluesky client
        db_path: Path to the database
        limit: Maximum number of posts to fetch
        verbose: Print detailed output
        fetch_threads: If True, fetch parent posts for replies (thread context)

    Returns:
        Stats dict with counts of inserted/duplicates
    """
    feed, _ = fetch_timeline(client, limit=limit)

    posts = []
    reposts = []
    quoted_posts = []
    skipped = 0

    for item in feed:
        try:
            post_dict, repost_dict, quoted_post_dict = normalize_post(item, client)

            # Store quoted post first (so it exists when the quoting post references it)
            if quoted_post_dict:
                quoted_posts.append(quoted_post_dict)
                if verbose:
                    author = quoted_post_dict["author"]["username"]
                    text_preview = quoted_post_dict["text"][:30].replace("\n", " ")
                    print(f"  [quoted] @{author}: {text_preview}...")

            if post_dict:
                posts.append(post_dict)
                if verbose:
                    author = post_dict["author"]["username"]
                    text_preview = post_dict["text"][:50].replace("\n", " ")
                    qt_marker = " (quote)" if quoted_post_dict else ""
                    print(f"  @{author}: {text_preview}...{qt_marker}")

            if repost_dict:
                reposts.append(repost_dict)

        except Exception as e:
            if verbose:
                print(f"  [Warning] Failed to normalize post: {e}")
            skipped += 1

    # Fetch thread context for replies with missing parents
    thread_context_posts: list[dict] = []
    if fetch_threads:
        # Collect reply URIs that point to posts we don't have
        reply_uris = [
            p["reply_to"]["tweet_id"]
            for p in posts
            if p.get("reply_to", {}).get("tweet_id")
        ]

        if reply_uris:
            # Build set of existing IDs (from DB + current batch)
            existing_ids = get_bluesky_post_ids(db_path)
            existing_ids.update(p["id"] for p in posts)
            existing_ids.update(p["id"] for p in quoted_posts)

            # Fetch thread context for each unique missing parent
            fetched_uris: set[str] = set()
            for uri in reply_uris:
                if uri not in existing_ids and uri not in fetched_uris:
                    fetched_uris.add(uri)
                    parents = fetch_thread_context(
                        client, uri, existing_ids, verbose=verbose
                    )
                    if parents:
                        thread_context_posts.extend(parents)
                        if verbose:
                            for p in parents:
                                author = p["author"]["username"]
                                text_preview = p["text"][:40].replace("\n", " ")
                                print(f"  [thread] @{author}: {text_preview}...")
                    # Brief pause between API calls for rate limiting
                    time.sleep(0.1)

    # Store in order: thread context -> quoted -> main posts
    thread_result = (
        store_tweets(thread_context_posts, db_path)
        if thread_context_posts
        else {"inserted": 0, "duplicates": 0}
    )

    # Store quoted posts (so foreign key references work)
    quoted_result = (
        store_tweets(quoted_posts, db_path)
        if quoted_posts
        else {"inserted": 0, "duplicates": 0}
    )

    # Store main posts
    post_result = (
        store_tweets(posts, db_path)
        if posts
        else {"inserted": 0, "duplicates": 0}
    )
    repost_result = (
        store_retweets(reposts, db_path)
        if reposts
        else {"inserted": 0, "duplicates": 0}
    )

    return {
        "posts_fetched": len(feed),
        "posts_inserted": post_result["inserted"],
        "posts_duplicates": post_result["duplicates"],
        "quoted_posts_inserted": quoted_result["inserted"],
        "quoted_posts_duplicates": quoted_result["duplicates"],
        "thread_context_inserted": thread_result["inserted"],
        "thread_context_duplicates": thread_result["duplicates"],
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
    client = create_client(db_path)

    def do_poll():
        print(f"[Bluesky] Polling timeline (limit={limit})...")
        stats = poll_and_store(client, db_path, limit=limit, verbose=verbose)
        print(
            f"[Bluesky] Fetched {stats['posts_fetched']} posts: "
            f"{stats['posts_inserted']} new, {stats['posts_duplicates']} existing"
        )
        if stats.get("thread_context_inserted", 0) > 0:
            print(f"[Bluesky] Fetched {stats['thread_context_inserted']} thread context posts")
        if stats["quoted_posts_inserted"] > 0:
            print(f"[Bluesky] Stored {stats['quoted_posts_inserted']} quoted posts")
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
        help="Path to database (default: AERIE_DB_PATH env var or ../tweets.db)",
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
    except AuthenticationError as e:
        # Auth errors are not recoverable by retrying - exit cleanly
        print(f"[Error] {e}")
        print("[Error] This is not recoverable by retrying. Fix credentials and restart manually.")
        return 1
    except ValueError as e:
        print(f"[Error] {e}")
        return 1
    except Exception as e:
        print(f"[Error] Unexpected error: {e}")
        raise


if __name__ == "__main__":
    main()
