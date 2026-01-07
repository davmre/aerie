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

## Web UI

The collector service includes web interfaces:

- `/ui/label` - Labeling interface for creating ground truth data
- `/ui/read` - Clean reading view for approved tweets

Both UIs show full tweet context (quoted tweets, thread ancestors, retweet attribution).

## Design Principle: Context Symmetry

**Human labelers and LLMs should see the same information when classifying tweets.**

A tweet viewed in isolation may be ambiguous or misleading. Both humans and LLMs need context to make good decisions:

| Context Type | What It Provides |
|--------------|------------------|
| **Quote tweets** | The original being commented on - essential for understanding the commentary |
| **Thread ancestors** | Earlier posts in a thread - needed to understand later posts |
| **Retweet attribution** | Who amplified this content - relevant for "who do I follow" decisions |

If a human needs to click through to understand a tweet, an LLM would benefit from the same context. This principle guided our data model: we capture and display full context, not just individual tweets.

## Database Schema

### Core Tables

**tweets** - Raw tweet data captured from Twitter
- `id` - Tweet ID (primary key)
- `text` - Full tweet text (uses note_tweet for long-form content)
- `author_username`, `author_display_name`, `author_id`
- `author_bio`, `author_following`, `author_blue_verified`, `author_followers_count`
- `is_retweet`, `is_quote`, `is_promoted` - Tweet type flags
- `quoted_tweet_id` - ID of quoted tweet (if quote tweet)
- `reply_to_tweet_id`, `reply_to_username` - Thread/reply info
- ~~`classification_status`, `classification_result`~~ - DEPRECATED, use prompt_responses

**retweets** - Tracks who retweeted what (avoids duplicate tweet storage)
- `original_tweet_id` - The original tweet being retweeted
- `retweeter_username`, `retweeter_display_name`, `retweeter_user_id`
- `retweeted_at`, `captured_at`

**prompts** - Versioned prompt definitions
- `id` - Prompt ID (e.g., "binary_filter_v1", "topic_tagger_v1")
- `prompt_text` - The system prompt sent to the LLM
- `response_schema` - Expected JSON response format

**prompt_responses** - Cached LLM responses
- `tweet_id`, `prompt_id`, `model` - Composite primary key
- `response_json` - Raw JSON response from LLM

**modes** - Viewing mode definitions
- `id` - Mode ID (e.g., "default", "ml_focus", "dharma")
- `prompt_id` - Which prompt to use
- `prefilter` - Optional prefilter function (short-circuits LLM)
- `extractor` - Function to extract decision from response

**human_labels** - Ground truth for evaluation
- `tweet_id`, `mode_id` - Composite primary key
- `should_show` - 1 = show, 0 = hide
- `notes` - Optional annotation

### Retweet Handling

When user A retweets user B's tweet:
- **Old behavior**: Stored twice (A's "RT @B: ..." wrapper AND B's original)
- **New behavior**: Store B's original once, create retweet record linking A → B

This ensures:
- Each tweet is rated once (rating applies to all retweets)
- No duplicate entries in labeling UI
- Retweeter attribution preserved for display

### Multi-Mode Architecture

The system supports multiple viewing modes:

1. **Prompts** return arbitrary JSON (topics, scores, binary decisions, etc.)
2. **Modes** combine a prompt with an extractor function
3. **Prefilters** can short-circuit LLM calls (e.g., author whitelist)
4. **Extractors** interpret prompt responses into show/hide decisions

```
Tweet → Prefilter → (short-circuit?) → LLM → Response → Extractor → Decision
           │                                                          │
           └────────────────────────────────────────────────────────────┘
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/tweets` | POST | Receive tweets from extension |
| `/tweets/check` | POST | Check approval status of tweet IDs (mode defaults to "default") |
| `/tweets/classified-ids` | GET | Get all classified IDs (mode defaults to "default") |
| `/modes` | GET | List available filtering modes |
| `/stats` | GET | Database statistics |
| `/api/ui/tweets` | GET | Get tweets for web UI with full context |
| `/api/ui/label` | POST | Add human label for a tweet |
| `/ui/label` | GET | Web UI for labeling tweets |
| `/ui/read` | GET | Web UI for reading approved tweets |

## Running the Project

```bash
# Activate the virtual environment first
source .venv/bin/activate
```

1. Start collector: `cd collector && python server.py`
2. Load extension: Firefox → `about:debugging` → Load Temporary Add-on → select `extension/manifest.json`
3. Browse twitter.com - tweets are captured automatically
4. Check stats: `curl http://localhost:8080/stats`
5. Run classifier: `cd collector && python classifier.py`

## Classifier Usage

The classifier uses Claude to evaluate tweets against prompts stored in the database.

```bash
# Set your API key
export ANTHROPIC_API_KEY="your-key-here"

# Classify with default prompt (binary_filter_v1)
python classifier.py

# Classify with a specific prompt
python classifier.py classify topic_tagger_v1

# Classify with verbose output
python classifier.py classify --verbose

# Dry run (no changes saved)
python classifier.py classify --dry-run --verbose

# Limit to 20 tweets, use faster/cheaper model
python classifier.py classify --max 20 --model claude-3-haiku-20240307

# List available prompts
python classifier.py prompts

# List available modes
python classifier.py modes

# Create a custom mode
python classifier.py create-mode ml_focus \
    --prompt topic_tagger_v1 \
    --extractor topic_ml \
    --name "ML Focus" \
    --description "Show only ML/AI content"
```

### Built-in Prompts

- **binary_filter_v1**: Simple yes/no filter for quality content
- **topic_tagger_v1**: Extracts topics and quality scores (toxicity, informativeness, etc.)

### Creating Custom Modes

Modes combine a prompt with extractor and optional prefilter functions:

```python
# extractors.py - add custom extractors
def my_custom_extractor(response: dict) -> bool:
    return "ml" in response.get("topics", []) and response.get("scores", {}).get("toxicity", 1) < 0.2

# prefilters.py - add custom prefilters
def my_whitelist(tweet: dict) -> bool | None:
    if tweet.get("author_username") in {"favorite_author"}:
        return True
    return None  # Let LLM decide
```

## TODO

- [ ] Background/scheduled classification (cron or daemon)
- [ ] Extension support for mode switching
- [ ] Evaluation metrics for human labels vs model predictions
- [ ] Batch API calls for efficiency (messages batches API)
