#!/usr/bin/env python3
"""
Context Updater for Aerie Tweet Classification

Manages situational context that helps LLMs make better classification decisions.
Context is maintained as a freeform text document describing current topics,
events, and discussions on the timeline.

Usage:
    # Update context using LLM
    python context_updater.py refresh

    # View current context
    python context_updater.py show

    # Manually set context
    python context_updater.py set "Description of current events..."
"""

import argparse
import re
from pathlib import Path

from database import (
    DEFAULT_DB_PATH,
    create_context,
    get_current_context,
    get_recent_tweets_for_context,
    get_tweets_by_author,
    init_database,
    list_contexts,
    set_current_context,
)
from providers import get_default_provider, get_provider

# =============================================================================
# Context Update Prompt
# =============================================================================

CONTEXT_UPDATE_PROMPT = """You are maintaining a situational context document for a tweet classifier.
Your job is to summarize what's currently being discussed on the timeline.

CURRENT CONTEXT (may be empty if first run):
{current_context}

RECENT POSTS (last 24-48 hours):
{recent_posts}

Update the context document. Guidelines:
- Keep it under {token_limit} tokens (roughly {char_limit} characters)
- Focus on topics/events that help interpret ambiguous posts
- Include ongoing discussions, current events, recurring themes
- Note any notable accounts or personas frequently appearing
- Retire topics no longer relevant based on recent posts
- Use whatever structure feels natural (lists, prose, sections)
- Be concise but informative

Return ONLY the updated context text, nothing else."""


# =============================================================================
# Context Update
# =============================================================================


def format_tweets_for_context(tweets: list[dict], max_chars: int = 20000) -> str:
    """
    Format tweets for inclusion in the context update prompt.

    Args:
        tweets: List of tweet dicts
        max_chars: Maximum characters to include

    Returns:
        Formatted string of tweets
    """
    lines = []
    total_chars = 0

    for tweet in tweets:
        author = tweet.get("author_username") or "unknown"
        text = tweet.get("text", "")[:500]  # Truncate very long tweets
        created_at = tweet.get("created_at", "")[:10]  # Just the date

        line = f"@{author} ({created_at}): {text}"
        line_len = len(line) + 2  # +2 for newlines

        if total_chars + line_len > max_chars:
            break

        lines.append(line)
        total_chars += line_len

    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """
    Estimate token count for text (rough approximation).
    Uses ~4 characters per token as a rule of thumb.
    """
    return len(text) // 4


def update_context(
    provider_name: str | None = None,
    model: str | None = None,
    hours: int = 48,
    token_limit: int = 2000,
    db_path: Path = DEFAULT_DB_PATH,
    verbose: bool = False,
) -> dict:
    """
    Update the context document using an LLM.

    Fetches recent posts, sends them to the LLM along with the current context,
    and asks it to generate an updated context document.

    Args:
        provider_name: LLM provider to use (default: anthropic)
        model: Model to use (default: provider's default)
        hours: Hours of recent posts to include
        token_limit: Target token limit for the context
        db_path: Database path
        verbose: Print detailed output

    Returns:
        Dict with:
        - context_id: ID of the new context
        - text: The context text
        - token_count: Estimated token count
        - previous_context_id: ID of the previous context (if any)
    """
    init_database(db_path)

    # Get provider
    if provider_name is None:
        provider_name = get_default_provider()

    try:
        provider = get_provider(provider_name)
    except ValueError as e:
        return {"error": str(e)}

    api_key = provider.get_api_key()
    if not api_key:
        return {"error": f"API key not set for {provider_name}"}

    actual_model = provider.get_model(model)

    # Get current context
    current_context = get_current_context(db_path)
    current_text = current_context["text"] if current_context else "(No previous context)"
    previous_context_id = current_context["id"] if current_context else None

    if verbose:
        print(f"Previous context: {previous_context_id or 'none'}")
        print(f"Provider: {provider_name}, Model: {actual_model}")

    # Get recent tweets
    tweets = get_recent_tweets_for_context(hours=hours, limit=500, db_path=db_path)
    if verbose:
        print(f"Found {len(tweets)} tweets from last {hours} hours")

    if not tweets:
        return {"error": "No recent tweets found"}

    # Format tweets for the prompt
    tweets_text = format_tweets_for_context(tweets)
    char_limit = token_limit * 4

    # Build the prompt
    prompt = CONTEXT_UPDATE_PROMPT.format(
        current_context=current_text,
        recent_posts=tweets_text,
        token_limit=token_limit,
        char_limit=char_limit,
    )

    if verbose:
        print(f"Prompt size: {len(prompt)} chars")
        print("Calling LLM...")

    # Call the LLM
    try:
        import anthropic
        from anthropic.types import TextBlock

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=actual_model,
            max_tokens=token_limit + 500,  # Allow some buffer
            system="You are a context summarizer. Return only the updated context text.",
            messages=[{"role": "user", "content": prompt}],
        )

        first_block = response.content[0]
        if not isinstance(first_block, TextBlock):
            return {"error": "Unexpected response format from LLM"}

        new_context_text = first_block.text.strip()

    except Exception as e:
        return {"error": f"LLM call failed: {e}"}

    # Estimate token count
    token_count = estimate_tokens(new_context_text)

    if verbose:
        print(f"New context: {len(new_context_text)} chars, ~{token_count} tokens")

    # Create and store the new context
    context_id = create_context(
        text=new_context_text,
        token_count=token_count,
        metadata={"source": "auto", "model": actual_model, "hours": hours},
        db_path=db_path,
    )

    # Set as current context
    set_current_context(context_id, db_path)

    if verbose:
        print(f"Created context: {context_id}")

    return {
        "context_id": context_id,
        "text": new_context_text,
        "token_count": token_count,
        "previous_context_id": previous_context_id,
    }


