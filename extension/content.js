// Aerie Content Script
// Hides unapproved tweets and reveals them as they get classified

const BACKEND_URL = "http://localhost:8080";
const POLL_INTERVAL_MS = 5000; // Check for newly approved tweets every 5 seconds

// Track tweets we're monitoring (id -> element)
const pendingTweets = new Map();

// Cache of known statuses to avoid repeated backend calls
const statusCache = new Map(); // id -> 'approved' | 'pending' | 'filtered'

// Extract tweet ID from a tweet element
function getTweetId(tweetElement) {
  // Twitter includes links like /username/status/1234567890
  // Look for the tweet permalink
  const links = tweetElement.querySelectorAll('a[href*="/status/"]');
  for (const link of links) {
    const match = link.href.match(/\/status\/(\d+)/);
    if (match) {
      return match[1];
    }
  }
  return null;
}

// Check tweet statuses with backend
async function checkTweetStatuses(tweetIds) {
  if (tweetIds.length === 0) return {};

  try {
    const response = await fetch(`${BACKEND_URL}/tweets/check`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: tweetIds })
    });

    if (!response.ok) {
      console.error('[Aerie] Backend returned', response.status);
      return {};
    }

    return await response.json();
  } catch (err) {
    console.warn('[Aerie] Could not reach backend:', err.message);
    return {};
  }
}

// Process a tweet element - check status and show/hide accordingly
async function processTweet(tweetElement) {
  const tweetId = getTweetId(tweetElement);
  if (!tweetId) return;

  // Check cache first
  if (statusCache.has(tweetId)) {
    const status = statusCache.get(tweetId);
    if (status === 'approved') {
      tweetElement.classList.add('aerie-approved');
    } else if (status === 'filtered') {
      tweetElement.classList.add('aerie-filtered');
    }
    // 'pending' stays hidden
    return;
  }

  // Track this tweet for polling
  pendingTweets.set(tweetId, tweetElement);

  // Check with backend
  const statuses = await checkTweetStatuses([tweetId]);

  if (statuses[tweetId]) {
    statusCache.set(tweetId, statuses[tweetId]);
    pendingTweets.delete(tweetId);

    if (statuses[tweetId] === 'approved') {
      tweetElement.classList.add('aerie-approved');
    } else if (statuses[tweetId] === 'filtered') {
      tweetElement.classList.add('aerie-filtered');
    }
  }
  // If not in backend yet, it stays hidden and we'll poll for it
}

// Batch process multiple tweets
async function processTweets(tweetElements) {
  const tweetsToCheck = [];

  for (const element of tweetElements) {
    const tweetId = getTweetId(element);
    if (!tweetId) continue;

    // Check cache first
    if (statusCache.has(tweetId)) {
      const status = statusCache.get(tweetId);
      if (status === 'approved') {
        element.classList.add('aerie-approved');
      } else if (status === 'filtered') {
        element.classList.add('aerie-filtered');
      }
      continue;
    }

    // Track for checking
    pendingTweets.set(tweetId, element);
    tweetsToCheck.push({ id: tweetId, element });
  }

  if (tweetsToCheck.length === 0) return;

  // Batch check with backend
  const ids = tweetsToCheck.map(t => t.id);
  const statuses = await checkTweetStatuses(ids);

  for (const { id, element } of tweetsToCheck) {
    if (statuses[id]) {
      statusCache.set(id, statuses[id]);
      pendingTweets.delete(id);

      if (statuses[id] === 'approved') {
        element.classList.add('aerie-approved');
      } else if (statuses[id] === 'filtered') {
        element.classList.add('aerie-filtered');
      }
    }
    // If not in backend yet, stays hidden and pending
  }
}

// Poll for updates on pending tweets
async function pollForUpdates() {
  if (pendingTweets.size === 0) return;

  const ids = Array.from(pendingTweets.keys());
  const statuses = await checkTweetStatuses(ids);

  for (const [id, element] of pendingTweets) {
    if (statuses[id] && statuses[id] !== 'pending') {
      statusCache.set(id, statuses[id]);
      pendingTweets.delete(id);

      if (statuses[id] === 'approved') {
        element.classList.add('aerie-approved');
        console.log(`[Aerie] Revealed tweet ${id}`);
      } else if (statuses[id] === 'filtered') {
        element.classList.add('aerie-filtered');
        console.log(`[Aerie] Filtered tweet ${id}`);
      }
    }
  }
}

// Find all tweet elements currently in the DOM
function findTweetElements() {
  // Twitter uses data-testid="tweet" for tweet articles
  return document.querySelectorAll('article[data-testid="tweet"]');
}

// Watch for new tweets being added to the DOM
function setupMutationObserver() {
  const observer = new MutationObserver((mutations) => {
    const newTweets = [];

    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeType !== Node.ELEMENT_NODE) continue;

        // Check if this node is a tweet
        if (node.matches?.('article[data-testid="tweet"]')) {
          newTweets.push(node);
        }

        // Check for tweets inside this node
        const tweets = node.querySelectorAll?.('article[data-testid="tweet"]');
        if (tweets) {
          newTweets.push(...tweets);
        }
      }
    }

    if (newTweets.length > 0) {
      processTweets(newTweets);
    }
  });

  observer.observe(document.body, {
    childList: true,
    subtree: true
  });

  return observer;
}

// Initialize
async function init() {
  console.log('[Aerie] Content script loaded');

  // Process any tweets already in the DOM
  const existingTweets = findTweetElements();
  if (existingTweets.length > 0) {
    console.log(`[Aerie] Processing ${existingTweets.length} existing tweets`);
    await processTweets(Array.from(existingTweets));
  }

  // Watch for new tweets
  setupMutationObserver();

  // Poll for updates on pending tweets
  setInterval(pollForUpdates, POLL_INTERVAL_MS);
}

// Run when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
