"""
Extractor functions for mode-based tweet filtering.

Each extractor takes an LLM response dict and returns a boolean decision.
Extractors are referenced by name in the modes table (e.g., "extractors.binary_approved").
"""

from typing import Any


def binary_approved(response: dict[str, Any]) -> bool:
    """
    Simple extractor for binary yes/no prompts.
    Expects response like: {"approved": true/false, "reason": "..."}
    """
    return bool(response.get("approved", False))


def topic_contains(topic: str):
    """
    Factory for topic-based extractors.
    Returns an extractor that checks if a topic is in the response's topics list.

    Usage in modes table: "extractors.topic_ml" (requires registering the function)
    """
    def extractor(response: dict[str, Any]) -> bool:
        topics = response.get("topics", [])
        return topic.lower() in [t.lower() for t in topics]
    return extractor


def low_toxicity(threshold: float = 0.3):
    """
    Factory for toxicity threshold extractors.
    Returns True if toxicity score is below threshold.
    """
    def extractor(response: dict[str, Any]) -> bool:
        scores = response.get("scores", {})
        toxicity = scores.get("toxicity", 0.0)
        return toxicity < threshold
    return extractor


def combined(*conditions):
    """
    Factory for combining multiple conditions with AND logic.

    Example:
        combined(topic_contains("ml"), low_toxicity(0.2))
    """
    def extractor(response: dict[str, Any]) -> bool:
        return all(cond(response) for cond in conditions)
    return extractor


# =============================================================================
# Pre-built extractors for common modes
# =============================================================================

# Simple binary approval
def default(response: dict[str, Any]) -> bool:
    """Default extractor - looks for 'approved' field."""
    return binary_approved(response)


# Topic-specific extractors
def topic_ml(response: dict[str, Any]) -> bool:
    """Show ML/AI related content."""
    topics = response.get("topics", [])
    ml_topics = {"ml", "machine_learning", "ai", "deep_learning", "neural_networks"}
    return bool(set(t.lower() for t in topics) & ml_topics)


def topic_spirituality(response: dict[str, Any]) -> bool:
    """Show spirituality/dharma content."""
    topics = response.get("topics", [])
    spirit_topics = {"spirituality", "dharma", "meditation", "buddhism", "philosophy"}
    return bool(set(t.lower() for t in topics) & spirit_topics)


def topic_tech(response: dict[str, Any]) -> bool:
    """Show general tech content."""
    topics = response.get("topics", [])
    tech_topics = {"tech", "programming", "software", "engineering", "startups", "ml", "ai"}
    return bool(set(t.lower() for t in topics) & tech_topics)


# Quality-based extractors
def high_quality(response: dict[str, Any]) -> bool:
    """Show only high-quality, non-toxic content."""
    scores = response.get("scores", {})
    toxicity = scores.get("toxicity", 1.0)
    engagement_bait = scores.get("engagement_bait", 1.0)
    informativeness = scores.get("informativeness", 0.0)

    return (
        toxicity < 0.2 and
        engagement_bait < 0.3 and
        informativeness > 0.4
    )


def chill_vibes(response: dict[str, Any]) -> bool:
    """Show only chill, positive content."""
    scores = response.get("scores", {})
    toxicity = scores.get("toxicity", 1.0)
    engagement_bait = scores.get("engagement_bait", 1.0)
    positivity = scores.get("positivity", 0.0)

    return toxicity < 0.1 and engagement_bait < 0.2 and positivity > 0.5


# =============================================================================
# Extractor registry
# =============================================================================

# Map of extractor names to functions
# This allows looking up extractors by string name from the database
EXTRACTORS = {
    "default": default,
    "binary_approved": binary_approved,
    "topic_ml": topic_ml,
    "topic_spirituality": topic_spirituality,
    "topic_tech": topic_tech,
    "high_quality": high_quality,
    "chill_vibes": chill_vibes,
}


def get_extractor(name: str):
    """
    Get an extractor function by name.
    Raises KeyError if not found.
    """
    if name not in EXTRACTORS:
        raise KeyError(f"Unknown extractor: {name}. Available: {list(EXTRACTORS.keys())}")
    return EXTRACTORS[name]


def register_extractor(name: str, func):
    """Register a custom extractor function."""
    EXTRACTORS[name] = func
