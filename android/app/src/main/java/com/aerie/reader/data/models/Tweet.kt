package com.aerie.reader.data.models

import com.google.gson.annotations.SerializedName

/**
 * A single tweet/post from Twitter or Bluesky.
 */
data class Tweet(
    val id: String,
    val text: String,
    val platform: String,

    @SerializedName("author_id")
    val authorId: String?,

    @SerializedName("author_username")
    val authorUsername: String?,

    @SerializedName("author_display_name")
    val authorDisplayName: String?,

    @SerializedName("author_verified")
    val authorVerified: Boolean?,

    @SerializedName("author_blue_verified")
    val authorBlueVerified: Boolean?,

    @SerializedName("author_bio")
    val authorBio: String?,

    @SerializedName("author_following")
    val authorFollowing: Boolean?,

    @SerializedName("author_followers_count")
    val authorFollowersCount: Int?,

    @SerializedName("created_at")
    val createdAt: String?,

    @SerializedName("captured_at")
    val capturedAt: String?,

    @SerializedName("like_count")
    val likeCount: Int?,

    @SerializedName("retweet_count")
    val retweetCount: Int?,

    @SerializedName("reply_count")
    val replyCount: Int?,

    @SerializedName("quote_count")
    val quoteCount: Int?,

    @SerializedName("reply_to_tweet_id")
    val replyToTweetId: String?,

    @SerializedName("reply_to_username")
    val replyToUsername: String?,

    @SerializedName("is_retweet")
    val isRetweet: Boolean?,

    @SerializedName("is_quote")
    val isQuote: Boolean?,

    @SerializedName("is_promoted")
    val isPromoted: Boolean?,

    @SerializedName("quoted_tweet_id")
    val quotedTweetId: String?,

    @SerializedName("quoted_tweet")
    val quotedTweet: Tweet?,

    @SerializedName("retweeted_by")
    val retweetedBy: List<Retweeter>?,

    val responses: List<PromptResponse>?,

    @SerializedName("urls_json")
    val urlsJson: String?,

    @SerializedName("media_json")
    val mediaJson: String?
)

/**
 * Represents someone who retweeted/reposted a tweet.
 */
data class Retweeter(
    @SerializedName("retweeter_username")
    val username: String,

    @SerializedName("retweeter_display_name")
    val displayName: String?,

    @SerializedName("retweeter_user_id")
    val userId: String?,

    @SerializedName("retweeted_at")
    val retweetedAt: String?
)

/**
 * An LLM prompt response for classification.
 */
data class PromptResponse(
    @SerializedName("prompt_id")
    val promptId: String,

    val model: String?,

    val response: Map<String, Any>?
)
