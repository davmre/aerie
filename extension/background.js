// Aerie Tweet Collector - Background Script
// Intercepts Twitter API responses using webRequest API (invisible to page JavaScript)

// Default settings
const DEFAULTS = {
  backendUrl: "http://localhost:8080",
  mode: "default",
  pollInterval: 3000,
  pendingOpacity: 0.02,
  filteredOpacity: 0.02
};

// Current settings (loaded from storage)
let settings = { ...DEFAULTS };

// Load settings on startup
async function loadSettings() {
  settings = await browser.storage.local.get(DEFAULTS);
  console.log("[Aerie] Settings loaded:", settings.backendUrl);
}

// Listen for settings changes
browser.storage.onChanged.addListener((changes, area) => {
  if (area === "local") {
    for (const [key, { newValue }] of Object.entries(changes)) {
      if (key in settings) {
        settings[key] = newValue;
      }
    }
    console.log("[Aerie] Settings updated:", settings.backendUrl);
  }
});

// Initialize settings
loadSettings();

// Twitter API endpoints that contain timeline/tweet data
const TIMELINE_PATTERNS = [
  /\/graphql\/[^/]+\/Home(Timeline|LatestTimeline)/,
  /\/graphql\/[^/]+\/UserTweets/,
  /\/graphql\/[^/]+\/TweetDetail/,
];

function isTimelineEndpoint(url) {
  return TIMELINE_PATTERNS.some(pattern => pattern.test(url));
}

browser.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (!isTimelineEndpoint(details.url)) {
      return;
    }

    // Use filterResponseData to read the response body
    // Note: Firefox gives us already-decompressed data
    const filter = browser.webRequest.filterResponseData(details.requestId);
    const chunks = [];

    filter.ondata = (event) => {
      chunks.push(new Uint8Array(event.data));
      filter.write(event.data); // Pass through unchanged
    };

    filter.onstop = async () => {
      filter.close();

      // Combine chunks
      const totalLength = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
      const combined = new Uint8Array(totalLength);
      let offset = 0;
      for (const chunk of chunks) {
        combined.set(chunk, offset);
        offset += chunk.length;
      }

      try {
        const text = new TextDecoder().decode(combined);
        const data = JSON.parse(text);
        const { tweets, retweets } = extractTweetsAndRetweets(data);

        if (tweets.length > 0 || retweets.length > 0) {
          console.log(`[Aerie] Captured ${tweets.length} tweets, ${retweets.length} retweets`);
          sendToCollector(tweets, retweets);
        }
      } catch (err) {
        console.error("[Aerie] Error processing response:", err);
      }
    };

    filter.onerror = () => {
      console.error(`[Aerie] Filter error: ${filter.error}`);
    };
  },
  { urls: ["*://*.twitter.com/*", "*://*.x.com/*"] },
  ["blocking", "responseHeaders"]
);

// Extract tweet objects from Twitter's nested API response
// Returns {tweets: [], retweets: []} where retweets link retweeters to original tweets
function extractTweetsAndRetweets(data) {
  const tweets = [];
  const retweets = [];
  const seenTweets = new Set();
  const seenRetweets = new Set(); // "originalId:retweeterUsername"

  function processTweet(tweetObj, contextInfo) {
    const legacy = tweetObj.legacy || tweetObj;

    // Check if this is a retweet
    const retweetedResult = legacy.retweeted_status_result?.result;
    if (retweetedResult) {
      // This is a retweet - extract the original tweet and create a retweet record

      // Get retweeter info from the current tweet
      const retweeterResult = tweetObj.core?.user_results?.result || tweetObj.user_results?.result || {};
      const retweeterCore = retweeterResult.core || {};
      const retweeterLegacy = retweeterResult.legacy || {};

      const retweeterUsername = retweeterCore.screen_name || retweeterLegacy.screen_name;
      const retweeterDisplayName = retweeterCore.name || retweeterLegacy.name;
      const retweeterUserId = retweeterResult.rest_id || retweeterLegacy.id_str;

      // Normalize and store the original tweet
      const originalTweet = normalizeTweet(retweetedResult, { ...contextInfo, isPromoted: false });
      if (originalTweet && !seenTweets.has(originalTweet.id)) {
        seenTweets.add(originalTweet.id);
        tweets.push(originalTweet);
      }

      // Create retweet record if we have the original and retweeter info
      if (originalTweet && retweeterUsername) {
        const rtKey = `${originalTweet.id}:${retweeterUsername}`;
        if (!seenRetweets.has(rtKey)) {
          seenRetweets.add(rtKey);
          retweets.push({
            original_tweet_id: originalTweet.id,
            retweeter_user_id: retweeterUserId,
            retweeter_username: retweeterUsername,
            retweeter_display_name: retweeterDisplayName,
            retweeted_at: legacy.created_at || null,
            captured_at: new Date().toISOString(),
          });
        }
      }

      // Don't add the "RT @..." wrapper tweet - we only want the original
      return;
    }

    // Not a retweet - process normally
    const tweet = normalizeTweet(tweetObj, contextInfo);
    if (tweet && !seenTweets.has(tweet.id)) {
      seenTweets.add(tweet.id);
      tweets.push(tweet);
    }
  }

  function traverse(obj, parent = null, grandparent = null) {
    if (!obj || typeof obj !== "object") return;

    // Check for promoted content indicators at the entry/item level
    const isPromoted = !!(
      obj.promotedMetadata ||
      obj.advertiser_results ||
      parent?.promotedMetadata ||
      parent?.advertiser_results ||
      obj.entryId?.includes("promoted") ||
      obj.entryId?.includes("cursor-ad") ||
      parent?.entryId?.includes("promoted")
    );

    const contextInfo = {
      isPromoted,
      entryId: obj.entryId || parent?.entryId,
      entryType: obj.__typename || obj.entryType,
    };

    // Look for tweet_results.result pattern (most reliable)
    if (obj.tweet_results?.result) {
      const tweetObj = obj.tweet_results.result;
      if (tweetObj.__typename === "Tweet" || tweetObj.legacy?.full_text !== undefined) {
        processTweet(tweetObj, contextInfo);
      }
    }

    // Also look for direct Tweet objects (fallback)
    if (obj.__typename === "Tweet" && obj.legacy?.full_text !== undefined) {
      if (obj.core?.user_results || obj.user_results) {
        processTweet(obj, contextInfo);
      }
    }

    // Recurse into arrays and objects
    if (Array.isArray(obj)) {
      for (const item of obj) {
        traverse(item, obj, parent);
      }
    } else {
      for (const value of Object.values(obj)) {
        traverse(value, obj, parent);
      }
    }
  }

  traverse(data);
  return { tweets, retweets };
}

