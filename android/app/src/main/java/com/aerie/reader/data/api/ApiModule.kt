package com.aerie.reader.data.api

import com.aerie.reader.data.auth.CredentialsManager
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

/**
 * Factory for creating configured API instances.
 */
object ApiModule {

    private var currentBaseUrl: String? = null
    private var api: AerieApi? = null
    private var okHttpClient: OkHttpClient? = null

    /**
     * Get or create an AerieApi instance for the given credentials manager.
     * Creates a new instance if the server URL has changed.
     */
    fun getApi(credentialsManager: CredentialsManager): AerieApi? {
        val serverUrl = credentialsManager.getServerUrl() ?: return null

        // Return cached instance if URL hasn't changed
        if (api != null && currentBaseUrl == serverUrl) {
            return api
        }

        // Create new instance
        val client = createOkHttpClient(credentialsManager)
        okHttpClient = client

        val retrofit = Retrofit.Builder()
            .baseUrl("$serverUrl/")
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()

        api = retrofit.create(AerieApi::class.java)
        currentBaseUrl = serverUrl

        return api
    }

    /**
     * Create a one-time API instance for testing credentials.
     * Doesn't cache the instance.
     */
    fun createTestApi(serverUrl: String, username: String, password: String): AerieApi {
        val loggingInterceptor = HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.BASIC
        }

        val client = OkHttpClient.Builder()
            .addInterceptor { chain ->
                val authHeader = okhttp3.Credentials.basic(username, password)
                val request = chain.request().newBuilder()
                    .header("Authorization", authHeader)
                    .build()
                chain.proceed(request)
            }
            .addInterceptor(loggingInterceptor)
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .build()

        val normalizedUrl = normalizeUrl(serverUrl)

        return Retrofit.Builder()
            .baseUrl("$normalizedUrl/")
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(AerieApi::class.java)
    }

    /**
     * Clear the cached API instance (call when logging out).
     */
    fun clearCache() {
        api = null
        currentBaseUrl = null
        okHttpClient = null
    }

    private fun createOkHttpClient(credentialsManager: CredentialsManager): OkHttpClient {
        val loggingInterceptor = HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.BASIC
        }

        return OkHttpClient.Builder()
            .addInterceptor(AuthInterceptor(credentialsManager))
            .addInterceptor(loggingInterceptor)
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .build()
    }

    private fun normalizeUrl(url: String): String {
        var normalized = url.trim()
        if (!normalized.startsWith("http://") && !normalized.startsWith("https://")) {
            normalized = "https://$normalized"
        }
        return normalized.trimEnd('/')
    }
}
