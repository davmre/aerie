"""
Batch chain classification using multi-chain LLM requests.

This module provides efficient classification by sending multiple conversation
chains in a single LLM request, reducing costs while providing proper thread
context for each classification decision.
"""

from pathlib import Path

from classifier import format_chain_for_classification
from database import DEFAULT_DB_PATH, get_prompt, store_prompt_response
from providers import get_default_provider, get_provider


def format_chain_for_batch(chain: dict, index: int) -> str:
    """
    Format a conversation chain for inclusion in a batch request.

    Args:
        chain: Chain dict from assemble_classification_chains with 'tweets' and 'unclassified_ids'
        index: 1-based index for this chain in the batch

    Returns:
        Formatted string with chain header and content
    """
    tweets = chain["tweets"]
    # Use existing format_chain_for_classification for content
    chain_content = format_chain_for_classification(tweets)
    return f"[Chain {index}]\n{chain_content}"


def format_chains_batch(chains: list[dict]) -> str:
    """
    Format multiple chains for a batch classification request.

    Args:
        chains: List of chain dicts from assemble_classification_chains

    Returns:
        Formatted string with all chains separated by dividers
    """
    formatted = []
    for i, chain in enumerate(chains, 1):
        formatted.append(format_chain_for_batch(chain, i))
    return "\n\n---\n\n".join(formatted)


def build_batch_prompt(base_prompt: str, context_text: str | None = None) -> str:
    """
    Wrap the base prompt with batch chain classification instructions.

    The base prompt contains the classification criteria. We add instructions
    for handling multiple chains and the expected response format.

    Args:
        base_prompt: The core classification prompt text
        context_text: Optional situational context to prepend

    Returns:
        The complete system prompt for batch chain classification
    """
    # Add context prefix if provided
    context_section = ""
    if context_text:
        context_section = f"""CURRENT SITUATIONAL CONTEXT:
{context_text}

---

"""

    return f"""{context_section}{base_prompt}

---

You will be given multiple conversation chains to classify. Each chain may be a single tweet or a full conversation thread. Evaluate each chain as a whole and provide your classification.

IMPORTANT: Respond with ONLY a JSON array. Each element must have:
- "chain": the chain number (1, 2, 3, etc.)
- "approved": boolean (true to show, false to hide)
- "reason": brief explanation (1 sentence)
- "needs_context": boolean (optional, true if you need more context about the topic/author to make a good decision)

Example response format:
[
  {{"chain": 1, "approved": true, "reason": "Informative tech discussion thread"}},
  {{"chain": 2, "approved": false, "reason": "Engagement bait thread"}},
  {{"chain": 3, "approved": true, "reason": "Unclear reference", "needs_context": true}}
]"""


def classify_chains_batch(
    chains: list[dict],
    prompt_id: str,
    provider_name: str | None = None,
    model: str | None = None,
) -> dict[int, dict]:
    """
    Classify multiple conversation chains in a single LLM request.

    Args:
        chains: List of chain dicts from assemble_classification_chains.
            Each has 'tweets' and 'unclassified_ids'.
        prompt_id: ID of the prompt to use from the prompts table.
        provider_name: LLM provider ("anthropic", "gemini"). Defaults to "anthropic".
        model: Model to use (defaults to provider's default model).

    Returns:
        Dict mapping chain_index (1-based int) -> classification result dict.
        Each result has either {approved, reason} or {_error, ...}.
    """
    if not chains:
        return {}

    # Get the prompt
    prompt = get_prompt(prompt_id)
    if not prompt:
        return {
            i: {"_error": "prompt_not_found", "_prompt_id": prompt_id}
            for i in range(1, len(chains) + 1)
        }

    # Get provider
    if provider_name is None:
        provider_name = get_default_provider()

    try:
        provider = get_provider(provider_name)
    except ValueError as e:
        return {
            i: {"_error": "provider_error", "_message": str(e)[:200]}
            for i in range(1, len(chains) + 1)
        }

    # Check API key
    if not provider.get_api_key():
        return {
            i: {"_error": "no_api_key", "_env_var": provider.config.api_key_env_var}
            for i in range(1, len(chains) + 1)
        }

    # Build the batch prompt and format chains
    system_prompt = build_batch_prompt(prompt["prompt_text"])
    chains_text = format_chains_batch(chains)
    # Use chain indices as expected IDs (1, 2, 3, ...)
    expected_indices: list[str | int] = list(range(1, len(chains) + 1))

    # Use provider's batch classification with id_field="chain"
    results = provider.classify_batch(
        chains_text,
        expected_indices,
        system_prompt,
        model,
        id_field="chain",
    )

    # Convert keys to int for our return type
    return {int(k): v for k, v in results.items()}


def classify_chains_and_store_batch(
    chains: list[dict],
    prompt_id: str,
    provider_name: str | None = None,
    model: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
    context_id: str | None = None,
) -> dict[str, dict]:
    """
    Classify chains and store results for all unclassified_ids in each chain.

    For each chain result, the same classification is stored for ALL tweet IDs
    in chain["unclassified_ids"]. This matches how classifier.py works - all
    tweets in a chain get the same classification.

    Args:
        chains: List of chain dicts from assemble_classification_chains.
        prompt_id: ID of the prompt to use from the prompts table.
        provider_name: LLM provider ("anthropic", "gemini"). Defaults to "anthropic".
        model: Model to use (defaults to provider's default model).
        db_path: Path to the database.
        context_id: Optional context ID to associate with classifications.

    Returns:
        Dict mapping tweet_id -> classification result for all classified tweets.
    """
    if not chains:
        return {}

    # Get actual model name for storage
    if provider_name is None:
        provider_name = get_default_provider()
    provider = get_provider(provider_name)
    actual_model = provider.get_model(model)

    # Classify all chains
    chain_results = classify_chains_batch(chains, prompt_id, provider_name, model)

    # Store results for each tweet in each chain
    import uuid

    tweet_results: dict[str, dict] = {}

    for chain_idx, chain in enumerate(chains, 1):
        result = chain_results.get(chain_idx, {"_error": "missing_from_response"})

        # Generate a batch ID for tweets in this chain
        batch_id = str(uuid.uuid4())

        # Apply same result to ALL unclassified tweets in this chain
        for tweet_id in chain["unclassified_ids"]:
            store_prompt_response(
                tweet_id,
                prompt_id,
                actual_model,
                result,
                db_path,
                classification_batch_id=batch_id,
                context_id=context_id,
            )
            tweet_results[tweet_id] = result

    return tweet_results
