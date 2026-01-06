#!/usr/bin/env python3
"""
Aerie Tweet Classifier

Uses Claude to classify tweets based on a user-defined filter prompt.
Tweets that pass the filter are marked 'approved', others are 'filtered'.
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import anthropic
from database import (
    DEFAULT_DB_PATH,
    get_pending_tweets,
    get_stats,
    update_classification,
)

# Default filter prompt - users can customize via --prompt-file
DEFAULT_PROMPT = """You are a tweet filter assistant. Your job is to determine if a tweet should be shown to the user based on the following criteria:

SHOW tweets that are:
- Informative, educational, or genuinely interesting
- Positive or constructive discussions
- Creative content, humor, or entertainment
- Professional updates or industry news
- Personal updates from friends/mutuals that aren't negative

HIDE tweets that are:
- Ragebait or content designed to provoke outrage
- Doomposting or excessively negative content
- Political flamewars or tribal arguments
- Engagement farming ("ratio this", "hot take:", etc.)
- Crypto/NFT spam or get-rich-quick schemes
- Inflammatory hot takes designed for engagement
- Pile-ons or harassment campaigns

Respond with ONLY a JSON object in this exact format:
{"approved": true, "reason": "brief reason"}
or
{"approved": false, "reason": "brief reason"}

Do not include any other text before or after the JSON."""


def load_prompt(prompt_file: Path | None) -> str:
    """Load filter prompt from file or use default."""
    if prompt_file and prompt_file.exists():
        return prompt_file.read_text().strip()
    return DEFAULT_PROMPT


def format_tweet_for_classification(tweet: dict) -> str:
    """Format a tweet dict into a string for the LLM."""
    parts = []

    # Author info
    author = tweet.get("author_username") or "unknown"
    display_name = tweet.get("author_display_name") or author
    parts.append(f"@{author} ({display_name})")

    if tweet.get("author_verified"):
        parts[-1] += " [verified]"

    # Tweet text
    parts.append(tweet.get("text", ""))

    # Context
    if tweet.get("is_retweet"):
        parts.append("[This is a retweet]")
    if tweet.get("is_quote"):
        parts.append("[This is a quote tweet]")
    if tweet.get("reply_to_username"):
        parts.append(f"[Replying to @{tweet['reply_to_username']}]")

    # Engagement metrics (can indicate viral/controversial content)
    metrics = []
    if tweet.get("like_count", 0) > 0:
        metrics.append(f"{tweet['like_count']} likes")
    if tweet.get("retweet_count", 0) > 0:
        metrics.append(f"{tweet['retweet_count']} retweets")
    if tweet.get("reply_count", 0) > 0:
        metrics.append(f"{tweet['reply_count']} replies")
    if metrics:
        parts.append(f"[{', '.join(metrics)}]")

    return "\n".join(parts)


def classify_tweet(
    client: anthropic.Anthropic,
    tweet: dict,
    system_prompt: str,
    model: str = "claude-sonnet-4-20250514",
) -> tuple[bool, str]:
    """
    Classify a single tweet using Claude.
    Returns (approved: bool, reason: str).
    """
    tweet_text = format_tweet_for_classification(tweet)
    print(tweet_text)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=150,
            system=system_prompt,
            messages=[
                {"role": "user", "content": f"Classify this tweet:\n\n{tweet_text}"}
            ],
        )

        # Parse the response
        content = response.content[0].text.strip()

        # Try to extract JSON from the response
        # Handle case where model might include extra text
        try:
            # First try direct parse
            result = json.loads(content)
        except json.JSONDecodeError:
            # Try to find JSON in the response
            import re

            json_match = re.search(r"\{[^}]+\}", content)
            if json_match:
                result = json.loads(json_match.group())
            else:
                # Fallback: look for keywords
                content_lower = content.lower()
                if "approved" in content_lower and "true" in content_lower:
                    return True, "LLM indicated approval"
                elif "approved" in content_lower and "false" in content_lower:
                    return False, "LLM indicated rejection"
                else:
                    # Default to approved if we can't parse
                    return (
                        True,
                        f"Could not parse response, defaulting to approved: {content[:100]}",
                    )

        approved = result.get("approved", True)
        reason = result.get("reason", "No reason provided")

        return approved, reason

    except anthropic.APIError as e:
        print(f"  API error: {e}")
        return True, f"API error, defaulting to approved: {str(e)[:100]}"


def classify_batch(
    client: anthropic.Anthropic,
    tweets: list[dict],
    system_prompt: str,
    model: str = "claude-sonnet-4-20250514",
    verbose: bool = False,
) -> list[tuple[str, bool, str]]:
    """
    Classify a batch of tweets.
    Returns list of (tweet_id, approved, reason).
    """
    results = []

    for i, tweet in enumerate(tweets):
        tweet_id = tweet["id"]
        if verbose:
            author = tweet.get("author_username", "unknown")
            text_preview = (
                (tweet.get("text", "")[:50] + "...")
                if len(tweet.get("text", "")) > 50
                else tweet.get("text", "")
            )
            print(f"  [{i + 1}/{len(tweets)}] @{author}: {text_preview}")

        approved, reason = classify_tweet(client, tweet, system_prompt, model)
        results.append((tweet_id, approved, reason))

        if verbose:
            status = "APPROVED" if approved else "FILTERED"
            print(f"    -> {status}: {reason}")

    return results


def run_classifier(
    prompt_file: Path | None = None,
    batch_size: int = 10,
    max_tweets: int | None = None,
    model: str = "claude-sonnet-4-20250514",
    db_path: Path = DEFAULT_DB_PATH,
    dry_run: bool = False,
    verbose: bool = False,
):
    """
    Main classification loop.
    Fetches pending tweets and classifies them using Claude.
    """
    # Check for API key
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable not set")
        print("Get your API key from https://console.anthropic.com/")
        return

    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = load_prompt(prompt_file)

    # Get stats
    stats = get_stats(db_path)
    print(
        f"Database: {stats['total']} total, {stats['pending']} pending, "
        f"{stats['approved']} approved, {stats['filtered']} filtered"
    )

    if stats["pending"] == 0:
        print("No pending tweets to classify.")
        return

    # Determine how many to process
    limit = min(max_tweets, stats["pending"]) if max_tweets else stats["pending"]
    print(f"Processing up to {limit} tweets...")

    if dry_run:
        print("(Dry run - no changes will be saved)")

    print()

    # Process in batches
    processed = 0
    approved_count = 0
    filtered_count = 0

    while processed < limit:
        # Fetch next batch
        remaining = limit - processed
        fetch_count = min(batch_size, remaining)
        tweets = get_pending_tweets(fetch_count, db_path)

        if not tweets:
            break

        print(
            f"Batch {processed // batch_size + 1}: classifying {len(tweets)} tweets..."
        )

        # Classify
        results = classify_batch(client, tweets, system_prompt, model, verbose)

        # Save results
        for tweet_id, approved, reason in results:
            if not dry_run:
                update_classification(tweet_id, approved, reason, db_path)

            if approved:
                approved_count += 1
            else:
                filtered_count += 1

        processed += len(tweets)
        print(
            f"  Batch complete: {sum(1 for _, a, _ in results if a)} approved, "
            f"{sum(1 for _, a, _ in results if not a)} filtered"
        )
        print()

    # Final stats
    print("=" * 40)
    print(f"Classification complete!")
    print(f"  Processed: {processed}")
    print(f"  Approved:  {approved_count}")
    print(f"  Filtered:  {filtered_count}")

    if not dry_run:
        new_stats = get_stats(db_path)
        print(
            f"\nNew totals: {new_stats['pending']} pending, "
            f"{new_stats['approved']} approved, {new_stats['filtered']} filtered"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Classify pending tweets using Claude",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Classify all pending tweets
  python classifier.py

  # Classify with custom prompt
  python classifier.py --prompt-file my_filter.txt

  # Dry run to see what would happen
  python classifier.py --dry-run --verbose

  # Classify only 20 tweets with Haiku (faster/cheaper)
  python classifier.py --max 20 --model claude-3-haiku-20240307
        """,
    )

    parser.add_argument(
        "--prompt-file", "-p", type=Path, help="Path to custom filter prompt file"
    )
    parser.add_argument(
        "--batch-size",
        "-b",
        type=int,
        default=10,
        help="Number of tweets to process per batch (default: 10)",
    )
    parser.add_argument(
        "--max",
        "-m",
        type=int,
        dest="max_tweets",
        help="Maximum number of tweets to classify",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-20250514",
        help="Claude model to use (default: claude-sonnet-4-20250514)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Database path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Don't save results, just show what would happen",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed output for each tweet",
    )

    args = parser.parse_args()

    run_classifier(
        prompt_file=args.prompt_file,
        batch_size=args.batch_size,
        max_tweets=args.max_tweets,
        model=args.model,
        db_path=args.db,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
