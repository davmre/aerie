// Aerie Tweet Collector - Background Script
// Intercepts Twitter API responses using webRequest API (invisible to page JavaScript)

const COLLECTOR_URL = "http://localhost:8080/tweets";

// Twitter API endpoints that contain timeline data
const TIMELINE_PATTERNS = [
  /\/graphql\/[^/]+\/Home(Timeline|LatestTimeline)/,
  /\/graphql\/[^/]+\/UserTweets/,
  /\/graphql\/[^/]+\/TweetDetail/,
  /\/2\/timeline\/home/,
];

function isTimelineEndpoint(url) {
  return TIMELINE_PATTERNS.some(pattern => pattern.test(url));
}

// Track pending responses (url -> {chunks, encoding})
const pendingResponses = new Map();

browser.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (!isTimelineEndpoint(details.url)) {
      return;
    }

    // Find content-encoding header
    let encoding = "identity";
    for (const header of details.responseHeaders || []) {
      if (header.name.toLowerCase() === "content-encoding") {
        encoding = header.value.toLowerCase();
        break;
      }
    }

    console.log(`[Aerie] Intercepting timeline response: ${details.url} (encoding: ${encoding})`);

    // Use filterResponseData to read the response body
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
        // Decompress if needed
        let text;
        if (encoding === "gzip" || encoding === "deflate" || encoding === "br") {
          const decompressed = await decompress(combined, encoding);
          text = new TextDecoder().decode(decompressed);
        } else {
          text = new TextDecoder().decode(combined);
        }

        // Parse JSON and extract tweets
        const data = JSON.parse(text);
        const tweets = extractTweets(data);

        if (tweets.length > 0) {
          console.log(`[Aerie] Extracted ${tweets.length} tweets, sending to collector`);
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

// Decompress response body
async function decompress(data, encoding) {
  let decompressionStream;

  if (encoding === "gzip") {
    decompressionStream = new DecompressionStream("gzip");
  } else if (encoding === "deflate") {
    decompressionStream = new DecompressionStream("deflate");
  } else if (encoding === "br") {
    // Brotli - DecompressionStream doesn't support it in all browsers
    // Fall back to trying gzip, or return as-is
    try {
      decompressionStream = new DecompressionStream("gzip");
    } catch {
      console.warn("[Aerie] Brotli decompression not supported, trying raw");
      return data;
    }
  } else {
    return data;
  }

  const stream = new Blob([data]).stream().pipeThrough(decompressionStream);
  const response = new Response(stream);
  return new Uint8Array(await response.arrayBuffer());
}

// Extract tweet objects from Twitter's nested API response
function extractTweets(data) {
  const tweets = [];
  const seen = new Set();

  function traverse(obj) {
    if (!obj || typeof obj !== "object") return;

    // Twitter wraps tweets in various structures - look for the telltale signs
    if (obj.__typename === "Tweet" || obj.legacy?.full_text !== undefined) {
      const tweet = normalizeTweet(obj);
      if (tweet && !seen.has(tweet.id)) {
        seen.add(tweet.id);
        tweets.push(tweet);
      }
    }

    // Recurse into arrays and objects
    if (Array.isArray(obj)) {
      for (const item of obj) {
        traverse(item);
      }
    } else {
      for (const value of Object.values(obj)) {
        traverse(value);
      }
    }
  }

  traverse(data);
  return tweets;
}

// Normalize a tweet object into our standard schema
function normalizeTweet(raw) {
  try {
    // Handle both direct tweet objects and wrapped ones
    const legacy = raw.legacy || raw;
    const core = raw.core?.user_results?.result || {};
    const userLegacy = core.legacy || {};

    // Extract tweet ID - could be in various places
    const id = raw.rest_id || legacy.id_str || legacy.id;
    if (!id) return null;

    return {
      id: String(id),
      text: legacy.full_text || legacy.text || "",
      created_at: legacy.created_at || null,
      author: {
        id: core.rest_id || userLegacy.id_str || legacy.user_id_str || null,
        username: userLegacy.screen_name || null,
        display_name: userLegacy.name || null,
        verified: userLegacy.verified || false,
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
      console.log(`[Aerie] Collector response:`, result);
    }
  } catch (err) {
    // Collector might not be running - that's okay, log and continue
    console.warn(`[Aerie] Could not reach collector at ${COLLECTOR_URL}:`, err.message);
  }
}

console.log("[Aerie] Tweet collector extension loaded");
