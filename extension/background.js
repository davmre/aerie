// Aerie Tweet Collector - Background Script
// Intercepts Twitter API responses using webRequest API (invisible to page JavaScript)

const COLLECTOR_URL = "http://localhost:8080/tweets";

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
        const tweets = extractTweets(data);

        if (tweets.length > 0) {
          console.log(`[Aerie] Captured ${tweets.length} tweets`);
          sendToCollector(tweets);
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
function extractTweets(data) {
  const tweets = [];
  const seen = new Set();

  function traverse(obj, parent = null, grandparent = null) {
    if (!obj || typeof obj !== "object") return;

    // Check for promoted content indicators at the entry/item level
    // Promoted tweets are often wrapped in special entry types
    const isPromoted = !!(
      obj.promotedMetadata ||
      obj.advertiser_results ||
      parent?.promotedMetadata ||
      parent?.advertiser_results ||
      obj.entryId?.includes("promoted") ||
      obj.entryId?.includes("cursor-ad") ||
      parent?.entryId?.includes("promoted")
    );

    // Build context info to pass to normalizeTweet
    const contextInfo = {
      isPromoted,
      entryId: obj.entryId || parent?.entryId,
      entryType: obj.__typename || obj.entryType,
    };

    // Look for tweet_results.result pattern (most reliable)
    if (obj.tweet_results?.result) {
      const tweetObj = obj.tweet_results.result;
      // Handle tombstone tweets (deleted/unavailable)
      if (tweetObj.__typename === "Tweet" || tweetObj.legacy?.full_text !== undefined) {
        const tweet = normalizeTweet(tweetObj, contextInfo);
        if (tweet && !seen.has(tweet.id)) {
          seen.add(tweet.id);
          tweets.push(tweet);
        }
      }
    }

    // Also look for direct Tweet objects (fallback)
    if (obj.__typename === "Tweet" && obj.legacy?.full_text !== undefined) {
      // Only process if we have user info (to avoid duplicates from above)
      if (obj.core?.user_results || obj.user_results) {
        const tweet = normalizeTweet(obj, contextInfo);
        if (tweet && !seen.has(tweet.id)) {
          seen.add(tweet.id);
          tweets.push(tweet);
        }
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
  return tweets;
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
      media: extractMedia(legacy.extended_entities || legacy.entities),
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

// Send extracted tweets to local collector service
async function sendToCollector(tweets) {
  try {
    const response = await fetch(COLLECTOR_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ tweets }),
    });

    if (!response.ok) {
      console.error(`[Aerie] Collector returned ${response.status}`);
    } else {
      const result = await response.json();
      console.log(`[Aerie] Stored: ${result.inserted} new, ${result.duplicates} duplicates`);
    }
  } catch (err) {
    // Collector might not be running - that's okay, log and continue
    console.warn(`[Aerie] Could not reach collector: ${err.message}`);
  }
}

console.log("[Aerie] Tweet collector extension loaded");
