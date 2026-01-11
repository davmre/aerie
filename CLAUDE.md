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
│  (Anthropic/Gemini API) │
│  └── classifier.py      │
└─────────────────────────┘
```

## Technical Decisions & Gotchas

### Tweet Capture
We use Firefox's `webRequest.filterResponseData()` to intercept Twitter's GraphQL API responses. This operates at the browser level, invisible to Twitter's page JavaScript. The data arrives already decompressed despite headers saying `gzip`/`br`.

### Tweet Hiding
We use `opacity: 0.02` to hide tweets. This is critical:
- `display: none` / `max-height: 0` breaks Twitter's virtualized list (viewport calculations fail)
- `opacity: 0` triggers Twitter's "Welcome to X!" empty state (visibility tracking)
- `filter: blur()` is GPU-intensive and laggy

Twitter virtualizes the timeline aggressively—tweets are removed/re-added as you scroll. Our content script handles this with a cache and `requestAnimationFrame` for deferred processing.

### GraphQL Endpoints
Twitter's timeline data comes from `/graphql/.../HomeTimeline`, `/graphql/.../HomeLatestTimeline`, etc. The hash in the URL changes but the operation name is stable.

## Web UI

The collector service includes web interfaces:

- `/ui/label` - Labeling interface for creating ground truth data
- `/ui/read` - Clean reading view for approved tweets
- `/ui/modes` - Mode configuration UI for creating/editing/deleting classification modes
- `/ui/prompts` - Prompt management UI for creating/editing classification prompts

Both label and read UIs show full tweet context (quoted tweets, thread ancestors, retweet attribution) and display mode-aware statistics. The Read page groups tweets into "conversation chains" - see `database.py` for the algorithm.

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
- `prefilter` - Optional prefilter function name (short-circuits LLM)
- `extractor` - Extractor function name to extract decision from response
- `extractor_config` - JSON config for parameterized extractors
- `prefilter_config` - JSON config for parameterized prefilters
- `provider` - LLM provider ("anthropic", "gemini")
- `model_name` - Optional model override

**human_labels** - Ground truth for evaluation
- `tweet_id`, `mode_id` - Composite primary key
- `should_show` - 1 = show, 0 = hide
- `notes` - Optional annotation

**mode_decisions** - Cached mode decisions for fast lookups
- `tweet_id`, `mode_id` - Composite primary key
- `decision` - 'approved' or 'filtered'
- `source` - 'prefilter' or 'extractor' (how decision was made)
- `computed_at` - When this decision was computed

This table caches the final show/hide decision for each tweet in each mode. It enables O(1) lookups for stats and fast SQL queries for the Read page, avoiding the need to recompute decisions on every page load.

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
| `/stats` | GET | Database statistics (accepts `?mode=` for mode-specific stats) |
| `/api/ui/tweets` | GET | Get tweets for web UI with full context |
| `/api/ui/chains` | GET | Get conversation chains for Read page (paginated) |
| `/api/ui/tweet/<id>/replies` | GET | Get tweet and its direct replies for modal view |
| `/api/ui/label` | POST | Add human label for a tweet |
| `/api/modes` | GET/POST | List modes or create new mode |
| `/api/modes/<id>` | GET/PUT/DELETE | Get, update, or delete a mode |
| `/api/extractors` | GET | List available extractors with config schemas |
| `/api/prefilters` | GET | List available prefilters with config schemas |
| `/api/prompts` | GET/POST | List prompts or create new prompt |
| `/api/prompts/<id>` | GET/PUT/DELETE | Get, update, or delete a prompt |
| `/api/providers` | GET | List available LLM providers |
| `/ui/label` | GET | Web UI for labeling tweets |
| `/ui/read` | GET | Web UI for reading approved tweets |
| `/ui/modes` | GET | Web UI for managing classification modes |
| `/ui/prompts` | GET | Web UI for managing prompts |

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

## Parallel Development with Worktrees

The repo uses git worktrees to support multiple Claude Code agents working in parallel:

```
/Users/dave/code/
├── aerie/           ← Main clone (live server + extension)
│   └── .venv/       ← Shared virtualenv
├── aerie-dev1/      ← Worktree for parallel session
└── aerie-dev2/      ← Worktree for parallel session
```

**If you're running in a worktree** (path contains `aerie-dev`), use the shared venv:
```bash
source ../aerie/.venv/bin/activate
```

**Workflow:**
- Each agent session works in its own worktree on a feature branch
- Push changes to remote, then merge into main clone for testing
- The main `aerie/` directory runs the live server and has the extension loaded

**Managing worktrees:**
```bash
# Create a new worktree
git worktree add ../aerie-dev3 -b dev/session3

# Remove when done
git worktree remove ../aerie-dev1
git branch -d dev/session1
```

## Testing

```bash
cd collector
pytest tests/ -v
```

Tests use isolated SQLite databases via `tmp_path` fixtures. See `tests/conftest.py` for fixtures (`test_db`, `app`, `client`) and `tests/fixtures.py` for helpers (`make_tweet`, `make_thread`, `approve_tweets`).

The Flask app uses the factory pattern (`create_app(config)`) to support test isolation with custom database paths.

## Type Checking and Linting

```bash
cd collector
pyright          # Type checking
ruff check .     # Linting (use --fix to auto-fix)
ruff format .    # Formatting
```

Configuration is in `pyproject.toml`. Key convention: use keyword arguments for functions with multiple optional parameters to avoid positional argument bugs:
```python
get_thread_context_batch(tweet_ids, db_path=db_path)  # Good
get_thread_context_batch(tweet_ids, db_path)          # Risky
```

## Classifier

The server includes a background worker that classifies tweets automatically. Supports multiple LLM providers:
- **Anthropic** (default): Set `ANTHROPIC_API_KEY`
- **Gemini**: Set `GEMINI_API_KEY` and `pip install google-genai`

Each mode can specify its own provider and model.

```bash
cd collector

# Manual batch classification
python classifier.py                          # Classify pending tweets
python classifier.py classify --verbose       # With detailed output
python classifier.py classify --provider gemini  # Use Gemini instead
python classifier.py recompute-decisions      # Refresh cached mode decisions

# Inspect
python classifier.py prompts                  # List prompts
python classifier.py modes                    # List modes
python classifier.py providers                # List available providers
```

**Built-in prompts:** `binary_filter_v1` (yes/no filter), `topic_tagger_v1` (topics + quality scores)

**Custom modes:** Create via web UI at `/ui/modes` or CLI. Modes combine a prompt with an extractor, optional prefilter, and provider. See `extractors.py`, `prefilters.py`, and `providers.py`.
