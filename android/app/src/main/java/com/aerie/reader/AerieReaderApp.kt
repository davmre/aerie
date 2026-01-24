package com.aerie.reader

import android.app.Application
import com.aerie.reader.data.auth.CredentialsManager

/**
 * Application class for Aerie Reader.
 * Initializes app-wide dependencies.
 */
class AerieReaderApp : Application() {

    lateinit var credentialsManager: CredentialsManager
        private set

    override fun onCreate() {
        super.onCreate()
        instance = this

        // Initialize the credentials manager
        credentialsManager = CredentialsManager(this)
    }

    companion object {
        lateinit var instance: AerieReaderApp
            private set
    }
}
