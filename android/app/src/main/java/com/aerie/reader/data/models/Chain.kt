package com.aerie.reader.data.models

import com.google.gson.annotations.SerializedName

/**
 * Response from /api/ui/chains endpoint.
 */
data class ChainResponse(
    val chains: List<ChainWrapper>,
    val total: Int,

    @SerializedName("oldest_cursor")
    val oldestCursor: String?,

    @SerializedName("newest_cursor")
    val newestCursor: String?
)

/**
 * A single conversation chain with hidden reply counts.
 */
data class ChainWrapper(
    val chain: List<Tweet>,

    @SerializedName("hidden_replies")
    val hiddenReplies: Map<String, Int>?
)

/**
 * Available filtering mode.
 */
data class Mode(
    val id: String,
    val name: String?,

    @SerializedName("prompt_id")
    val promptId: String?,

    val prefilter: String?,
    val extractor: String?,
    val provider: String?,

    @SerializedName("model_name")
    val modelName: String?
)

/**
 * Server statistics response.
 */
data class StatsResponse(
    val total: Int,
    val classified: Int,
    val approved: Int,
    val filtered: Int,
    val pending: Int,

    @SerializedName("by_platform")
    val byPlatform: Map<String, PlatformStats>?
)

data class PlatformStats(
    val total: Int,
    val classified: Int
)
