#!/usr/bin/env python3
"""
Aerie Tweet Classifier

Uses Claude to classify tweets based on prompts stored in the database.
Responses are cached in prompt_responses for use by mode extractors.
"""

import argparse
import json
import uuid
from pathlib import Path

from database import (
    DEFAULT_DB_PATH,
    assemble_classification_chains,
    create_mode,
    create_prompt,
    get_prompt,
    get_stats,
    get_tweets_without_response,
    init_database,
    list_modes,
    list_prompts,
    store_prompt_response,
)
from modes import compute_all_mode_decisions
from providers import get_default_provider, get_provider, list_providers

# =============================================================================
# Built-in Prompts
# =============================================================================

BUILTIN_PROMPTS = {
    "binary_filter_v1": {
        "prompt_text": """You are a tweet filter assistant. Determine if this content should be shown to the user.

You may receive either a single tweet or a conversation thread. If it's a thread, evaluate the overall value of the conversation - your decision will apply to all tweets in the thread.

SHOW content that is:
- Informative, educational, or genuinely interesting
- Positive or constructive discussions
- Creative content, humor, or entertainment
- Professional updates or industry news
- Personal updates that aren't negative

HIDE content that is:
- Ragebait or content designed to provoke outrage
- Doomposting or excessively negative content
- Political flamewars or tribal arguments
- Engagement farming ("ratio this", "hot take:", etc.)
- Crypto/NFT spam or get-rich-quick schemes
- Inflammatory hot takes designed for engagement
- Pile-ons or harassment campaigns

Respond with ONLY a JSON object:
{"approved": true, "reason": "brief reason"} or {"approved": false, "reason": "brief reason"}""",
        "response_schema": '{"approved": "boolean", "reason": "string"}',
    },
    "topic_tagger_v1": {
        "prompt_text": """Analyze this content and extract topics and quality scores.

You may receive either a single tweet or a conversation thread. If it's a thread, evaluate the overall conversation.

Topics (select all that apply):
- ml: machine learning, AI, deep learning, neural networks
- tech: programming, software, engineering, startups
- science: research, physics, biology, mathematics
- spirituality: meditation, dharma, philosophy, Buddhism, consciousness
- politics: political news, policy, elections
- humor: jokes, memes, comedy
- personal: life updates, personal stories
- news: current events, breaking news
- crypto: cryptocurrency, NFT, web3
- other: doesn't fit other categories

Scores (0.0 to 1.0):
- toxicity: hostile, inflammatory, mean-spirited
- engagement_bait: "ratio this", hot takes for attention, rage farming
- informativeness: teaches something, shares genuine insight
- positivity: uplifting, encouraging, warm

Respond with ONLY a JSON object:
{"topics": ["topic1", "topic2"], "scores": {"toxicity": 0.1, "engagement_bait": 0.2, "informativeness": 0.7, "positivity": 0.5}}""",
        "response_schema": '{"topics": ["string"], "scores": {"toxicity": "number", "engagement_bait": "number", "informativeness": "number", "positivity": "number"}}',
    },
}


def ensure_builtin_prompts(db_path: Path = DEFAULT_DB_PATH):
    """Create built-in prompts if they don't exist."""
    for prompt_id, config in BUILTIN_PROMPTS.items():
        if not get_prompt(prompt_id, db_path):
            create_prompt(
                prompt_id,
                config["prompt_text"],
                config.get("response_schema"),
                db_path,
            )
            print(f"Created built-in prompt: {prompt_id}")


def ensure_default_mode(db_path: Path = DEFAULT_DB_PATH):
    """Create default mode if no modes exist."""
    modes = list_modes(db_path)
    if not modes:
        create_mode(
            mode_id="default",
            name="Default",
            prompt_id="binary_filter_v1",
            extractor="default",
            description="Simple binary filter - shows quality content, hides toxicity",
            db_path=db_path,
        )
        print("Created default mode")