# =============================================================================
# Phase 2: Context Retrieval for Ambiguous Tweets
# =============================================================================


def retrieve_context_for_tweet(
    tweet: dict,
    db_path: Path = DEFAULT_DB_PATH,
    max_author_posts: int = 10,
    max_similar_posts: int = 5,
) -> str:
    """
    Retrieve additional context for a tweet that needs more information.

    Used in Phase 2 of classification when a tweet is flagged with needs_context=true.

    Args:
        tweet: The tweet dict that needs context
        db_path: Database path
        max_author_posts: Max number of author's other posts to include
        max_similar_posts: Max number of similar posts to include

    Returns:
        Formatted context string with author's posts and similar posts
    """
    parts = []

    # Get author's recent posts
    author = tweet.get("author_username")
    if author:
        author_posts = get_tweets_by_author(author, limit=max_author_posts, db_path=db_path)
        # Exclude the tweet itself
        author_posts = [p for p in author_posts if p.get("id") != tweet.get("id")]

        if author_posts:
            parts.append(f"RECENT POSTS BY @{author}:")
            for post in author_posts:
                text = post.get("text", "")[:300]
                created_at = post.get("created_at", "")[:10]
                parts.append(f"  [{created_at}] {text}")
            parts.append("")

    # Get keyword-similar posts
    tweet_text = tweet.get("text", "")
    if tweet_text:
        similar = find_similar_posts(tweet, limit=max_similar_posts, db_path=db_path)
        if similar:
            parts.append("RELATED POSTS FROM TIMELINE:")
            for post in similar:
                author = post.get("author_username", "unknown")
                text = post.get("text", "")[:300]
                parts.append(f"  @{author}: {text}")
            parts.append("")

    return "\n".join(parts) if parts else ""


