package com.aerie.reader.data.api

import com.aerie.reader.data.auth.CredentialsManager
import okhttp3.Credentials
import okhttp3.Interceptor
import okhttp3.Response

/**
 * OkHttp interceptor that adds HTTP Basic Auth header to all requests.
 */
class AuthInterceptor(
    private val credentialsManager: CredentialsManager
) : Interceptor {

    override fun intercept(chain: Interceptor.Chain): Response {
        val creds = credentialsManager.getCredentials()

        val request = if (creds != null) {
            val authHeader = Credentials.basic(creds.username, creds.password)
            chain.request().newBuilder()
                .header("Authorization", authHeader)
                .build()
        } else {
            chain.request()
        }

        return chain.proceed(request)
    }
}