# =============================================================================
# Tweet Formatting
# =============================================================================


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
        quoted = tweet.get("quoted_tweet")
        if quoted:
            quoted_author = quoted.get("author_username") or "unknown"
            quoted_text = quoted.get("text", "")
            # Truncate very long quoted tweets
            if len(quoted_text) > 500:
                quoted_text = quoted_text[:500] + "..."
            parts.append(f"[Quoting @{quoted_author}: \"{quoted_text}\"]")
        else:
            parts.append("[This is a quote tweet]")
    if tweet.get("reply_to_username"):
        parts.append(f"[Replying to @{tweet['reply_to_username']}]")

    # Media with alt text
    media_json = tweet.get("media_json")
    if media_json:
        import json
        try:
            media_list = json.loads(media_json) if isinstance(media_json, str) else media_json
            for item in media_list:
                media_type = item.get("type", "media")
                alt = item.get("alt", "")
                if media_type == "link":
                    # Link preview card
                    title = item.get("title", "")
                    uri = item.get("uri", "")
                    if title or uri:
                        parts.append(f"[Link: {title or uri}]")
                elif alt:
                    # Image/video with alt text
                    parts.append(f"[Image: {alt}]")
                else:
                    # Media without alt text - just note its presence
                    parts.append(f"[{media_type}]")
        except (json.JSONDecodeError, TypeError):
            pass

    # Engagement metrics
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


def format_chain_for_classification(tweets: list[dict]) -> str:
    """
    Format a conversation chain for LLM classification.

    Args:
        tweets: List of tweets in chronological order (oldest first)

    Returns:
        Formatted string showing the conversation thread
    """
    if not tweets:
        return ""

    if len(tweets) == 1:
        # Single tweet - use simpler format
        return format_tweet_for_classification(tweets[0])

    # Multiple tweets - format as conversation
    parts = ["CONVERSATION THREAD:", ""]

    for i, tweet in enumerate(tweets, 1):
        # Tweet number and author
        author = tweet.get("author_username") or "unknown"
        display_name = tweet.get("author_display_name") or author
        verified = " [verified]" if tweet.get("author_verified") else ""

        # Determine context (reply relationship)
        context = ""
        if i > 1:
            prev_author = tweets[i-2].get("author_username") or "unknown"
            if tweet.get("reply_to_username") == prev_author:
                context = f" (replying to @{prev_author})"
            elif tweet.get("reply_to_username"):
                context = f" (replying to @{tweet['reply_to_username']})"

        parts.append(f"Tweet {i}{context}:")
        parts.append(f"@{author} ({display_name}){verified}")

        # Tweet text
        parts.append(tweet.get("text", ""))

        # Engagement metrics
        metrics = []
        if tweet.get("like_count", 0) > 0:
            metrics.append(f"{tweet['like_count']} likes")
        if tweet.get("retweet_count", 0) > 0:
            metrics.append(f"{tweet['retweet_count']} retweets")
        if tweet.get("reply_count", 0) > 0:
            metrics.append(f"{tweet['reply_count']} replies")
        if metrics:
            parts.append(f"[{', '.join(metrics)}]")

        # Add blank line between tweets
        parts.append("")

    return "\n".join(parts)


# =============================================================================
# Classification
# =============================================================================


