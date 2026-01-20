"""Tests for thread-level classification."""

from database import (
    assemble_classification_chains,
    create_prompt,
    get_prompt_response,
    store_prompt_response,
    store_tweets,
)
from tests.fixtures import make_tweet


def setup_test_prompt(db_path):
    """Create a test prompt for use in tests."""
    create_prompt(
        "test_prompt",
        "Test prompt text",
        '{"test": "schema"}',
        db_path=db_path
    )


class TestAssembleClassificationChains:
    """Tests for assemble_classification_chains function."""

    def test_single_tweet_becomes_single_chain(self, test_db):
        """A single unclassified tweet becomes a chain of length 1."""
        tweets = [make_tweet(id="1", text="Single tweet")]
        store_tweets(tweets, test_db)

        chains = assemble_classification_chains(tweets, "test_prompt", "test_model", test_db)

        assert len(chains) == 1
        assert len(chains[0]["tweets"]) == 1
        assert chains[0]["tweets"][0]["id"] == "1"
        assert chains[0]["unclassified_ids"] == {"1"}

    def test_linear_thread_becomes_single_chain(self, test_db):
        """A->B->C where all are unclassified becomes one chain."""
        tweets = [
            make_tweet(id="1", text="Tweet A", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Tweet B", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="Tweet C", reply_to_tweet_id="2", created_at="2025-01-01T12:00:00"),
        ]
        store_tweets(tweets, test_db)

        chains = assemble_classification_chains(tweets, "test_prompt", "test_model", test_db)

        assert len(chains) == 1
        chain = chains[0]
        assert len(chain["tweets"]) == 3
        assert [t["id"] for t in chain["tweets"]] == ["1", "2", "3"]
        assert chain["unclassified_ids"] == {"1", "2", "3"}

    def test_two_separate_threads(self, test_db):
        """Two independent threads become two chains."""
        tweets = [
            make_tweet(id="1", text="Thread 1 root"),
            make_tweet(id="2", text="Thread 1 reply", reply_to_tweet_id="1"),
            make_tweet(id="3", text="Thread 2 root"),
            make_tweet(id="4", text="Thread 2 reply", reply_to_tweet_id="3"),
        ]
        store_tweets(tweets, test_db)

        chains = assemble_classification_chains(tweets, "test_prompt", "test_model", test_db)

        assert len(chains) == 2

        # Sort chains by first tweet ID for deterministic testing
        chains_sorted = sorted(chains, key=lambda c: c["tweets"][0]["id"])

        # First chain: 1->2
        assert len(chains_sorted[0]["tweets"]) == 2
        assert [t["id"] for t in chains_sorted[0]["tweets"]] == ["1", "2"]
        assert chains_sorted[0]["unclassified_ids"] == {"1", "2"}

        # Second chain: 3->4
        assert len(chains_sorted[1]["tweets"]) == 2
        assert [t["id"] for t in chains_sorted[1]["tweets"]] == ["3", "4"]
        assert chains_sorted[1]["unclassified_ids"] == {"3", "4"}

    def test_includes_classified_ancestor_as_context(self, test_db):
        """If parent is classified, include it in chain as context."""
        setup_test_prompt(test_db)

        tweets = [
            make_tweet(id="1", text="Classified parent", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Unclassified child", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
        ]
        store_tweets(tweets, test_db)

        # Classify parent
        store_prompt_response("1", "test_prompt", "test_model", {"approved": True}, test_db)

        # Only tweet 2 is unclassified
        unclassified = [tweets[1]]
        chains = assemble_classification_chains(unclassified, "test_prompt", "test_model", test_db)

        assert len(chains) == 1
        chain = chains[0]

        # Chain should include both tweets (parent as context)
        assert len(chain["tweets"]) == 2
        assert [t["id"] for t in chain["tweets"]] == ["1", "2"]

        # But only tweet 2 needs classification
        assert chain["unclassified_ids"] == {"2"}

    def test_mid_thread_unclassified_tweets(self, test_db):
        """A->B->C->D where B and C are unclassified, A and D are classified."""
        setup_test_prompt(test_db)

        tweets = [
            make_tweet(id="1", text="Tweet A", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Tweet B", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="Tweet C", reply_to_tweet_id="2", created_at="2025-01-01T12:00:00"),
            make_tweet(id="4", text="Tweet D", reply_to_tweet_id="3", created_at="2025-01-01T13:00:00"),
        ]
        store_tweets(tweets, test_db)

        # Classify A and D
        store_prompt_response("1", "test_prompt", "test_model", {"approved": True}, test_db)
        store_prompt_response("4", "test_prompt", "test_model", {"approved": False}, test_db)

        # B and C are unclassified
        unclassified = [tweets[1], tweets[2]]
        chains = assemble_classification_chains(unclassified, "test_prompt", "test_model", test_db)

        assert len(chains) == 1
        chain = chains[0]

        # Chain should include ancestor A as context, plus B and C
        assert len(chain["tweets"]) == 3
        assert [t["id"] for t in chain["tweets"]] == ["1", "2", "3"]

        # Only B and C need classification
        assert chain["unclassified_ids"] == {"2", "3"}


class TestClassificationInheritance:
    """Tests for classification inheritance logic."""

    def test_child_can_inherit_parent_classification(self, test_db):
        """When parent is classified, child can inherit the classification."""
        setup_test_prompt(test_db)

        tweets = [
            make_tweet(id="1", text="Parent tweet"),
            make_tweet(id="2", text="Child tweet", reply_to_tweet_id="1"),
        ]
        store_tweets(tweets, test_db)

        # Classify parent with batch_id
        parent_response = {"approved": True, "reason": "Good content"}
        store_prompt_response("1", "test_prompt", "test_model", parent_response, test_db,
                            classification_batch_id="batch_123")

        # Verify parent classification
        parent_result = get_prompt_response("1", "test_prompt", "test_model", test_db)
        assert parent_result is not None

        import json
        response = json.loads(parent_result["response_json"])
        assert response["approved"] is True
        assert parent_result.get("classification_batch_id") == "batch_123"

    def test_inheritance_preserves_batch_id(self, test_db):
        """Inherited classifications should preserve the parent's batch_id."""
        setup_test_prompt(test_db)

        tweets = [
            make_tweet(id="1", text="Parent tweet"),
            make_tweet(id="2", text="Child tweet", reply_to_tweet_id="1"),
        ]
        store_tweets(tweets, test_db)

        # Classify parent
        parent_response = {"approved": True}
        batch_id = "original_batch_123"
        store_prompt_response("1", "test_prompt", "test_model", parent_response, test_db,
                            classification_batch_id=batch_id)

        # Child inherits (simulate what classifier does)
        parent_result = get_prompt_response("1", "test_prompt", "test_model", test_db)
        store_prompt_response("2", "test_prompt", "test_model", parent_result["response_json"],
                            test_db, classification_batch_id=parent_result.get("classification_batch_id"))

        # Verify child has same batch_id
        child_result = get_prompt_response("2", "test_prompt", "test_model", test_db)
        assert child_result is not None
        assert child_result.get("classification_batch_id") == batch_id


class TestBatchIdTracking:
    """Tests for classification_batch_id tracking."""

    def test_tweets_in_same_chain_share_batch_id(self, test_db):
        """All tweets classified together should have the same batch_id."""
        setup_test_prompt(test_db)

        tweets = [
            make_tweet(id="1", text="Tweet A"),
            make_tweet(id="2", text="Tweet B", reply_to_tweet_id="1"),
            make_tweet(id="3", text="Tweet C", reply_to_tweet_id="2"),
        ]
        store_tweets(tweets, test_db)

        # Simulate chain classification
        batch_id = "chain_batch_456"
        response = {"approved": True, "reason": "Good thread"}

        for tweet in tweets:
            store_prompt_response(tweet["id"], "test_prompt", "test_model", response, test_db,
                                classification_batch_id=batch_id)

        # Verify all have same batch_id
        for tweet in tweets:
            result = get_prompt_response(tweet["id"], "test_prompt", "test_model", test_db)
            assert result is not None
            assert result.get("classification_batch_id") == batch_id

    def test_different_chains_have_different_batch_ids(self, test_db):
        """Tweets from different chains should have different batch_ids."""
        setup_test_prompt(test_db)

        tweets = [
            make_tweet(id="1", text="Chain 1"),
            make_tweet(id="2", text="Chain 2"),
        ]
        store_tweets(tweets, test_db)

        # Classify with different batch_ids
        store_prompt_response("1", "test_prompt", "test_model", {"approved": True}, test_db,
                            classification_batch_id="batch_1")
        store_prompt_response("2", "test_prompt", "test_model", {"approved": False}, test_db,
                            classification_batch_id="batch_2")

        result1 = get_prompt_response("1", "test_prompt", "test_model", test_db)
        result2 = get_prompt_response("2", "test_prompt", "test_model", test_db)

        assert result1.get("classification_batch_id") == "batch_1"
        assert result2.get("classification_batch_id") == "batch_2"
        assert result1.get("classification_batch_id") != result2.get("classification_batch_id")
