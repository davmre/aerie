# Aerie Headless Twitter Collector

A standalone headless Twitter collector using [Camoufox](https://github.com/daijro/camoufox) (Firefox-based anti-detect browser). This collector can run on a separate machine from the main Aerie server and posts captured tweets to the main collector's `/tweets` endpoint.

## Architecture

```
┌─────────────────────────────────────────────┐
│  Headless Collector (this package)          │
│  └── twitter-headless/                      │
│       ├── server.py    (HTTP API)           │
│       ├── collector.py (Browser automation) │
│       └── tweet_parser.py (GraphQL parsing) │
└───────────────────┬─────────────────────────┘
                    │ POST /tweets
                    ▼
┌─────────────────────────────────────────────┐
│  Main Aerie Collector (Flask server)        │
│  └── Receives tweets via existing /tweets   │
│       endpoint (same as browser extension)  │
└─────────────────────────────────────────────┘
```

## Requirements

- **Linux** (x86_64 or arm64) - Camoufox doesn't support macOS
- Python 3.11+
- Virtual display (Xvfb) for truly headless operation

## Installation

### 1. Create virtual environment

```bash
cd twitter-headless
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Install Camoufox browser

```bash
# Download and install the Camoufox browser
camoufox fetch
```

### 3. Install virtual display (if running headless without desktop)

```bash
# Debian/Ubuntu
sudo apt-get install xvfb

# You can then run commands with xvfb-run:
xvfb-run python collector.py
```

## Configuration

### 1. Copy and edit config file

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
    "collector_url": "http://your-aerie-server:8080",
    "cookies_file": "cookies.json",
    "scroll_duration": 60,
    "server_port": 8081,
    "auth": {
        "username": "",
        "password": ""
    }
}
```

- `collector_url`: URL of your main Aerie collector
- `cookies_file`: Path to exported Twitter cookies
- `scroll_duration`: How long to scroll the timeline (seconds)
- `server_port`: Port for the HTTP API server
- `auth`: Basic auth credentials (if your Aerie server requires auth)

### 2. Export Twitter cookies

1. Install a cookie export extension in your browser:
   - Firefox: [Cookie Editor](https://addons.mozilla.org/en-US/firefox/addon/cookie-editor/)
   - Chrome: [Cookie Editor](https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)

2. Log in to Twitter/X in your browser

3. Click the cookie extension and export cookies as JSON

4. Save the exported cookies to `cookies.json` in this directory

The cookies file should contain at least:
- `auth_token`
- `ct0`

Example format:
```json
[
    {
        "name": "auth_token",
        "value": "your-auth-token",
        "domain": ".twitter.com",
        "path": "/",
        "secure": true,
        "httpOnly": true
    },
    {
        "name": "ct0",
        "value": "your-ct0-token",
        "domain": ".twitter.com",
        "path": "/",
        "secure": true
    }
]
```

## Usage

### Command Line

```bash
# Activate venv
source .venv/bin/activate

# Run collection once (headless)
python collector.py

# Run with custom duration (2 minutes)
python collector.py --duration 120

# Run with visible browser (for debugging)
python collector.py --headful

# If running without a display
xvfb-run python collector.py
```

### HTTP Server

The server allows you to trigger collection remotely (e.g., from Aerie's web UI):

```bash
# Start the server
python server.py

# Or with xvfb
xvfb-run python server.py
```

API Endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/collect` | POST | Trigger collection (runs in background) |
| `/status` | GET | Get current collection status |

Example:

```bash
# Trigger collection
curl -X POST http://localhost:8081/collect

# Check status
curl http://localhost:8081/status
```

### Integration with Aerie UI

If you configure the headless collector URL in Aerie's Settings page, you can trigger collection directly from the Read page using the "Collect Tweets" button.

## Troubleshooting

### "No display" errors

Install and use xvfb:
```bash
sudo apt-get install xvfb
xvfb-run python collector.py
```

### Cookies expired

Twitter cookies expire periodically. If collection fails with authentication errors:
1. Log into Twitter in your browser
2. Re-export cookies
3. Save to `cookies.json`

### No tweets captured

1. Verify your cookies are valid by running with `--headful`
2. Check if Twitter is blocking the browser
3. Try increasing `--duration` for more scroll time

### Connection refused to collector

Ensure your main Aerie collector is running and the `collector_url` in `config.json` is correct. If using basic auth, set the credentials in the `auth` section.

## How It Works

1. **Browser Launch**: Camoufox launches a Firefox-based browser with anti-fingerprinting features
2. **Cookie Injection**: Your exported Twitter cookies are loaded to authenticate the session
3. **Timeline Navigation**: Browser navigates to twitter.com/home
4. **GraphQL Interception**: Playwright's `page.route()` intercepts Twitter's API responses
5. **Tweet Extraction**: The `tweet_parser` module extracts tweets from the GraphQL responses
6. **Human-like Scrolling**: Random scroll distances and pauses simulate human behavior
7. **Data Submission**: Captured tweets are POSTed to the main Aerie collector

## Security Notes

- Keep your `cookies.json` file secure - it contains your Twitter session
- Don't commit `cookies.json` or `config.json` to version control
- Consider using a secondary Twitter account for automated collection
- Run collection infrequently to avoid detection