// Normalize a tweet object into our standard schema
function normalizeTweet(raw, contextInfo = {}) {
  try {
    // Handle both direct tweet objects and wrapped ones
    const legacy = raw.legacy || raw;

    // Extract full text - prefer note_tweet for long-form content
    const noteTweetText = raw.note_tweet?.note_tweet_results?.result?.text;
    const fullText = noteTweetText || legacy.full_text || legacy.text || "";

    // Check if this is a promoted/ad tweet
    const isPromoted = contextInfo.isPromoted || false;

    // Twitter has multiple paths to user data - try them all
    const userResult =
      raw.core?.user_results?.result ||  // Most common path
      raw.user_results?.result ||         // Alternative path
      raw.author?.result ||               // Another alternative
      {};

    // Twitter now nests screen_name/name in userResult.core (not userResult.legacy)
    const userCore = userResult.core || {};
    const userLegacy = userResult.legacy || {};

    // Sometimes user is directly on legacy
    const legacyUser = legacy.user || {};

    // Extract tweet ID - could be in various places
    const id = raw.rest_id || legacy.id_str || legacy.id;
    if (!id) return null;

    // Try multiple sources for author info - userCore is the new primary location
    const authorUsername = userCore.screen_name || userLegacy.screen_name || legacyUser.screen_name || legacy.user_screen_name || null;
    const authorDisplayName = userCore.name || userLegacy.name || legacyUser.name || legacy.user_name || null;
    const authorId = userResult.rest_id || userLegacy.id_str || legacyUser.id_str || legacy.user_id_str || null;
    const authorVerified = userLegacy.verified || legacyUser.verified || false;

    // Additional author info useful for classification
    const authorBio = userResult.profile_bio?.description || userLegacy.description || null;
    const authorFollowing = userResult.relationship_perspectives?.following ?? null;
    const authorBlueVerified = userResult.is_blue_verified ?? null;
    const authorFollowersCount = userLegacy.followers_count ?? null;

    return {
      id: String(id),
      text: fullText,
      created_at: legacy.created_at || null,
      author: {
        id: authorId,
        username: authorUsername,
        display_name: authorDisplayName,
        verified: authorVerified,
        blue_verified: authorBlueVerified,
        bio: authorBio,
        following: authorFollowing,
        followers_count: authorFollowersCount,
      },
      metrics: {
        retweet_count: legacy.retweet_count || 0,
        reply_count: legacy.reply_count || 0,
        like_count: legacy.favorite_count || 0,
        quote_count: legacy.quote_count || 0,
      },
      reply_to: {
        tweet_id: legacy.in_reply_to_status_id_str || null,
        user_id: legacy.in_reply_to_user_id_str || null,
        username: legacy.in_reply_to_screen_name || null,
      },
      is_retweet: !!legacy.retweeted_status_result,
      is_quote: !!raw.quoted_status_result,
      is_promoted: isPromoted,
      quoted_tweet_id: raw.quoted_status_result?.result?.rest_id ||
                       legacy.quoted_status_id_str || null,
      media: (() => {
        const media = extractMedia(legacy.extended_entities || legacy.entities);
        const card = extractCard(raw);
        if (card) {
          // Resolve t.co URL to expanded URL using entities
          const urlEntity = legacy.entities?.urls?.find(u => u.url === card.uri);
          if (urlEntity?.expanded_url) {
            card.uri = urlEntity.expanded_url;
          }
          media.push(card);
        }
        return media;
      })(),
      urls: extractUrls(legacy.entities),
      hashtags: (legacy.entities?.hashtags || []).map(h => h.text),
      mentions: (legacy.entities?.user_mentions || []).map(m => ({
        id: m.id_str,
        username: m.screen_name,
      })),
      captured_at: new Date().toISOString(),
    };
  } catch (err) {
    console.error("[Aerie] Error normalizing tweet:", err);
    return null;
  }
}

