package com.aerie.reader.data.repository

import com.aerie.reader.data.api.AerieApi
import com.aerie.reader.data.models.ChainResponse
import com.aerie.reader.data.models.ChainWrapper
import com.aerie.reader.data.models.Mode
import com.aerie.reader.data.models.StatsResponse

/**
 * Repository for fetching feed data from the Aerie server.
 * Abstracts the API layer and can be extended for caching.
 */
class FeedRepository(private val api: AerieApi) {

    /**
     * Fetch conversation chains.
     */
    suspend fun getChains(
        mode: String = "default",
        platform: String? = null,
        sort: String = "captured_at",
        limit: Int = 20,
        beforeCursor: String? = null,
        afterCursor: String? = null
    ): Result<ChainResponse> {
        return try {
            val response = api.getChains(
                mode = mode,
                platform = platform,
                sort = sort,
                limit = limit,
                beforeCursor = beforeCursor,
                afterCursor = afterCursor
            )
            Result.success(response)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * Fetch available modes.
     */
    suspend fun getModes(): Result<List<Mode>> {
        return try {
            val modes = api.getModes()
            Result.success(modes)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * Fetch server stats.
     */
    suspend fun getStats(mode: String? = null): Result<StatsResponse> {
        return try {
            val stats = api.getStats(mode)
            Result.success(stats)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
}
