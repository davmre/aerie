"""
Conversation chain operations.

Provides operations for building thread context and conversation chains for display.
"""

from pathlib import Path
from typing import Any

from database.core import DEFAULT_DB_PATH, decode_cursor, encode_cursor, transaction

# =============================================================================
# Thread Context
# =============================================================================


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


def get_direct_replies(tweet_id: str, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
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
    platform_params: list[str] = []
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
            newest_cursor = encode_cursor(first_root.get(sort_field) or "", first_root["id"])

            # Oldest cursor points to the last (oldest) item
            last_root = roots[-1]
            oldest_cursor = encode_cursor(last_root.get(sort_field) or "", last_root["id"])

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
        root_row = conn.execute("SELECT * FROM tweets WHERE id = ?", (root_id,)).fetchone()
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
) -> dict[str, Any]:
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
        current_id: str | None = root_id

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
) -> dict[str, Any]:
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
        for root_id, tweet_ids_list in chains_by_root.items():
            # Sort tweets chronologically
            chain_tweets = [tweets_by_id[tid] for tid in tweet_ids_list]
            chain_tweets.sort(key=lambda t: t.get("created_at") or "")

            # Check if we need to fetch classified ancestors for context
            root_tweet = tweets_by_id[root_id]
            ancestor_context: list[dict] = []

            # Handle both database format and make_tweet format
            parent_id = root_tweet.get("reply_to_tweet_id")
            if not parent_id and "reply_to" in root_tweet:
                parent_id = root_tweet.get("reply_to", {}).get("tweet_id")

            if parent_id:
                # This thread has classified ancestors - fetch them for context
                current_parent_id = parent_id
                visited: set[str] = set()

                while current_parent_id and current_parent_id not in visited:
                    visited.add(current_parent_id)

                    # Fetch parent tweet
                    parent_row = conn.execute(
                        "SELECT * FROM tweets WHERE id = ?", (current_parent_id,)
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
            quoted_ids = [t.get("quoted_tweet_id") for t in full_chain if t.get("quoted_tweet_id")]
            if quoted_ids:
                placeholders = ",".join("?" * len(quoted_ids))
                quoted_rows = conn.execute(
                    f"SELECT * FROM tweets WHERE id IN ({placeholders})", quoted_ids
                ).fetchall()
                quoted_by_id = {row["id"]: dict(row) for row in quoted_rows}

                for tweet in full_chain:
                    qid = tweet.get("quoted_tweet_id")
                    if qid and qid in quoted_by_id:
                        tweet["quoted_tweet"] = quoted_by_id[qid]

            chains.append({
                "tweets": full_chain,
                "unclassified_ids": set(tweet_ids_list),
            })

    return chains
