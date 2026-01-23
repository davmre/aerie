"""Tests for Bluesky thread context fetching."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bluesky_poller import (
    fetch_thread_context,
    normalize_thread_view_post,
)
from database import get_bluesky_post_ids, init_database, store_tweets


@pytest.fixture
def bluesky_db(tmp_path):
    """Create a fresh test database with Bluesky posts."""
    db_path = tmp_path / "test.db"
    init_database(db_path)
    return db_path


class MockAuthor:
    """Mock Bluesky author object."""

    def __init__(self, handle: str, did: str, display_name: str | None = None):
        self.handle = handle
        self.did = did
        self.display_name = display_name or handle
        self.description = None
        self.followers_count = None


class MockRecord:
    """Mock Bluesky post record."""

    def __init__(
        self,
        text: str,
        created_at: str = "2025-01-01T12:00:00Z",
        reply: MagicMock | None = None,
    ):
        self.text = text
        self.created_at = created_at
        self.reply = reply
        self.facets = None
        self.langs = ["en"]


class MockPostView:
    """Mock Bluesky PostView."""

    def __init__(self, uri: str, cid: str, author: MockAuthor, record: MockRecord):
        self.uri = uri
        self.cid = cid
        self.author = author
        self.record = record
        self.embed = None
        self.labels = None
        self.like_count = 5
        self.repost_count = 2
        self.reply_count = 1
        self.quote_count = 0


class MockThreadViewPost:
    """Mock Bluesky ThreadViewPost."""

    def __init__(self, post: MockPostView, parent: Any = None):
        self.post = post
        self.parent = parent
        self.py_type = "app.bsky.feed.defs#threadViewPost"


class MockBlockedPost:
    """Mock Bluesky blocked post."""

    def __init__(self):
        self.py_type = "app.bsky.feed.defs#blockedPost"
        self.post = None  # Blocked posts don't have post attribute


class MockNotFoundPost:
    """Mock Bluesky not found post."""

    def __init__(self):
        self.py_type = "app.bsky.feed.defs#notFoundPost"
        self.post = None


class TestGetBlueskyPostIds:
    """Tests for get_bluesky_post_ids()."""

    def test_empty_database(self, bluesky_db):
        """Should return empty set for empty database."""
        ids = get_bluesky_post_ids(bluesky_db)
        assert ids == set()

    def test_only_bluesky_posts(self, bluesky_db):
        """Should return only Bluesky post IDs."""
        tweets = [
            {
                "id": "bsky:at://did:plc:abc/app.bsky.feed.post/123",
                "text": "Bluesky post 1",
                "platform": "bluesky",
                "author": {"username": "user1.bsky.social"},
            },
            {
                "id": "bsky:at://did:plc:def/app.bsky.feed.post/456",
                "text": "Bluesky post 2",
                "platform": "bluesky",
                "author": {"username": "user2.bsky.social"},
            },
        ]
        store_tweets(tweets, bluesky_db)

        ids = get_bluesky_post_ids(bluesky_db)
        assert ids == {
            "bsky:at://did:plc:abc/app.bsky.feed.post/123",
            "bsky:at://did:plc:def/app.bsky.feed.post/456",
        }

    def test_excludes_twitter_posts(self, bluesky_db):
        """Should not include Twitter posts."""
        tweets = [
            {
                "id": "bsky:at://did:plc:abc/app.bsky.feed.post/123",
                "text": "Bluesky post",
                "platform": "bluesky",
                "author": {"username": "user1.bsky.social"},
            },
            {
                "id": "1234567890",
                "text": "Twitter post",
                "platform": "twitter",
                "author": {"username": "twitter_user"},
            },
        ]
        store_tweets(tweets, bluesky_db)

        ids = get_bluesky_post_ids(bluesky_db)
        assert ids == {"bsky:at://did:plc:abc/app.bsky.feed.post/123"}


class TestNormalizeThreadViewPost:
    """Tests for normalize_thread_view_post()."""

    def test_normalizes_basic_post(self):
        """Should normalize a basic thread view post."""
        author = MockAuthor("user.bsky.social", "did:plc:abc123")
        record = MockRecord("Hello from Bluesky!")
        post_view = MockPostView(
            uri="at://did:plc:abc123/app.bsky.feed.post/xyz",
            cid="bafyreiabc",
            author=author,
            record=record,
        )
        thread_post = MockThreadViewPost(post_view)

        result = normalize_thread_view_post(thread_post)

        assert result is not None
        assert result["id"] == "bsky:at://did:plc:abc123/app.bsky.feed.post/xyz"
        assert result["text"] == "Hello from Bluesky!"
        assert result["platform"] == "bluesky"
        assert result["author"]["username"] == "user.bsky.social"
        assert result["author"]["id"] == "did:plc:abc123"
        assert result["metrics"]["like_count"] == 5
        assert result["metrics"]["repost_count"] == 2

    def test_normalizes_reply_info(self):
        """Should extract reply info from thread view post."""
        author = MockAuthor("user.bsky.social", "did:plc:abc123")
        reply_ref = MagicMock()
        reply_ref.parent = MagicMock(uri="at://did:plc:other/app.bsky.feed.post/parent")
        reply_ref.root = MagicMock(uri="at://did:plc:other/app.bsky.feed.post/root")
        record = MockRecord("This is a reply", reply=reply_ref)
        post_view = MockPostView(
            uri="at://did:plc:abc123/app.bsky.feed.post/reply",
            cid="bafyreiabc",
            author=author,
            record=record,
        )
        thread_post = MockThreadViewPost(post_view)

        result = normalize_thread_view_post(thread_post)

        assert result is not None
        assert result["reply_to"] == {
            "tweet_id": "bsky:at://did:plc:other/app.bsky.feed.post/parent"
        }

    def test_returns_none_for_blocked_post(self):
        """Should return None for blocked posts."""
        blocked = MockBlockedPost()
        result = normalize_thread_view_post(blocked)
        assert result is None

    def test_returns_none_for_not_found_post(self):
        """Should return None for not found posts."""
        not_found = MockNotFoundPost()
        result = normalize_thread_view_post(not_found)
        assert result is None

    def test_custom_domain_verified(self):
        """Should mark custom domain users as verified."""
        author = MockAuthor("customdomain.com", "did:plc:abc123")
        record = MockRecord("Verified post")
        post_view = MockPostView(
            uri="at://did:plc:abc123/app.bsky.feed.post/xyz",
            cid="bafyreiabc",
            author=author,
            record=record,
        )
        thread_post = MockThreadViewPost(post_view)

        result = normalize_thread_view_post(thread_post)

        assert result is not None
        assert result["author"]["verified"] is True

    def test_bsky_social_not_verified(self):
        """Should not mark .bsky.social users as verified."""
        author = MockAuthor("regular.bsky.social", "did:plc:abc123")
        record = MockRecord("Regular post")
        post_view = MockPostView(
            uri="at://did:plc:abc123/app.bsky.feed.post/xyz",
            cid="bafyreiabc",
            author=author,
            record=record,
        )
        thread_post = MockThreadViewPost(post_view)

        result = normalize_thread_view_post(thread_post)

        assert result is not None
        assert result["author"]["verified"] is False


class TestFetchThreadContext:
    """Tests for fetch_thread_context()."""

    def test_fetches_parent_chain(self):
        """Should fetch and return parent posts in chronological order."""
        # Build a chain: grandparent -> parent -> reply
        grandparent_author = MockAuthor("gp.bsky.social", "did:plc:gp")
        grandparent_record = MockRecord("Grandparent post")
        grandparent_view = MockPostView(
            uri="at://did:plc:gp/app.bsky.feed.post/gp",
            cid="bafyreigp",
            author=grandparent_author,
            record=grandparent_record,
        )
        grandparent_thread = MockThreadViewPost(grandparent_view, parent=None)

        parent_author = MockAuthor("parent.bsky.social", "did:plc:parent")
        parent_record = MockRecord("Parent post")
        parent_view = MockPostView(
            uri="at://did:plc:parent/app.bsky.feed.post/parent",
            cid="bafyreiparent",
            author=parent_author,
            record=parent_record,
        )
        parent_thread = MockThreadViewPost(parent_view, parent=grandparent_thread)

        reply_author = MockAuthor("reply.bsky.social", "did:plc:reply")
        reply_record = MockRecord("Reply post")
        reply_view = MockPostView(
            uri="at://did:plc:reply/app.bsky.feed.post/reply",
            cid="bafyreireply",
            author=reply_author,
            record=reply_record,
        )
        reply_thread = MockThreadViewPost(reply_view, parent=parent_thread)

        mock_response = MagicMock()
        mock_response.thread = reply_thread

        mock_client = MagicMock()
        mock_client.get_post_thread.return_value = mock_response

        existing_ids: set[str] = set()
        parents = fetch_thread_context(
            mock_client,
            "bsky:at://did:plc:reply/app.bsky.feed.post/reply",
            existing_ids,
        )

        assert len(parents) == 3
        # Should be chronological order (oldest first): grandparent, parent, then queried post
        assert parents[0]["text"] == "Grandparent post"
        assert parents[1]["text"] == "Parent post"
        assert parents[2]["text"] == "Reply post"

    def test_stops_at_existing_post(self):
        """Should stop walking ancestors when existing post is found, but still return queried post."""
        parent_author = MockAuthor("parent.bsky.social", "did:plc:parent")
        parent_record = MockRecord("Parent post")
        parent_view = MockPostView(
            uri="at://did:plc:parent/app.bsky.feed.post/parent",
            cid="bafyreiparent",
            author=parent_author,
            record=parent_record,
        )
        parent_thread = MockThreadViewPost(parent_view, parent=None)

        reply_author = MockAuthor("reply.bsky.social", "did:plc:reply")
        reply_record = MockRecord("Reply post")
        reply_view = MockPostView(
            uri="at://did:plc:reply/app.bsky.feed.post/reply",
            cid="bafyreireply",
            author=reply_author,
            record=reply_record,
        )
        reply_thread = MockThreadViewPost(reply_view, parent=parent_thread)

        mock_response = MagicMock()
        mock_response.thread = reply_thread

        mock_client = MagicMock()
        mock_client.get_post_thread.return_value = mock_response

        # Mark parent as already existing
        existing_ids = {"bsky:at://did:plc:parent/app.bsky.feed.post/parent"}
        parents = fetch_thread_context(
            mock_client,
            "bsky:at://did:plc:reply/app.bsky.feed.post/reply",
            existing_ids,
        )

        # Should return just the queried post (reply), not the parent we already have
        assert len(parents) == 1
        assert parents[0]["text"] == "Reply post"

    def test_skips_already_existing_queried_post(self):
        """Should return empty if queried post is already in existing_ids."""
        reply_author = MockAuthor("reply.bsky.social", "did:plc:reply")
        reply_record = MockRecord("Reply post")
        reply_view = MockPostView(
            uri="at://did:plc:reply/app.bsky.feed.post/reply",
            cid="bafyreireply",
            author=reply_author,
            record=reply_record,
        )
        reply_thread = MockThreadViewPost(reply_view, parent=None)

        mock_response = MagicMock()
        mock_response.thread = reply_thread

        mock_client = MagicMock()
        mock_client.get_post_thread.return_value = mock_response

        # Mark the queried post itself as already existing
        existing_ids = {"bsky:at://did:plc:reply/app.bsky.feed.post/reply"}
        parents = fetch_thread_context(
            mock_client,
            "bsky:at://did:plc:reply/app.bsky.feed.post/reply",
            existing_ids,
        )

        # Should return empty since we already have the queried post
        assert len(parents) == 0

    def test_handles_api_error(self):
        """Should return empty list on API error."""
        mock_client = MagicMock()
        mock_client.get_post_thread.side_effect = Exception("API error")

        existing_ids: set[str] = set()
        parents = fetch_thread_context(
            mock_client,
            "bsky:at://did:plc:abc/app.bsky.feed.post/xyz",
            existing_ids,
        )

        assert parents == []

    def test_handles_blocked_parent(self):
        """Should return queried post but stop at blocked ancestor."""
        blocked = MockBlockedPost()

        reply_author = MockAuthor("reply.bsky.social", "did:plc:reply")
        reply_record = MockRecord("Reply post")
        reply_view = MockPostView(
            uri="at://did:plc:reply/app.bsky.feed.post/reply",
            cid="bafyreireply",
            author=reply_author,
            record=reply_record,
        )
        reply_thread = MockThreadViewPost(reply_view, parent=blocked)

        mock_response = MagicMock()
        mock_response.thread = reply_thread

        mock_client = MagicMock()
        mock_client.get_post_thread.return_value = mock_response

        existing_ids: set[str] = set()
        parents = fetch_thread_context(
            mock_client,
            "bsky:at://did:plc:reply/app.bsky.feed.post/reply",
            existing_ids,
        )

        # Should return just the queried post; parent is blocked so we stop there
        assert len(parents) == 1
        assert parents[0]["text"] == "Reply post"

    def test_strips_bsky_prefix(self):
        """Should handle URIs with or without bsky: prefix."""
        mock_response = MagicMock()
        mock_response.thread = MagicMock()
        mock_response.thread.parent = None

        mock_client = MagicMock()
        mock_client.get_post_thread.return_value = mock_response

        existing_ids: set[str] = set()

        # Call with bsky: prefix
        fetch_thread_context(
            mock_client,
            "bsky:at://did:plc:abc/app.bsky.feed.post/xyz",
            existing_ids,
        )

        # Should strip the prefix when calling API
        mock_client.get_post_thread.assert_called_with(
            uri="at://did:plc:abc/app.bsky.feed.post/xyz", parent_height=50
        )


class TestPollAndStoreWithThreadContext:
    """Integration tests for poll_and_store with thread context fetching."""

    def test_fetches_thread_context_for_replies(self, bluesky_db):
        """Should fetch thread context for reply posts."""
        # Create a reply post in the timeline
        reply_author = MockAuthor("reply.bsky.social", "did:plc:reply")
        reply_ref = MagicMock()
        reply_ref.parent = MagicMock(uri="at://did:plc:parent/app.bsky.feed.post/parent")
        reply_ref.root = None
        reply_record = MockRecord("This is a reply", reply=reply_ref)
        reply_post = MockPostView(
            uri="at://did:plc:reply/app.bsky.feed.post/reply",
            cid="bafyreireply",
            author=reply_author,
            record=reply_record,
        )

        # Create feed item (FeedViewPost wrapper)
        feed_item = MagicMock()
        feed_item.post = reply_post
        feed_item.reason = None

        # Mock the parent thread response
        parent_author = MockAuthor("parent.bsky.social", "did:plc:parent")
        parent_record = MockRecord("Parent post")
        parent_view = MockPostView(
            uri="at://did:plc:parent/app.bsky.feed.post/parent",
            cid="bafyreiparent",
            author=parent_author,
            record=parent_record,
        )
        parent_thread = MockThreadViewPost(parent_view, parent=None)

        reply_thread = MockThreadViewPost(
            MagicMock(uri="at://did:plc:reply/app.bsky.feed.post/reply"),
            parent=parent_thread,
        )

        mock_thread_response = MagicMock()
        mock_thread_response.thread = reply_thread

        # Mock timeline response with proper structure
        mock_timeline_response = MagicMock()
        mock_timeline_response.feed = [feed_item]
        mock_timeline_response.cursor = None

        mock_client = MagicMock()
        mock_client.get_timeline.return_value = mock_timeline_response
        mock_client.get_post_thread.return_value = mock_thread_response

        # Import and run poll_and_store
        from bluesky_poller import poll_and_store

        with patch("bluesky_poller.time.sleep"):  # Speed up test
            stats = poll_and_store(
                mock_client,
                db_path=bluesky_db,
                limit=50,
                verbose=False,
                fetch_threads=True,
            )

        # Should have inserted the reply and the parent
        assert stats["posts_inserted"] == 1  # The reply
        assert stats["thread_context_inserted"] == 1  # The parent

        # Verify both posts are in the database
        ids = get_bluesky_post_ids(bluesky_db)
        assert "bsky:at://did:plc:reply/app.bsky.feed.post/reply" in ids
        assert "bsky:at://did:plc:parent/app.bsky.feed.post/parent" in ids
