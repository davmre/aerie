package com.aerie.reader

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.aerie.reader.data.api.ApiModule
import com.aerie.reader.ui.screens.FeedScreen
import com.aerie.reader.ui.screens.LoginScreen
import com.aerie.reader.ui.theme.AerieReaderTheme
import com.aerie.reader.viewmodel.FeedViewModel
import com.aerie.reader.viewmodel.LoginViewModel

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        val app = application as AerieReaderApp

        setContent {
            AerieReaderTheme {
                AerieReaderNavHost(
                    credentialsManager = app.credentialsManager
                )
            }
        }
    }
}

sealed class Screen(val route: String) {
    object Login : Screen("login")
    object Feed : Screen("feed")
}

@Composable
fun AerieReaderNavHost(
    credentialsManager: com.aerie.reader.data.auth.CredentialsManager
) {
    val navController = rememberNavController()

    // Determine start destination based on whether we have credentials
    val startDestination = if (credentialsManager.hasCredentials()) {
        Screen.Feed.route
    } else {
        Screen.Login.route
    }

    // Create ViewModels (in a real app, use ViewModelProvider or Hilt)
    val loginViewModel = remember { LoginViewModel(credentialsManager) }

    NavHost(
        navController = navController,
        startDestination = startDestination
    ) {
        composable(Screen.Login.route) {
            LoginScreen(
                viewModel = loginViewModel,
                onLoginSuccess = {
                    navController.navigate(Screen.Feed.route) {
                        popUpTo(Screen.Login.route) { inclusive = true }
                    }
                }
            )
        }

        composable(Screen.Feed.route) {
            // Create FeedViewModel when entering feed screen
            val feedViewModel = remember { FeedViewModel(credentialsManager) }

            FeedScreen(
                viewModel = feedViewModel,
                onLogout = {
                    loginViewModel.logout()
                    ApiModule.clearCache()
                    navController.navigate(Screen.Login.route) {
                        popUpTo(Screen.Feed.route) { inclusive = true }
                    }
                }
            )
        }
    }
}
