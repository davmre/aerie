// Aerie Content Script
// Hides unapproved tweets and reveals them as they get classified

// Default settings
const DEFAULTS = {
  backendUrl: "http://localhost:8080",
  authUsername: "",
  authPassword: "",
  mode: "default",
  pollInterval: 3000,
  pendingOpacity: 0.02,
  filteredOpacity: 0.02,
  collectOnly: false
};

// Current settings (loaded from storage)
let settings = { ...DEFAULTS };

// Build auth headers if credentials are set
function getAuthHeaders() {
  if (settings.authUsername && settings.authPassword) {
    const credentials = btoa(`${settings.authUsername}:${settings.authPassword}`);
    return { 'Authorization': `Basic ${credentials}` };
  }
  return {};
}

// Poll interval handle (for restarting on settings change)
let pollIntervalId = null;

// Track tweets we're monitoring (id -> element)
const pendingTweets = new Map();

// Cache of known statuses to avoid repeated backend calls
const statusCache = new Map(); // id -> 'approved' | 'pending' | 'filtered'

// Apply CSS custom properties for opacity settings
function applyOpacitySettings() {
  document.documentElement.style.setProperty('--aerie-pending-opacity', settings.pendingOpacity);
  document.documentElement.style.setProperty('--aerie-filtered-opacity', settings.filteredOpacity);
}

// Load settings from storage
async function loadSettings() {
  try {
    settings = await browser.storage.local.get(DEFAULTS);
    applyOpacitySettings();
    console.log('[Aerie] Settings loaded:', settings.backendUrl, 'mode:', settings.mode);
  } catch (err) {
    console.warn('[Aerie] Could not load settings, using defaults:', err.message);
  }
}

// Listen for settings changes
browser.storage.onChanged.addListener((changes, area) => {
  if (area === "local") {
    for (const [key, { newValue }] of Object.entries(changes)) {
      if (key in settings) {
        settings[key] = newValue;
      }
    }

    // Apply opacity changes immediately
    applyOpacitySettings();

    // Restart poll interval if it changed
    if (changes.pollInterval && pollIntervalId) {
      clearInterval(pollIntervalId);
      pollIntervalId = setInterval(pollForUpdates, settings.pollInterval);
      console.log('[Aerie] Poll interval updated to', settings.pollInterval, 'ms');
    }

    console.log('[Aerie] Settings updated');
  }
});

// Pre-load cache with all classified tweets on init
async function preloadCache() {
  try {
    const response = await fetch(`${settings.backendUrl}/tweets/classified-ids?mode=${settings.mode}`, {
      headers: getAuthHeaders()
    });
    if (response.ok) {
      const classified = await response.json();
      for (const [id, status] of Object.entries(classified)) {
        statusCache.set(id, status);
      }
      console.log(`[Aerie] Pre-loaded ${statusCache.size} classified tweets into cache`);
    }
  } catch (err) {
    console.warn('[Aerie] Could not pre-load cache:', err.message);
  }
}

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
    const response = await fetch(`${settings.backendUrl}/tweets/check`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...getAuthHeaders()
      },
      body: JSON.stringify({ ids: tweetIds, mode: settings.mode })
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

// Apply status to a tweet element (using both class and data attribute for robustness)
function applyStatus(tweetElement, status) {
  // Set data attribute (survives some DOM manipulations better than classes)
  tweetElement.setAttribute('data-aerie-status', status);

  // Also set class for CSS
  tweetElement.classList.remove('aerie-approved', 'aerie-filtered', 'aerie-pending');
  if (status === 'approved') {
    tweetElement.classList.add('aerie-approved');
  } else if (status === 'filtered') {
    tweetElement.classList.add('aerie-filtered');
  } else {
    tweetElement.classList.add('aerie-pending');
  }
}

// Process a tweet element - check status and show/hide accordingly
async function processTweet(tweetElement) {
  const tweetId = getTweetId(tweetElement);
  if (!tweetId) return;

  // Check if already processed with same ID
  const existingStatus = tweetElement.getAttribute('data-aerie-status');
  const existingId = tweetElement.getAttribute('data-aerie-id');

  if (existingId === tweetId && existingStatus) {
    // Already processed this exact element with this ID
    return;
  }

  // Mark that we're processing this tweet
  tweetElement.setAttribute('data-aerie-id', tweetId);

  // Check cache first
  if (statusCache.has(tweetId)) {
    const status = statusCache.get(tweetId);
    applyStatus(tweetElement, status);
    if (status === 'pending' || status === 'unknown') {
      pendingTweets.set(tweetId, tweetElement);
    }
    return;
  }

  // Track this tweet for polling
  pendingTweets.set(tweetId, tweetElement);

  // Check with backend
  const statuses = await checkTweetStatuses([tweetId]);

  if (statuses[tweetId]) {
    statusCache.set(tweetId, statuses[tweetId]);
    applyStatus(tweetElement, statuses[tweetId]);

    if (statuses[tweetId] !== 'pending' && statuses[tweetId] !== 'unknown') {
      pendingTweets.delete(tweetId);
    }
  }
}

