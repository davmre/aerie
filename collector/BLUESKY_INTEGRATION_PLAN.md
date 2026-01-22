# Bluesky Integration Plan

This document captures the plan for adding Bluesky support to Aerie, allowing the web UI to display a filtered mix of Twitter and Bluesky content.

## Overview

Aerie's core value proposition—LLM-based content filtering—is platform-agnostic. The classifier doesn't care where content comes from. The main work involves:

1. **Data model normalization** - Mapping platform-specific fields to a common schema
2. **Capture mechanism** - Bluesky is actually easier than Twitter (direct API, no browser interception)
3. **Web UI updates** - Displaying mixed feeds with platform context

## Architecture

```
┌─────────────────────────┐
│  Firefox Extension      │  (Twitter only)
│  Intercepts GraphQL     │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐     ┌─────────────────────────┐
│  Collector Service      │ ◄── │  Bluesky Poller        │
│  (Platform Agnostic)    │     │  (Uses ATProto API)     │
│  ├── Unified DB         │     │  Polls getTimeline      │
│  └── Platform Adapters  │     └─────────────────────────┘
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│  Classifier             │  (Unchanged - platform agnostic!)
│  Works on any content   │
└─────────────────────────┘
            │
            ▼
┌─────────────────────────┐
│  Web UI                 │
│  Filters by platform    │
│  Shows mixed feed       │
└─────────────────────────┘
```

## Implementation Phases

### Phase 1: Data Model ✅ COMPLETED

**Status**: Done (commit `07a006f`)

Added platform support to the database schema:

- `platform` column in tweets table (defaults to 'twitter')
- `platform_metadata` column for platform-specific JSON data
- `platform` column in retweets table
- Index on `tweets.platform` for efficient filtering
- Updated `store_tweets()` and `store_retweets()` to handle platform field
- Added `platform` parameter to `get_conversation_roots()` and `get_cached_decision_stats()`
- Added `get_platform_stats()` helper function
- Created migration script (`migrate_add_platform.py`)

**ID Format Convention**:
```python
twitter_id = "twitter:1234567890"
bluesky_id = "bsky:at://did:plc:xxx/app.bsky.feed.post/abc123"
```

**Platform Metadata Examples**:
```python
# Twitter (most fields already in schema)
{"blue_verified": true, "is_promoted": false}

# Bluesky (important for verification and threading)
{
  "cid": "bafyreig...",      # Content hash for verification
  "root_uri": "at://...",    # Thread root (Bluesky has both root and parent)
  "labels": ["self-label"],  # Content warnings, etc.
  "langs": ["en"]            # Language tags
}
```

### Phase 2: Bluesky Poller ✅ COMPLETED

**Status**: Done (commit `0f68014`)

Created a poller service to fetch posts from Bluesky's API.

**Files added**:
- `collector/bluesky_poller.py` - Main poller with auth, fetching, normalization
- Updated `collector/requirements.txt` - Added `atproto>=0.0.55`

**Features implemented**:
- Authentication via `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD` env vars
- Polls `app.bsky.feed.getTimeline` API
- Normalizes Bluesky posts to Aerie's tweet schema
- Handles reposts (stores in retweets table)
- Handles quote posts and threads
- Extracts engagement metrics, labels, and language tags
- Stores platform metadata (CID, root_uri, labels, langs)
- Supports continuous polling with `--watch` flag

**Usage**:
```bash
export BLUESKY_HANDLE="yourhandle.bsky.social"
export BLUESKY_APP_PASSWORD="xxxx-xxxx-xxxx-xxxx"
python bluesky_poller.py              # Poll once
python bluesky_poller.py --watch      # Poll continuously
python bluesky_poller.py -v           # Verbose output
```

**Bug fix**: Fixed database.py to move platform index creation after column migration

**Post Normalization Mapping**:

| Aerie Field | Bluesky Source |
|-------------|----------------|
| `id` | `bsky:{post.uri}` |
| `text` | `post.record.text` |
| `created_at` | `post.record.createdAt` |
| `author_id` | `post.author.did` |
| `author_username` | `post.author.handle` |
| `author_display_name` | `post.author.displayName` |
| `author_verified` | Domain verification check |
| `is_repost` | Check if feed item is repost |
| `is_quote` | Check for `embed.record` |
| `quoted_tweet_id` | `embed.record.uri` |
| `reply_to_tweet_id` | `record.reply.parent.uri` |

### Phase 3: Web UI Updates

Update the UI to display platform context and allow filtering.

**Tasks**:
1. Add platform badges to Read and Label pages
2. Add platform filter dropdown (All / Twitter / Bluesky)
3. Update stats display to show per-platform counts
4. Add "Open original" links that work for both platforms
5. Handle platform-specific display (e.g., Bluesky content labels)

### Phase 4: Polish & Settings

Final touches and configuration.

**Tasks**:
1. Settings UI for Bluesky authentication
2. Platform-specific icons/colors
3. Configure polling interval
4. Error handling and retry logic
5. Documentation updates

## Key Differences: Twitter vs Bluesky

| Aspect | Twitter | Bluesky |
|--------|---------|---------|
| **Capture Method** | Browser extension intercepts GraphQL | Direct API polling |
| **Authentication** | Passive (uses logged-in session) | Active (app password/OAuth) |
| **Post IDs** | Numeric snowflake IDs | AT URIs (`at://did/collection/rkey`) |
| **User IDs** | Numeric | DIDs (`did:plc:xxx`) |
| **Handles** | Stable usernames | Mutable (use DID for durability) |
| **Threads** | Simple `reply_to_tweet_id` | Both `reply.root` and `reply.parent` |
| **Reposts** | Wrapped in tweet object | Separate record type |
| **Quotes** | In tweet object | In `embed.record` |
| **Verification** | Blue checkmark | Domain verification |
| **Content Labels** | N/A | Self-labels for content warnings |

## Potential Challenges

1. **Rate Limits**: Bluesky has generous limits, but may need tuning
2. **Historical Data**: Can only poll forward from start; no backfill
3. **Handle Changes**: Bluesky handles can change; always use DID for durable refs
4. **CID Verification**: Could verify content integrity using CIDs (optional)
5. **Session Management**: Need to handle token refresh for long-running poller

## Testing Strategy

1. Unit tests for post normalization
2. Integration tests with mock Bluesky responses
3. Manual testing with real Bluesky account
4. Verify classifier works identically on both platforms

## Files Changed/Added

### Phase 1 (Completed)
- `collector/database.py` - Schema and query updates
- `collector/migrate_add_platform.py` - Migration script
- `collector/test_platform.py` - Platform support tests

### Phase 2 (Planned)
- `collector/bluesky_poller.py` - New file
- `collector/bluesky_auth.py` - Authentication handling (optional)
- `pyproject.toml` - Add `atproto` dependency

### Phase 3 (Planned)
- `collector/templates/read.html` - Platform badges, filtering
- `collector/templates/label.html` - Platform badges
- `collector/server.py` - Platform filter endpoints

### Phase 4 (Planned)
- `collector/templates/settings.html` - Bluesky auth settings (new)
- `CLAUDE.md` - Documentation updates