function extractMedia(entities) {
  if (!entities?.media) return [];
  return entities.media.map(m => ({
    type: m.type,
    url: m.media_url_https || m.media_url,
    expanded_url: m.expanded_url,
  }));
}

function extractUrls(entities) {
  if (!entities?.urls) return [];
  return entities.urls.map(u => ({
    url: u.url,
    expanded_url: u.expanded_url,
    display_url: u.display_url,
  }));
}

// Extract link preview card data from Twitter's card object
function extractCard(raw) {
  const card = raw.card?.legacy;
  if (!card?.binding_values) return null;

  // Convert binding_values array to a lookup object
  const bindings = {};
  for (const item of card.binding_values) {
    if (item.key && item.value) {
      bindings[item.key] = item.value;
    }
  }

  // Get string value helper
  const getString = (key) => bindings[key]?.string_value || "";

  // Get image URL helper - try multiple possible keys in preference order
  const getImageUrl = (...keys) => {
    for (const key of keys) {
      const url = bindings[key]?.image_value?.url;
      if (url) return url;
    }
    return "";
  };

  // Extract title - try multiple possible keys
  const title = getString("title") || getString("og:title") || "";

  // Extract description
  const description = getString("description") || "";

  // Extract domain
  const domain = getString("domain") || "";

  // Extract thumbnail - prefer larger images, fall back to smaller
  const thumb = getImageUrl(
    "photo_image_full_size_large",
    "thumbnail_image_large",
    "photo_image_full_size_original",
    "summary_photo_image_original",
    "player_image_large",
    "thumbnail_image",
    "player_image"
  );

  // Only return card if we have meaningful content
  if (!title && !description && !thumb) return null;

  // Find the expanded URL for this card from entities
  // card.url is the t.co short URL, we want the real URL
  const cardUrl = card.url || "";

  return {
    type: "link",
    uri: cardUrl,  // Will be resolved to expanded_url in normalizeTweet
    title: title,
    description: description,
    domain: domain,
    thumb: thumb,
  };
}

// Send extracted tweets and retweets to local collector service
async function sendToCollector(tweets, retweets = []) {
  try {
    const collectorUrl = `${settings.backendUrl}/tweets`;
    const response = await fetch(collectorUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ tweets, retweets }),
    });

    if (!response.ok) {
      console.error(`[Aerie] Collector returned ${response.status}`);
    } else {
      const result = await response.json();
      const rtInfo = result.retweets_inserted !== undefined
        ? `, ${result.retweets_inserted} retweets`
        : '';
      console.log(`[Aerie] Stored: ${result.inserted} new, ${result.duplicates} duplicates${rtInfo}`);

      // Immediately check the captured tweets to trigger prefilter evaluation
      // This ensures prefilter-only modes (like "all tweets") get instant decisions
      if (tweets.length > 0) {
        const tweetIds = tweets.map(t => t.id);
        checkTweetsForPrefilter(tweetIds);
      }
    }
  } catch (err) {
    // Collector might not be running - that's okay, log and continue
    console.warn(`[Aerie] Could not reach collector: ${err.message}`);
  }
}

// Check tweets to trigger prefilter evaluation (non-blocking)
async function checkTweetsForPrefilter(tweetIds) {
  try {
    const checkUrl = `${settings.backendUrl}/tweets/check`;
    const response = await fetch(checkUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ids: tweetIds, mode: settings.mode }),
    });

    if (response.ok) {
      const statuses = await response.json();
      const approved = Object.values(statuses).filter(s => s === "approved").length;
      const filtered = Object.values(statuses).filter(s => s === "filtered").length;
      const pending = Object.values(statuses).filter(s => s === "pending").length;
      console.log(`[Aerie] Prefilter check: ${approved} approved, ${filtered} filtered, ${pending} pending`);
    }
  } catch (err) {
    // Non-critical, just log
    console.warn(`[Aerie] Prefilter check failed: ${err.message}`);
  }
}

console.log("[Aerie] Tweet collector extension loaded");
