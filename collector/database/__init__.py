"""
SQLite database operations for tweet storage and classification.

This package is organized into submodules:
- core: Connection management, transactions, schema initialization
- tweets: Tweet CRUD operations
- prompts: Prompt and LLM response operations
- db_modes: Mode CRUD, human labels, decision caching
- settings: Key-value settings storage
- context: Context snapshot management
- chains: Conversation chains for display and classification

All functions are re-exported here for backwards compatibility.
"""

# Core infrastructure
# Chain operations
from database.chains import (
    assemble_classification_chains,
    compute_conversation_chains,
    compute_single_chain,
    count_conversation_roots_after_cursor,
    get_approved_descendants,
    get_conversation_roots,
    get_conversation_roots_cursor,
    get_direct_replies,
    get_direct_replies_batch,
    get_replies_for_tweet,
    get_thread_ancestors,
    get_thread_context_batch,
)

# Context operations
from database.context import (
    create_context,
    get_context,
    get_current_context,
    list_contexts,
    set_current_context,
    update_context_text,
)
from database.core import (
    DEFAULT_DB_PATH,
    decode_cursor,
    encode_cursor,
    get_connection,
    init_database,
    parse_twitter_date,
    transaction,
)

# Mode operations
from database.db_modes import (
    add_human_label,
    create_mode,
    delete_mode,
    get_cached_decision_stats,
    get_human_labels,
    get_mode,
    get_mode_decision,
    get_mode_decisions_batch,
    get_mode_label_counts,
    get_recently_classified_tweets,
    get_tweets_without_decision,
    get_tweets_without_decision_filtered,
    get_unlabeled_tweets,
    invalidate_mode_decisions,
    invalidate_tweet_decisions,
    list_modes,
    store_mode_decision,
    store_mode_decisions_batch,
    update_mode,
)

# Prompt operations
from database.prompts import (
    count_modes_using_prompt,
    create_prompt,
    delete_prompt,
    get_prompt,
    get_prompt_response,
    get_prompt_responses_batch,
    get_stats,
    get_tweets_without_response,
    list_prompts,
    store_prompt_response,
    update_prompt,
)

# Settings operations
from database.settings import (
    delete_setting,
    get_all_settings,
    get_setting,
    set_setting,
)

# Tweet operations
from database.tweets import (
    get_all_tweet_ids,
    get_bluesky_post_ids,
    get_platform_stats,
    get_recent_tweets_for_context,
    get_retweets_batch,
    get_retweets_for_tweet,
    get_tweet,
    get_tweets_batch,
    get_tweets_by_author,
    store_retweets,
    store_tweets,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "add_human_label",
    "assemble_classification_chains",
    "compute_conversation_chains",
    "compute_single_chain",
    "count_conversation_roots_after_cursor",
    "count_modes_using_prompt",
    "create_context",
    "create_mode",
    "create_prompt",
    "decode_cursor",
    "delete_mode",
    "delete_prompt",
    "delete_setting",
    "encode_cursor",
    "get_all_settings",
    "get_all_tweet_ids",
    "get_approved_descendants",
    "get_bluesky_post_ids",
    "get_cached_decision_stats",
    "get_connection",
    "get_context",
    "get_conversation_roots",
    "get_conversation_roots_cursor",
    "get_current_context",
    "get_direct_replies",
    "get_direct_replies_batch",
    "get_human_labels",
    "get_mode",
    "get_mode_decision",
    "get_mode_decisions_batch",
    "get_mode_label_counts",
    "get_platform_stats",
    "get_prompt",
    "get_prompt_response",
    "get_prompt_responses_batch",
    "get_recent_tweets_for_context",
    "get_recently_classified_tweets",
    "get_replies_for_tweet",
    "get_retweets_batch",
    "get_retweets_for_tweet",
    "get_setting",
    "get_stats",
    "get_thread_ancestors",
    "get_thread_context_batch",
    "get_tweet",
    "get_tweets_batch",
    "get_tweets_by_author",
    "get_tweets_without_decision",
    "get_tweets_without_decision_filtered",
    "get_tweets_without_response",
    "get_unlabeled_tweets",
    "init_database",
    "invalidate_mode_decisions",
    "invalidate_tweet_decisions",
    "list_contexts",
    "list_modes",
    "list_prompts",
    "parse_twitter_date",
    "set_current_context",
    "set_setting",
    "store_mode_decision",
    "store_mode_decisions_batch",
    "store_prompt_response",
    "store_retweets",
    "store_tweets",
    "transaction",
    "update_context_text",
    "update_mode",
    "update_prompt",
]

# For direct module execution
if __name__ == "__main__":
    init_database()
    print(f"Database initialized at {DEFAULT_DB_PATH}")
