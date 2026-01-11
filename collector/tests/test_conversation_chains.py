"""Tests for conversation chain computation."""

from database import (
    compute_conversation_chains,
    compute_single_chain,
    get_approved_descendants,
    get_conversation_roots,
    get_replies_for_tweet,
    store_tweets,
)
from tests.fixtures import approve_tweets, make_tweet


class TestComputeConversationChains:
    """Tests for compute_conversation_chains function."""

    def test_single_tweet_becomes_single_chain(self, test_db):
        """A single tweet with no replies becomes a chain of length 1."""
        tweets = [make_tweet(id="1", text="Single tweet")]
        store_tweets(tweets, test_db)
        approve_tweets(["1"], test_db)

        chains = compute_conversation_chains(["1"], "default", test_db)

        assert len(chains) == 1
        assert len(chains[0]["chain"]) == 1
        assert chains[0]["chain"][0]["id"] == "1"
        assert chains[0]["hidden_replies"] == {}

    def test_linear_thread_becomes_single_chain(self, test_db):
        """A->B->C becomes one chain [A, B, C]."""
        tweets = [
            make_tweet(id="1", text="Tweet A", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Tweet B", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="Tweet C", reply_to_tweet_id="2", created_at="2025-01-01T12:00:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        chains = compute_conversation_chains(["1", "2", "3"], "default", test_db)

        assert len(chains) == 1
        chain = chains[0]["chain"]
        assert len(chain) == 3
        assert [t["id"] for t in chain] == ["1", "2", "3"]
        assert chains[0]["hidden_replies"] == {}

    def test_branch_picks_longest_chain(self, test_db):
        """A->B->C and A->D picks [A, B, C] with D as hidden reply on A."""
        tweets = [
            make_tweet(id="1", text="Tweet A", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Tweet B", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="Tweet C", reply_to_tweet_id="2", created_at="2025-01-01T12:00:00"),
            make_tweet(id="4", text="Tweet D", reply_to_tweet_id="1", created_at="2025-01-01T11:30:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3", "4"], test_db)

        chains = compute_conversation_chains(["1", "2", "3", "4"], "default", test_db)

        # Should get one chain: [A, B, C] with D as hidden reply on A
        # D is accessible via modal, not shown as a separate chain
        assert len(chains) == 1

        main_chain = chains[0]
        assert [t["id"] for t in main_chain["chain"]] == ["1", "2", "3"]
        # A has 1 hidden reply (D)
        assert main_chain["hidden_replies"].get("1") == 1

    def test_equal_length_branches_stops_at_ambiguity(self, test_db):
        """A->B and A->C (equal length) shows just [A] with 2 hidden replies."""
        tweets = [
            make_tweet(id="1", text="Tweet A", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Tweet B", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="Tweet C", reply_to_tweet_id="1", created_at="2025-01-01T11:30:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        chains = compute_conversation_chains(["1", "2", "3"], "default", test_db)

        # Main chain should be just [A] because B and C are tied
        # B and C are accessible via modal, not shown as separate chains
        assert len(chains) == 1

        a_chain = chains[0]
        assert len(a_chain["chain"]) == 1
        assert a_chain["chain"][0]["id"] == "1"
        assert a_chain["hidden_replies"].get("1") == 2

    def test_deep_branch_picks_longest(self, test_db):
        """A->B->C and A->B->D->E picks [A, B, D, E] with C as hidden reply on B."""
        tweets = [
            make_tweet(id="1", text="Tweet A", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Tweet B", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="Tweet C", reply_to_tweet_id="2", created_at="2025-01-01T12:00:00"),
            make_tweet(id="4", text="Tweet D", reply_to_tweet_id="2", created_at="2025-01-01T12:30:00"),
            make_tweet(id="5", text="Tweet E", reply_to_tweet_id="4", created_at="2025-01-01T13:00:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3", "4", "5"], test_db)

        chains = compute_conversation_chains(["1", "2", "3", "4", "5"], "default", test_db)

        # Main chain should be [A, B, D, E] (depth 4)
        # C is a separate chain or hidden
        main_chain = max(chains, key=lambda c: len(c["chain"]))
        assert [t["id"] for t in main_chain["chain"]] == ["1", "2", "4", "5"]
        # B has 1 hidden reply (C)
        assert main_chain["hidden_replies"].get("2") == 1

    def test_multiple_roots(self, test_db):
        """Independent tweets become separate chains."""
        tweets = [
            make_tweet(id="1", text="Tweet A", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Tweet B", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="Tweet C", created_at="2025-01-01T12:00:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        chains = compute_conversation_chains(["1", "2", "3"], "default", test_db)

        assert len(chains) == 3
        # Each chain has length 1
        for chain in chains:
            assert len(chain["chain"]) == 1
            assert chain["hidden_replies"] == {}

    def test_parent_not_approved_makes_reply_a_root(self, test_db):
        """If A->B but only B is approved, B becomes a root."""
        tweets = [
            make_tweet(id="1", text="Tweet A", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Tweet B", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
        ]
        store_tweets(tweets, test_db)
        # Only approve B
        approve_tweets(["2"], test_db)

        chains = compute_conversation_chains(["2"], "default", test_db)

        assert len(chains) == 1
        assert len(chains[0]["chain"]) == 1
        assert chains[0]["chain"][0]["id"] == "2"

    def test_chains_sorted_by_created_at_descending(self, test_db):
        """Chains are sorted newest-first by the root tweet's created_at."""
        tweets = [
            make_tweet(id="1", text="Old tweet", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="New tweet", created_at="2025-01-02T10:00:00"),
            make_tweet(id="3", text="Middle tweet", created_at="2025-01-01T15:00:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        chains = compute_conversation_chains(["1", "2", "3"], "default", test_db)

        chain_ids = [c["chain"][0]["id"] for c in chains]
        # Newest first: 2, 3, 1
        assert chain_ids == ["2", "3", "1"]

    def test_empty_input_returns_empty(self, test_db):
        """Empty tweet list returns empty chains."""
        chains = compute_conversation_chains([], "default", test_db)
        assert chains == []


class TestGetRepliesForTweet:
    """Tests for get_replies_for_tweet function."""

    def test_returns_tweet_and_approved_replies(self, test_db):
        """Returns the parent tweet and its approved replies."""
        tweets = [
            make_tweet(id="1", text="Parent tweet", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Reply B", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="Reply C", reply_to_tweet_id="1", created_at="2025-01-01T12:00:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        result = get_replies_for_tweet("1", "default", test_db)

        assert result["tweet"]["id"] == "1"
        assert len(result["replies"]) == 2
        reply_ids = [r["tweet"]["id"] for r in result["replies"]]
        assert "2" in reply_ids
        assert "3" in reply_ids

    def test_only_returns_approved_replies(self, test_db):
        """Only approved replies are returned."""
        tweets = [
            make_tweet(id="1", text="Parent tweet", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Approved reply", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="Filtered reply", reply_to_tweet_id="1", created_at="2025-01-01T12:00:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2"], test_db)
        # Note: 3 is not approved

        result = get_replies_for_tweet("1", "default", test_db)

        assert len(result["replies"]) == 1
        assert result["replies"][0]["tweet"]["id"] == "2"

    def test_includes_reply_count_for_replies(self, test_db):
        """Each reply includes the count of its own approved replies."""
        tweets = [
            make_tweet(id="1", text="Parent", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Reply to parent", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="Reply to 2", reply_to_tweet_id="2", created_at="2025-01-01T12:00:00"),
            make_tweet(id="4", text="Another reply to 2", reply_to_tweet_id="2", created_at="2025-01-01T13:00:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3", "4"], test_db)

        result = get_replies_for_tweet("1", "default", test_db)

        assert len(result["replies"]) == 1
        assert result["replies"][0]["tweet"]["id"] == "2"
        # Tweet 2 has 2 approved replies (3 and 4)
        assert result["replies"][0]["reply_count"] == 2

    def test_nonexistent_tweet_returns_none(self, test_db):
        """Returns None for tweet if it doesn't exist."""
        result = get_replies_for_tweet("nonexistent", "default", test_db)
        assert result["tweet"] is None
        assert result["replies"] == []

    def test_replies_sorted_by_created_at(self, test_db):
        """Replies are sorted by created_at ascending (oldest first)."""
        tweets = [
            make_tweet(id="1", text="Parent", created_at="2025-01-01T10:00:00"),
            make_tweet(id="3", text="Late reply", reply_to_tweet_id="1", created_at="2025-01-01T14:00:00"),
            make_tweet(id="2", text="Early reply", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="4", text="Middle reply", reply_to_tweet_id="1", created_at="2025-01-01T12:00:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3", "4"], test_db)

        result = get_replies_for_tweet("1", "default", test_db)

        reply_ids = [r["tweet"]["id"] for r in result["replies"]]
        # Oldest first: 2, 4, 3
        assert reply_ids == ["2", "4", "3"]


class TestGetConversationRoots:
    """Tests for get_conversation_roots function (scalable root finding)."""

    def test_finds_standalone_tweets_as_roots(self, test_db):
        """Standalone tweets (no reply_to) are roots."""
        tweets = [
            make_tweet(id="1", text="Tweet A", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Tweet B", created_at="2025-01-01T11:00:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2"], test_db)

        roots, total = get_conversation_roots("default", db_path=test_db)

        assert total == 2
        assert len(roots) == 2
        root_ids = {r["id"] for r in roots}
        assert root_ids == {"1", "2"}

    def test_reply_with_unapproved_parent_is_root(self, test_db):
        """A reply whose parent is not approved becomes a root."""
        tweets = [
            make_tweet(id="1", text="Tweet A", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Tweet B", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
        ]
        store_tweets(tweets, test_db)
        # Only approve B
        approve_tweets(["2"], test_db)

        roots, total = get_conversation_roots("default", db_path=test_db)

        assert total == 1
        assert roots[0]["id"] == "2"

    def test_reply_with_approved_parent_is_not_root(self, test_db):
        """A reply whose parent is approved is NOT a root."""
        tweets = [
            make_tweet(id="1", text="Tweet A", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Tweet B", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2"], test_db)

        roots, total = get_conversation_roots("default", db_path=test_db)

        # Only A is a root, B is not
        assert total == 1
        assert roots[0]["id"] == "1"

    def test_pagination_works(self, test_db):
        """Pagination limit and offset work correctly."""
        tweets = [
            make_tweet(id="1", text="Tweet 1", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Tweet 2", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="Tweet 3", created_at="2025-01-01T12:00:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        # Get first page
        roots1, total = get_conversation_roots(
            "default", limit=2, offset=0, db_path=test_db
        )
        assert total == 3
        assert len(roots1) == 2

        # Get second page
        roots2, total = get_conversation_roots(
            "default", limit=2, offset=2, db_path=test_db
        )
        assert total == 3
        assert len(roots2) == 1

    def test_sorts_by_created_at_descending(self, test_db):
        """Roots are sorted by created_at descending (newest first)."""
        tweets = [
            make_tweet(id="1", text="Old", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="New", created_at="2025-01-02T10:00:00"),
            make_tweet(id="3", text="Mid", created_at="2025-01-01T15:00:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        roots, _ = get_conversation_roots(
            "default", sort_field="created_at", db_path=test_db
        )

        root_ids = [r["id"] for r in roots]
        assert root_ids == ["2", "3", "1"]


class TestGetApprovedDescendants:
    """Tests for get_approved_descendants function."""

    def test_returns_root_only_if_no_children(self, test_db):
        """Returns just the root if it has no approved children."""
        tweets = [make_tweet(id="1", text="Solo tweet")]
        store_tweets(tweets, test_db)
        approve_tweets(["1"], test_db)

        descendants = get_approved_descendants("1", "default", test_db)

        assert len(descendants) == 1
        assert "1" in descendants

    def test_returns_all_approved_descendants(self, test_db):
        """Returns all approved tweets in the subtree."""
        tweets = [
            make_tweet(id="1", text="Root", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Child", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="Grandchild", reply_to_tweet_id="2", created_at="2025-01-01T12:00:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        descendants = get_approved_descendants("1", "default", test_db)

        assert len(descendants) == 3
        assert set(descendants.keys()) == {"1", "2", "3"}

    def test_excludes_unapproved_descendants(self, test_db):
        """Unapproved descendants are not included."""
        tweets = [
            make_tweet(id="1", text="Root", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="Approved child", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="Unapproved child", reply_to_tweet_id="1", created_at="2025-01-01T12:00:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2"], test_db)

        descendants = get_approved_descendants("1", "default", test_db)

        assert len(descendants) == 2
        assert "3" not in descendants


class TestComputeSingleChain:
    """Tests for compute_single_chain function (scalable per-chain computation)."""

    def test_single_tweet_chain(self, test_db):
        """A single tweet becomes a chain of length 1."""
        tweets = [make_tweet(id="1", text="Solo")]
        store_tweets(tweets, test_db)
        approve_tweets(["1"], test_db)

        result = compute_single_chain("1", "default", test_db)

        assert len(result["chain"]) == 1
        assert result["chain"][0]["id"] == "1"
        assert result["hidden_replies"] == {}

    def test_linear_chain(self, test_db):
        """A->B->C becomes chain [A, B, C]."""
        tweets = [
            make_tweet(id="1", text="A", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="B", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="C", reply_to_tweet_id="2", created_at="2025-01-01T12:00:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        result = compute_single_chain("1", "default", test_db)

        assert [t["id"] for t in result["chain"]] == ["1", "2", "3"]
        assert result["hidden_replies"] == {}

    def test_branch_picks_longest(self, test_db):
        """A->B->C and A->D picks [A, B, C] with hidden reply on A."""
        tweets = [
            make_tweet(id="1", text="A", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="B", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="C", reply_to_tweet_id="2", created_at="2025-01-01T12:00:00"),
            make_tweet(id="4", text="D", reply_to_tweet_id="1", created_at="2025-01-01T11:30:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3", "4"], test_db)

        result = compute_single_chain("1", "default", test_db)

        assert [t["id"] for t in result["chain"]] == ["1", "2", "3"]
        assert result["hidden_replies"]["1"] == 1

    def test_tie_stops_chain(self, test_db):
        """A->B and A->C (equal length) stops at [A] with 2 hidden."""
        tweets = [
            make_tweet(id="1", text="A", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="B", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="C", reply_to_tweet_id="1", created_at="2025-01-01T11:30:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        result = compute_single_chain("1", "default", test_db)

        assert [t["id"] for t in result["chain"]] == ["1"]
        assert result["hidden_replies"]["1"] == 2

    def test_matches_compute_conversation_chains(self, test_db):
        """compute_single_chain produces same result as compute_conversation_chains."""
        tweets = [
            make_tweet(id="1", text="A", created_at="2025-01-01T10:00:00"),
            make_tweet(id="2", text="B", reply_to_tweet_id="1", created_at="2025-01-01T11:00:00"),
            make_tweet(id="3", text="C", reply_to_tweet_id="2", created_at="2025-01-01T12:00:00"),
            make_tweet(id="4", text="D", reply_to_tweet_id="1", created_at="2025-01-01T11:30:00"),
        ]
        store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3", "4"], test_db)

        # Old method
        old_chains = compute_conversation_chains(["1", "2", "3", "4"], "default", test_db)

        # New method
        new_chain = compute_single_chain("1", "default", test_db)

        # Should produce same chain structure
        assert len(old_chains) == 1
        old_chain_ids = [t["id"] for t in old_chains[0]["chain"]]
        new_chain_ids = [t["id"] for t in new_chain["chain"]]
        assert old_chain_ids == new_chain_ids
        assert old_chains[0]["hidden_replies"] == new_chain["hidden_replies"]
