# Aerie

A "sheltered" Twitter feed viewer that captures tweets, filters them through an LLM classifier, and presents only the ones worth reading.

## Architecture

```
┌─────────────────────┐     POST /tweets     ┌─────────────────────┐
│  Firefox Extension  │ ──────────────────▶  │  Collector Service  │
│  (webRequest API)   │                      │  (Flask + SQLite)   │
└─────────────────────┘                      └─────────────────────┘
         │                                            │
         │ intercepts Twitter                         │ stores
         │ API responses                              ▼
         │                                   ┌─────────────────────┐
         │                                   │     tweets.db       │
         ▼                                   └─────────────────────┘
┌─────────────────────┐                              │
│   twitter.com/x.com │                              │ reads
│   (unchanged)       │                              ▼
└─────────────────────┘                      ┌─────────────────────┐
                                             │   Classifier (TBD)  │
                                             │   Viewer UI (TBD)   │
                                             └─────────────────────┘
```

## Components

### Browser Extension (`extension/`)

A Firefox extension that intercepts Twitter's API responses using the `webRequest` API. This approach:
- Operates at the browser level, **invisible to Twitter's page JavaScript**
- Captures the full JSON response including tweet IDs, text, threading info, media, etc.
- Passes data through unchanged (read-only wiretap)

### Collector Service (`collector/`)

A local Flask server that:
- Receives captured tweets from the extension
- Deduplicates by tweet ID
- Stores in SQLite for later classification

## Setup

### 1. Start the collector service

```bash
cd collector
pip install -r requirements.txt
python server.py
```

The server runs on `http://localhost:8080`.

### 2. Install the Firefox extension

1. Open Firefox and go to `about:debugging`
2. Click "This Firefox" in the left sidebar
3. Click "Load Temporary Add-on..."
4. Select `extension/manifest.json`

The extension will now capture tweets as you browse Twitter.

### 3. Verify it's working

Browse to twitter.com and load your timeline. Then check:

```bash
# See how many tweets were captured
curl http://localhost:8080/stats

# View pending tweets
curl http://localhost:8080/tweets/pending
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/tweets` | POST | Receive tweets from extension |
| `/stats` | GET | Database statistics |
| `/tweets/pending` | GET | Tweets awaiting classification |
| `/tweets/approved` | GET | Tweets that passed classification |
| `/health` | GET | Health check |

## Database Schema

The SQLite database stores:
- Tweet content (ID, text, created_at)
- Author info (username, display name, verified status)
- Engagement metrics (likes, retweets, replies, quotes)
- Threading relationships (reply-to, quote tweets)
- Media and URL metadata
- Classification results (pending by default)

## Next Steps

- [ ] LLM classifier to filter tweets based on user-defined criteria
- [ ] Web UI to view the filtered feed
- [ ] Background automation to capture tweets without manual browsing
- [ ] Configurable filter prompts