// Batch process multiple tweets
async function processTweets(tweetElements) {
  const tweetsToCheck = [];

  for (const element of tweetElements) {
    const tweetId = getTweetId(element);
    if (!tweetId) continue;

    // Check if already processed
    const existingId = element.getAttribute('data-aerie-id');
    const existingStatus = element.getAttribute('data-aerie-status');
    if (existingId === tweetId && existingStatus) continue;

    element.setAttribute('data-aerie-id', tweetId);

    // Check cache first
    if (statusCache.has(tweetId)) {
      const status = statusCache.get(tweetId);
      applyStatus(element, status);
      if (status === 'pending' || status === 'unknown') {
        pendingTweets.set(tweetId, element);
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
      applyStatus(element, statuses[id]);

      if (statuses[id] !== 'pending' && statuses[id] !== 'unknown') {
        pendingTweets.delete(id);
      }
    }
  }
}

// Poll for updates on pending tweets
async function pollForUpdates() {
  // Also rescan visible tweets to catch any that lost their status
  const allTweets = findTweetElements();
  for (const tweet of allTweets) {
    const tweetId = tweet.getAttribute('data-aerie-id') || getTweetId(tweet);
    if (!tweetId) continue;

    // If this tweet has a cached status but lost its visual state, reapply
    if (statusCache.has(tweetId)) {
      const cachedStatus = statusCache.get(tweetId);
      const currentStatus = tweet.getAttribute('data-aerie-status');
      if (currentStatus !== cachedStatus) {
        applyStatus(tweet, cachedStatus);
      }
    } else if (!tweet.hasAttribute('data-aerie-status')) {
      // New tweet we haven't seen, process it
      pendingTweets.set(tweetId, tweet);
      tweet.setAttribute('data-aerie-id', tweetId);
    }
  }

  if (pendingTweets.size === 0) return;

  const ids = Array.from(pendingTweets.keys());
  const statuses = await checkTweetStatuses(ids);

  for (const [id, element] of pendingTweets) {
    if (statuses[id]) {
      statusCache.set(id, statuses[id]);
      applyStatus(element, statuses[id]);

      if (statuses[id] !== 'pending' && statuses[id] !== 'unknown') {
        pendingTweets.delete(id);
        console.log(`[Aerie] Tweet ${id}: ${statuses[id]}`);
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
  let pendingNewTweets = [];
  let processScheduled = false;

  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeType !== Node.ELEMENT_NODE) continue;

        // Check if this node is a tweet
        if (node.matches?.('article[data-testid="tweet"]')) {
          pendingNewTweets.push(node);
        }

        // Check for tweets inside this node
        const tweets = node.querySelectorAll?.('article[data-testid="tweet"]');
        if (tweets) {
          pendingNewTweets.push(...tweets);
        }
      }
    }

    // Defer processing to avoid interfering with Twitter's DOM operations
    if (pendingNewTweets.length > 0 && !processScheduled) {
      processScheduled = true;
      requestAnimationFrame(() => {
        const tweets = pendingNewTweets;
        pendingNewTweets = [];
        processScheduled = false;
        processTweets(tweets);
      });
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

  // Load settings first
  await loadSettings();

  // In collect-only mode, skip all hide/show logic
  // Tweets are still collected by background.js, just not filtered in the browser
  if (settings.collectOnly) {
    console.log('[Aerie] Collect-only mode enabled - tweets will not be hidden');
    return;
  }

  // Enable CSS rules for hiding/showing tweets
  document.documentElement.classList.add('aerie-active');

  // Pre-load cache with classified tweets (reduces network requests)
  await preloadCache();

  // Process any tweets already in the DOM
  const existingTweets = findTweetElements();
  if (existingTweets.length > 0) {
    console.log(`[Aerie] Processing ${existingTweets.length} existing tweets`);
    await processTweets(Array.from(existingTweets));
  }

  // Watch for new tweets
  setupMutationObserver();

  // Poll for updates on pending tweets
  pollIntervalId = setInterval(pollForUpdates, settings.pollInterval);
}

// Run when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
