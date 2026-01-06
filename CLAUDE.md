# Aerie - Development Notes

## Project Overview

Aerie is a "sheltered" Twitter feed viewer that:
1. Captures tweets from Twitter's API as you browse
2. Stores them locally for LLM classification
3. Hides unclassified/filtered tweets, showing only approved ones

The goal is to filter out ragebait, doomposting, and low-quality content before you see it.

## Architecture

```
┌─────────────────────────┐
│  Firefox Extension      │
│  ├── background.js      │ ← Intercepts Twitter API responses, extracts tweets
│  ├── content.js         │ ← Hides/shows tweets based on approval status
│  └── content.css        │ ← Visual treatment for pending/approved/filtered
└───────────┬─────────────┘
            │ POST /tweets (captured)
            │ POST /tweets/check (status lookup)
            ▼
┌─────────────────────────┐
│  Collector Service      │
│  (Flask + SQLite)       │
│  └── tweets.db          │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│  Classifier             │
│  (Claude API)           │
│  └── classifier.py      │
└─────────────────────────┘
```

## Key Technical Decisions

### Tweet Capture: webRequest.filterResponseData

We use Firefox's `webRequest.filterResponseData()` API to intercept Twitter's GraphQL API responses. This approach:
- Operates at the browser level, **invisible to Twitter's page JavaScript**
- Gets structured JSON data (tweet IDs, text, author, threading, etc.)
- Passes data through unchanged (read-only wiretap)

Alternative considered: Content script that patches `fetch`/`XMLHttpRequest`. Rejected because it's detectable by the page.

### Tweet Hiding: CSS opacity

We went through several iterations:

1. **`display: none` / `max-height: 0`** - Broke Twitter's virtualized list. Twitter only keeps visible tweets in the DOM; collapsing height confused its viewport calculations, causing infinite scroll failures and tweets disappearing.

2. **`filter: blur()`** - Worked but was extremely slow. Blur is GPU-intensive, especially with embedded videos. Scrolling became laggy and tweets took seconds to render.

3. **`opacity: 0.02`** (current) - Fast and effective. Tweets are essentially invisible but still "exist" in the layout, so Twitter's virtualization works normally.

### Content Script Timing

The content script uses `requestAnimationFrame` to defer tweet processing. Processing tweets synchronously in the MutationObserver callback can interfere with Twitter's own DOM operations. Deferring lets Twitter's code complete first.

### Cache Pre-loading

On page load, the content script fetches all classified tweet IDs from the backend (`GET /tweets/classified-ids`). This populates the in-memory cache so subsequent status checks are instant (no network round-trip).

## Lessons Learned / Gotchas

### Firefox filterResponseData gives decompressed data
Despite response headers saying `content-encoding: gzip` or `br`, Firefox's `filterResponseData` provides already-decompressed data. We don't need to handle decompression ourselves.

### Twitter's visibility tracking
Twitter tracks which tweets you've "seen" (likely via Intersection Observer). CSS that makes tweets invisible (`opacity: 0`, `display: none`) may confuse this tracking and cause:
- "Welcome to X!" empty feed state
- Infinite scroll stopping
- Feed thinking all tweets are "read"

Using `opacity: 0.02` (nearly invisible but not zero) seems to avoid these issues.

### Twitter's virtualized list
Twitter aggressively virtualizes the timeline - only tweets in/near the viewport exist in the DOM. As you scroll:
- Tweets leaving the viewport are removed from DOM
- Tweets entering the viewport are added fresh

This means:
- Our content script must handle tweets being removed and re-added
- CSS that collapses tweet height breaks viewport calculations
- We maintain a cache so re-added tweets get their status reapplied instantly

### MutationObserver overhead
Watching `document.body` with `subtree: true` catches all DOM changes. This can be expensive if we process synchronously. Using `requestAnimationFrame` to batch and defer processing helps.

### GraphQL endpoint patterns
Twitter's timeline data comes from endpoints like:
- `/graphql/.../HomeTimeline` - "For You" feed
- `/graphql/.../HomeLatestTimeline` - "Following" feed
- `/graphql/.../TweetDetail` - Individual tweet view
- `/graphql/.../UserTweets` - Profile tweets

The hash in the URL (e.g., `/graphql/abc123xyz/HomeTimeline`) changes periodically but the operation name at the end is stable.

## Database Schema

Key fields in the `tweets` table:
- `id` - Tweet ID (primary key)
- `text`, `author_username`, `created_at` - Basic tweet data
- `classification_status` - 'pending' | 'completed'
- `classification_result` - 1 (approved) | 0 (filtered)
- `classification_reason` - LLM's explanation (for debugging)

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/tweets` | POST | Receive tweets from extension |
| `/tweets/check` | POST | Check approval status of tweet IDs |
| `/tweets/classified-ids` | GET | Get all classified IDs (for cache) |
| `/tweets/approve-all` | POST | Approve all pending (for testing) |
| `/tweets/pending` | GET | Get pending tweets (for classifier) |
| `/stats` | GET | Database statistics |

## Running the Project

1. Start collector: `cd collector && python server.py`
2. Load extension: Firefox → `about:debugging` → Load Temporary Add-on → select `extension/manifest.json`
3. Browse twitter.com - tweets are captured automatically
4. Check stats: `curl http://localhost:8080/stats`
5. Run classifier: `cd collector && python classifier.py`

## Classifier Usage

The classifier uses Claude to evaluate each pending tweet against a filter prompt.

```bash
# Set your API key
export ANTHROPIC_API_KEY="your-key-here"

# Classify all pending tweets
python classifier.py

# Classify with verbose output
python classifier.py --verbose

# Dry run (no changes saved)
python classifier.py --dry-run --verbose

# Use custom filter prompt
python classifier.py --prompt-file filter_prompt.txt

# Limit to 20 tweets, use faster/cheaper model
python classifier.py --max 20 --model claude-3-haiku-20240307
```

The default filter prompt approves informative/positive/creative content and filters ragebait, doomposting, engagement farming, etc. Customize by editing `filter_prompt.txt` or creating your own.

## TODO

- [ ] Background/scheduled classification (cron or daemon)
- [ ] Better visual treatment options (configurable)
- [ ] Handle rate limiting gracefully
- [ ] Batch API calls for efficiency (messages batches API)