def find_similar_posts(
    tweet: dict,
    limit: int = 5,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """
    Find posts similar to the given tweet using keyword overlap.

    Uses simple word overlap scoring - not sophisticated, but fast and
    doesn't require additional dependencies.

    Args:
        tweet: The tweet to find similar posts for
        limit: Maximum number of similar posts to return
        db_path: Database path

    Returns:
        List of similar tweet dicts, scored by keyword overlap
    """
    tweet_text = tweet.get("text", "")
    if not tweet_text:
        return []

    # Extract keywords (simple tokenization)
    keywords = extract_keywords(tweet_text)
    if not keywords:
        return []

    # Get recent tweets to compare
    recent = get_recent_tweets_for_context(hours=48, limit=200, db_path=db_path)

    # Score tweets by keyword overlap
    scored = []
    tweet_id = tweet.get("id")

    for other in recent:
        if other.get("id") == tweet_id:
            continue

        other_text = other.get("text", "")
        other_keywords = extract_keywords(other_text)

        # Calculate Jaccard-like overlap
        overlap = len(keywords & other_keywords)
        if overlap > 0:
            score = overlap / len(keywords | other_keywords)
            scored.append((score, other))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    return [tweet for _, tweet in scored[:limit]]


def extract_keywords(text: str) -> set[str]:
    """
    Extract keywords from text for similarity matching.

    Simple approach: lowercase, split on non-word chars, filter short words
    and common stop words.
    """
    # Common stop words to filter out
    stop_words = {
        "the", "a", "an", "is", "it", "to", "of", "and", "in", "for", "on",
        "with", "as", "at", "by", "this", "that", "from", "or", "be", "are",
        "was", "were", "been", "being", "have", "has", "had", "do", "does",
        "did", "will", "would", "could", "should", "may", "might", "can",
        "just", "so", "but", "if", "then", "than", "too", "very", "only",
        "now", "here", "there", "when", "where", "what", "which", "who",
        "how", "all", "each", "both", "few", "more", "most", "other", "some",
        "such", "no", "not", "nor", "own", "same", "into", "about", "over",
        "after", "before", "between", "under", "again", "out", "up", "down",
        "off", "once", "during", "through", "while", "above", "below",
        "i", "me", "my", "you", "your", "he", "she", "they", "them", "we",
        "our", "his", "her", "its", "their", "us", "im", "ive", "youre",
        "dont", "cant", "wont", "isnt", "arent", "wasnt", "werent", "hasnt",
        "havent", "hadnt", "doesnt", "didnt", "wouldnt", "shouldnt", "couldnt",
        "mightnt", "mustnt", "thats", "whats", "heres", "theres", "wheres",
        "lets", "gonna", "wanna", "gotta", "like", "lol", "lmao", "omg",
        "rt", "via", "amp",
    }

    # Tokenize: lowercase, split on non-word characters
    words = re.findall(r"\b[a-z]{3,}\b", text.lower())

    # Filter stop words
    return {w for w in words if w not in stop_words}


# =============================================================================
# CLI
# =============================================================================


def cmd_refresh(args):
    """Refresh context using LLM."""
    result = update_context(
        provider_name=args.provider,
        model=args.model,
        hours=args.hours,
        token_limit=args.token_limit,
        db_path=args.db,
        verbose=args.verbose,
    )

    if "error" in result:
        print(f"Error: {result['error']}")
        return

    print(f"Context updated: {result['context_id']}")
    print(f"Token count: ~{result['token_count']}")
    print()
    print("New context:")
    print("-" * 40)
    print(result["text"])


def cmd_show(args):
    """Show current context."""
    init_database(args.db)
    context = get_current_context(args.db)

    if not context:
        print("No context set.")
        return

    print(f"Context ID: {context['id']}")
    print(f"Created: {context['created_at']}")
    print(f"Token count: ~{context.get('token_count', 'unknown')}")
    if context.get("metadata"):
        print(f"Metadata: {context['metadata']}")
    print()
    print("Text:")
    print("-" * 40)
    print(context["text"])


def cmd_set(args):
    """Manually set context text."""
    init_database(args.db)

    text = args.text
    token_count = estimate_tokens(text)

    context_id = create_context(
        text=text,
        token_count=token_count,
        metadata={"source": "manual"},
        db_path=args.db,
    )
    set_current_context(context_id, args.db)

    print(f"Context set: {context_id}")
    print(f"Token count: ~{token_count}")


def cmd_history(args):
    """Show context history."""
    init_database(args.db)
    contexts = list_contexts(limit=args.limit, db_path=args.db)

    if not contexts:
        print("No context history.")
        return

    current = get_current_context(args.db)
    current_id = current["id"] if current else None

    print(f"Context history (last {args.limit}):")
    print()

    for ctx in contexts:
        marker = " [current]" if ctx["id"] == current_id else ""
        source = ctx.get("metadata", {}).get("source", "unknown")
        tokens = ctx.get("token_count", "?")
        print(f"  {ctx['id']}{marker}")
        print(f"    Source: {source}, Tokens: ~{tokens}")
        preview = ctx["text"][:100].replace("\n", " ")
        print(f"    Preview: {preview}...")
        print()


def cmd_clear(args):
    """Clear current context."""
    init_database(args.db)
    set_current_context(None, args.db)
    print("Current context cleared.")


def main():
    parser = argparse.ArgumentParser(
        description="Aerie Context Updater",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Database path (default: {DEFAULT_DB_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # refresh command
    refresh_parser = subparsers.add_parser("refresh", help="Update context using LLM")
    refresh_parser.add_argument(
        "--provider", "-p", help="LLM provider (anthropic, gemini)"
    )
    refresh_parser.add_argument("--model", "-m", help="Model to use")
    refresh_parser.add_argument(
        "--hours", type=int, default=48, help="Hours of posts to include (default: 48)"
    )
    refresh_parser.add_argument(
        "--token-limit", type=int, default=2000, help="Target token limit (default: 2000)"
    )
    refresh_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose output"
    )
    refresh_parser.set_defaults(func=cmd_refresh)

    # show command
    show_parser = subparsers.add_parser("show", help="Show current context")
    show_parser.set_defaults(func=cmd_show)

    # set command
    set_parser = subparsers.add_parser("set", help="Manually set context text")
    set_parser.add_argument("text", help="Context text to set")
    set_parser.set_defaults(func=cmd_set)

    # history command
    history_parser = subparsers.add_parser("history", help="Show context history")
    history_parser.add_argument(
        "--limit", "-l", type=int, default=10, help="Number of entries (default: 10)"
    )
    history_parser.set_defaults(func=cmd_history)

    # clear command
    clear_parser = subparsers.add_parser("clear", help="Clear current context")
    clear_parser.set_defaults(func=cmd_clear)

    args = parser.parse_args()

    if args.command is None:
        # Default to show
        args.db = DEFAULT_DB_PATH
        cmd_show(args)
    elif hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