def run_classification(
    prompt_id: str,
    provider_name: str | None = None,
    model: str | None = None,
    batch_size: int = 10,
    max_tweets: int | None = None,
    db_path: Path = DEFAULT_DB_PATH,
    dry_run: bool = False,
    verbose: bool = False,
):
    """
    Run classification for a specific prompt.
    Processes tweets that don't have responses for this prompt yet.

    Args:
        prompt_id: ID of the prompt to use.
        provider_name: LLM provider ("anthropic", "gemini"). Defaults to "anthropic".
        model: Model to use. Defaults to provider's default model.
        batch_size: Number of tweets to process per batch.
        max_tweets: Maximum number of tweets to classify.
        db_path: Path to the database.
        dry_run: If True, don't save results.
        verbose: If True, show detailed output.
    """
    # Use default provider if not specified
    if provider_name is None:
        provider_name = get_default_provider()

    # Get provider and validate API key
    try:
        provider = get_provider(provider_name)
    except ValueError as e:
        print(f"Error: {e}")
        return

    api_key = provider.get_api_key()
    if not api_key:
        print(f"Error: {provider.config.api_key_env_var} environment variable not set")
        if provider_name == "anthropic":
            print("Get your API key from https://console.anthropic.com/")
        elif provider_name == "gemini":
            print("Get your API key from https://aistudio.google.com/apikey")
        return

    # Use provider's default model if not specified
    actual_model = provider.get_model(model)

    # Get prompt
    prompt = get_prompt(prompt_id, db_path)
    if not prompt:
        print(f"Error: Prompt '{prompt_id}' not found")
        print("Available prompts:")
        for p in list_prompts(db_path):
            print(f"  - {p['id']}")
        return

    system_prompt = prompt["prompt_text"]

    # Get stats
    stats = get_stats(db_path=db_path)
    tweets_to_process = get_tweets_without_response(
        prompt_id, actual_model, limit=10000, db_path=db_path
    )
    pending_count = len(tweets_to_process)

    print(f"Prompt: {prompt_id}")
    print(f"Provider: {provider_name}")
    print(f"Model: {actual_model}")
    print(f"Database: {stats['total']} total tweets, {pending_count} need classification")

    if pending_count == 0:
        print("All tweets already have responses for this prompt.")
        return

    # Determine how many to process
    limit = min(max_tweets, pending_count) if max_tweets else pending_count
    print(f"Processing up to {limit} tweets...")

    if dry_run:
        print("(Dry run - no changes will be saved)")

    print()

    # Get all unclassified tweets
    tweets = get_tweets_without_response(prompt_id, actual_model, limit=limit, db_path=db_path)

    if not tweets:
        print("No tweets to classify.")
        return

    print(f"Step 1: Checking for tweets that can inherit classifications...")

    # First pass: Apply inheritance logic
    # Tweets that reply to classified tweets inherit their parent's classification
    inherited_count = 0
    tweets_needing_llm = []

    for tweet in tweets:
        parent_id = tweet.get("reply_to_tweet_id")
        if parent_id:
            # Check if parent has a classification for this prompt+model
            from database import get_prompt_response
            parent_response = get_prompt_response(parent_id, prompt_id, actual_model, db_path)

            if parent_response:
                # Inherit parent's classification
                if verbose:
                    print(f"  Tweet {tweet['id'][:10]}... inherits from parent {parent_id[:10]}...")

                if not dry_run:
                    # Copy parent's response with reference to parent's batch_id
                    parent_batch_id = parent_response.get("classification_batch_id")
                    store_prompt_response(
                        tweet["id"],
                        prompt_id,
                        actual_model,
                        parent_response.get("response_json", parent_response),
                        db_path,
                        classification_batch_id=parent_batch_id,  # Inherit batch_id
                    )

                inherited_count += 1
                continue

        # No parent or parent not classified - needs LLM
        tweets_needing_llm.append(tweet)

    print(f"  {inherited_count} tweets inherited parent classifications")
    print(f"  {len(tweets_needing_llm)} tweets need LLM classification")
    print()

    if not tweets_needing_llm:
        print("All tweets resolved via inheritance. No LLM calls needed.")
        return

    # Second pass: Assemble chains and classify
    print(f"Step 2: Assembling conversation chains...")
    chains = assemble_classification_chains(tweets_needing_llm, prompt_id, actual_model, db_path)
    print(f"  Assembled {len(chains)} conversation chains")
    print()

    # Process chains
    print(f"Step 3: Classifying conversation chains...")
    processed = 0
    success_count = 0
    error_count = 0

    for chain_idx, chain in enumerate(chains, 1):
        chain_tweets = chain["tweets"]
        unclassified_ids = chain["unclassified_ids"]

        if verbose:
            print(f"  Chain {chain_idx}/{len(chains)}: {len(chain_tweets)} tweets ({len(unclassified_ids)} unclassified)")
            for tweet in chain_tweets:
                author = tweet.get("author_username", "unknown")
                text_preview = (tweet.get("text", "")[:40] + "...") if len(tweet.get("text", "")) > 40 else tweet.get("text", "")
                classified_marker = "" if tweet["id"] in unclassified_ids else "[context] "
                print(f"    {classified_marker}@{author}: {text_preview}")

        # Format chain for LLM
        chain_text = format_chain_for_classification(chain_tweets)

        # Call LLM once for entire chain
        try:
            provider = get_provider(provider_name)
            response = provider.classify(chain_text, system_prompt, actual_model)
        except ValueError as e:
            response = {"_error": "provider_error", "_message": str(e)[:200]}

        if verbose:
            if "_error" in response:
                print(f"    -> ERROR: {response.get('_error')}")
            else:
                print(f"    -> {json.dumps(response)[:80]}...")

        # Generate batch ID for this chain
        batch_id = str(uuid.uuid4())

        # Store response for all unclassified tweets in chain
        if not dry_run:
            for tweet_id in unclassified_ids:
                store_prompt_response(
                    tweet_id,
                    prompt_id,
                    actual_model,
                    response,
                    db_path,
                    classification_batch_id=batch_id,
                )

        # Update counts
        processed += len(unclassified_ids)
        if "_error" in response:
            error_count += len(unclassified_ids)
        else:
            success_count += len(unclassified_ids)

        if not verbose:
            print(f"  Processed {processed}/{len(tweets_needing_llm)} tweets ({len(chains) - chain_idx} chains remaining)...")

    print()
    print(f"  Chain classification complete: {success_count} successful, {error_count} errors")
    print()

    # Final stats
    print("=" * 40)
    print("Classification complete!")
    print(f"  Total processed: {processed + inherited_count}")
    print(f"  Inherited from parent: {inherited_count}")
    print(f"  LLM classified: {success_count}")
    print(f"  Errors: {error_count}")

    # Update cached mode decisions
    if not dry_run and success_count > 0:
        print()
        print("Updating mode decision cache...")
        decision_results = compute_all_mode_decisions(actual_model, db_path)
        for mode_id, result in decision_results.items():
            if result["computed"] > 0:
                print(f"  {mode_id}: {result['computed']} decisions computed")


