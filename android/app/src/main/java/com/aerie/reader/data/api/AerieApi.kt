package com.aerie.reader.data.api

import com.aerie.reader.data.models.ChainResponse
import com.aerie.reader.data.models.Mode
import com.aerie.reader.data.models.StatsResponse
import retrofit2.http.GET
import retrofit2.http.Query

/**
 * Retrofit interface for the Aerie API.
 */
interface AerieApi {

    /**
     * Get conversation chains for the Read page.
     */
    @GET("api/ui/chains")
    suspend fun getChains(
        @Query("mode") mode: String = "default",
        @Query("platform") platform: String? = null,
        @Query("sort") sort: String = "captured_at",
        @Query("limit") limit: Int = 20,
        @Query("before_cursor") beforeCursor: String? = null,
        @Query("after_cursor") afterCursor: String? = null
    ): ChainResponse

    /**
     * Get available filtering modes.
     */
    @GET("modes")
    suspend fun getModes(): List<Mode>

    /**
     * Get server statistics (also used to verify credentials).
     */
    @GET("stats")
    suspend fun getStats(
        @Query("mode") mode: String? = null
    ): StatsResponse
}
