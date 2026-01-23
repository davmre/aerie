package com.aerie.reader.data.auth

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Credentials for connecting to the Aerie server.
 */
data class Credentials(
    val serverUrl: String,
    val username: String,
    val password: String
)

/**
 * Manages encrypted storage of server credentials.
 * Uses EncryptedSharedPreferences backed by Android Keystore.
 */
class CredentialsManager(context: Context) {

    private val masterKey = MasterKey.Builder(context)
        .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
        .build()

    private val prefs = EncryptedSharedPreferences.create(
        context,
        PREFS_NAME,
        masterKey,
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
    )

    /**
     * Save credentials securely.
     */
    fun saveCredentials(serverUrl: String, username: String, password: String) {
        prefs.edit()
            .putString(KEY_SERVER_URL, normalizeUrl(serverUrl))
            .putString(KEY_USERNAME, username)
            .putString(KEY_PASSWORD, password)
            .apply()
    }

    /**
     * Get stored credentials, or null if not set.
     */
    fun getCredentials(): Credentials? {
        val serverUrl = prefs.getString(KEY_SERVER_URL, null) ?: return null
        val username = prefs.getString(KEY_USERNAME, null) ?: return null
        val password = prefs.getString(KEY_PASSWORD, null) ?: return null
        return Credentials(serverUrl, username, password)
    }

    /**
     * Check if credentials are stored.
     */
    fun hasCredentials(): Boolean {
        return prefs.contains(KEY_SERVER_URL) &&
                prefs.contains(KEY_USERNAME) &&
                prefs.contains(KEY_PASSWORD)
    }

    /**
     * Clear stored credentials.
     */
    fun clearCredentials() {
        prefs.edit()
            .remove(KEY_SERVER_URL)
            .remove(KEY_USERNAME)
            .remove(KEY_PASSWORD)
            .apply()
    }

    /**
     * Get just the server URL (for API configuration).
     */
    fun getServerUrl(): String? {
        return prefs.getString(KEY_SERVER_URL, null)
    }

    /**
     * Normalize URL: ensure it has a scheme and no trailing slash.
     */
    private fun normalizeUrl(url: String): String {
        var normalized = url.trim()

        // Add https:// if no scheme provided
        if (!normalized.startsWith("http://") && !normalized.startsWith("https://")) {
            normalized = "https://$normalized"
        }

        // Remove trailing slash
        return normalized.trimEnd('/')
    }

    companion object {
        private const val PREFS_NAME = "aerie_credentials"
        private const val KEY_SERVER_URL = "server_url"
        private const val KEY_USERNAME = "username"
        private const val KEY_PASSWORD = "password"
    }
}