def setup_prompts_and_modes(db_path: Path = DEFAULT_DB_PATH):
    """Initialize database with built-in prompts and default mode."""
    init_database(db_path)
    ensure_builtin_prompts(db_path)
    ensure_default_mode(db_path)


# =============================================================================
# CLI
# =============================================================================


def cmd_classify(args):
    """Run classification for a prompt."""
    setup_prompts_and_modes(args.db)
    run_classification(
        prompt_id=args.prompt,
        provider_name=args.provider,
        model=args.model,
        batch_size=args.batch_size,
        max_tweets=args.max_tweets,
        db_path=args.db,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


def cmd_list_prompts(args):
    """List available prompts."""
    setup_prompts_and_modes(args.db)
    prompts = list_prompts(args.db)
    if not prompts:
        print("No prompts defined.")
        return

    print("Available prompts:")
    for p in prompts:
        print(f"\n  {p['id']}")
        print(f"    Created: {p['created_at']}")
        preview = p["prompt_text"][:100].replace("\n", " ")
        print(f"    Preview: {preview}...")


def cmd_list_modes(args):
    """List available modes."""
    setup_prompts_and_modes(args.db)
    modes = list_modes(args.db)
    if not modes:
        print("No modes defined.")
        return

    print("Available modes:")
    for m in modes:
        print(f"\n  {m['id']} - {m['name']}")
        print(f"    Prompt: {m['prompt_id']}")
        print(f"    Extractor: {m['extractor']}")
        if m["prefilter"]:
            print(f"    Prefilter: {m['prefilter']}")
        provider = m.get("provider", "anthropic")
        model_name = m.get("model_name")
        print(f"    Provider: {provider}" + (f" ({model_name})" if model_name else ""))
        if m["description"]:
            print(f"    Description: {m['description']}")


def cmd_create_prompt(args):
    """Create a new prompt from a file."""
    if not args.file.exists():
        print(f"Error: File not found: {args.file}")
        return

    prompt_text = args.file.read_text().strip()
    create_prompt(args.id, prompt_text, args.schema, args.db)
    print(f"Created prompt: {args.id}")


def cmd_create_mode(args):
    """Create a new mode."""
    setup_prompts_and_modes(args.db)

    # Verify prompt exists
    if not get_prompt(args.prompt, args.db):
        print(f"Error: Prompt '{args.prompt}' not found")
        return

    # Verify provider exists
    provider_name = args.provider or "anthropic"
    try:
        get_provider(provider_name)
    except ValueError as e:
        print(f"Error: {e}")
        return

    create_mode(
        mode_id=args.id,
        name=args.name or args.id,
        prompt_id=args.prompt,
        extractor=args.extractor,
        prefilter=args.prefilter,
        description=args.description,
        provider=provider_name,
        model_name=args.model,
        db_path=args.db,
    )
    print(f"Created mode: {args.id}")


def cmd_recompute_decisions(args):
    """Recompute cached decisions for all modes."""
    setup_prompts_and_modes(args.db)

    modes = list_modes(args.db)
    if not modes:
        print("No modes found.")
        return

    print(f"Recomputing decisions for {len(modes)} modes...")
    print()

    from database import invalidate_mode_decisions
    from modes import compute_mode_decisions

    for mode in modes:
        mode_id = mode["id"]
        print(f"  {mode_id}:")

        # Invalidate existing decisions
        deleted = invalidate_mode_decisions(mode_id, args.db)
        if deleted > 0:
            print(f"    Invalidated {deleted} existing decisions")

        # Compute new decisions
        result = compute_mode_decisions(mode_id, db_path=args.db)
        print(f"    Computed: {result['computed']} decisions")
        if result["pending"] > 0:
            print(f"    Pending (need LLM): {result['pending']}")
        print()

    print("Done!")


def cmd_list_providers(args):
    """List available LLM providers."""
    providers = list_providers()
    print("Available providers:")
    for p in providers:
        status = "[API key set]" if p["api_key_set"] else "[API key missing]"
        print(f"\n  {p['name']} {status}")
        print(f"    {p['description']}")
        print(f"    Default model: {p['default_model']}")
        print(f"    Available models: {', '.join(p['available_models'])}")
        print(f"    API key env var: {p['api_key_env_var']}")


def main():
    parser = argparse.ArgumentParser(
        description="Aerie Tweet Classifier",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Database path (default: {DEFAULT_DB_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # classify command
    classify_parser = subparsers.add_parser("classify", help="Run classification for a prompt")
    classify_parser.add_argument(
        "prompt",
        nargs="?",
        default="binary_filter_v1",
        help="Prompt ID to use (default: binary_filter_v1)",
    )
    classify_parser.add_argument(
        "--provider",
        "-p",
        help="LLM provider to use (anthropic, gemini). Default: anthropic",
    )
    classify_parser.add_argument(
        "--model",
        help="Model to use (defaults to provider's default model)",
    )
    classify_parser.add_argument(
        "--batch-size",
        "-b",
        type=int,
        default=10,
        help="Tweets per batch (default: 10)",
    )
    classify_parser.add_argument(
        "--max",
        "-m",
        type=int,
        dest="max_tweets",
        help="Maximum tweets to classify",
    )
    classify_parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Don't save results",
    )
    classify_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed output",
    )
    classify_parser.set_defaults(func=cmd_classify)

    # prompts command
    prompts_parser = subparsers.add_parser("prompts", help="List prompts")
    prompts_parser.set_defaults(func=cmd_list_prompts)

    # modes command
    modes_parser = subparsers.add_parser("modes", help="List modes")
    modes_parser.set_defaults(func=cmd_list_modes)

    # providers command
    providers_parser = subparsers.add_parser("providers", help="List available LLM providers")
    providers_parser.set_defaults(func=cmd_list_providers)

    # create-prompt command
    create_prompt_parser = subparsers.add_parser("create-prompt", help="Create a prompt from file")
    create_prompt_parser.add_argument("id", help="Prompt ID")
    create_prompt_parser.add_argument("file", type=Path, help="Prompt text file")
    create_prompt_parser.add_argument("--schema", help="JSON schema for response")
    create_prompt_parser.set_defaults(func=cmd_create_prompt)

    # create-mode command
    create_mode_parser = subparsers.add_parser("create-mode", help="Create a mode")
    create_mode_parser.add_argument("id", help="Mode ID")
    create_mode_parser.add_argument("--prompt", required=True, help="Prompt ID to use")
    create_mode_parser.add_argument("--extractor", required=True, help="Extractor function name")
    create_mode_parser.add_argument("--prefilter", help="Prefilter function name")
    create_mode_parser.add_argument("--name", help="Display name")
    create_mode_parser.add_argument("--description", help="Mode description")
    create_mode_parser.add_argument(
        "--provider", help="LLM provider (anthropic, gemini). Default: anthropic"
    )
    create_mode_parser.add_argument("--model", help="Model to use (defaults to provider's default)")
    create_mode_parser.set_defaults(func=cmd_create_mode)

    # recompute-decisions command
    recompute_parser = subparsers.add_parser(
        "recompute-decisions",
        help="Recompute cached mode decisions (run after schema migration)",
    )
    recompute_parser.set_defaults(func=cmd_recompute_decisions)

    args = parser.parse_args()

    if args.command is None:
        # Default to classify with default prompt
        args.prompt = "binary_filter_v1"
        args.provider = None  # Will use default provider
        args.model = None  # Will use provider's default model
        args.batch_size = 10
        args.max_tweets = None
        args.dry_run = False
        args.verbose = False
        cmd_classify(args)
    elif hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
