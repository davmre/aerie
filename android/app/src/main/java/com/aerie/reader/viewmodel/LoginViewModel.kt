package com.aerie.reader.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.aerie.reader.data.api.ApiModule
import com.aerie.reader.data.auth.CredentialsManager
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import retrofit2.HttpException

data class LoginUiState(
    val serverUrl: String = "",
    val username: String = "",
    val password: String = "",
    val isLoading: Boolean = false,
    val error: String? = null,
    val isLoggedIn: Boolean = false
)

class LoginViewModel(
    private val credentialsManager: CredentialsManager
) : ViewModel() {

    private val _uiState = MutableStateFlow(LoginUiState())
    val uiState: StateFlow<LoginUiState> = _uiState.asStateFlow()

    init {
        // Check if already logged in
        if (credentialsManager.hasCredentials()) {
            _uiState.update { it.copy(isLoggedIn = true) }
        }
    }

    fun updateServerUrl(url: String) {
        _uiState.update { it.copy(serverUrl = url, error = null) }
    }

    fun updateUsername(username: String) {
        _uiState.update { it.copy(username = username, error = null) }
    }

    fun updatePassword(password: String) {
        _uiState.update { it.copy(password = password, error = null) }
    }

    fun login() {
        val state = _uiState.value

        // Validate inputs
        if (state.serverUrl.isBlank()) {
            _uiState.update { it.copy(error = "Server URL is required") }
            return
        }
        if (state.username.isBlank()) {
            _uiState.update { it.copy(error = "Username is required") }
            return
        }
        if (state.password.isBlank()) {
            _uiState.update { it.copy(error = "Password is required") }
            return
        }

        _uiState.update { it.copy(isLoading = true, error = null) }

        viewModelScope.launch {
            try {
                // Test credentials by calling /stats
                val testApi = ApiModule.createTestApi(
                    serverUrl = state.serverUrl,
                    username = state.username,
                    password = state.password
                )

                // This will throw if credentials are invalid (401)
                testApi.getStats()

                // Credentials work - save them
                credentialsManager.saveCredentials(
                    serverUrl = state.serverUrl,
                    username = state.username,
                    password = state.password
                )

                _uiState.update { it.copy(isLoading = false, isLoggedIn = true) }

            } catch (e: HttpException) {
                val errorMsg = when (e.code()) {
                    401 -> "Invalid username or password"
                    403 -> "Access denied"
                    404 -> "Server not found at this URL"
                    else -> "Server error: ${e.code()}"
                }
                _uiState.update { it.copy(isLoading = false, error = errorMsg) }

            } catch (e: Exception) {
                val errorMsg = when {
                    e.message?.contains("Unable to resolve host") == true ->
                        "Cannot connect to server"
                    e.message?.contains("timeout") == true ->
                        "Connection timed out"
                    e.message?.contains("CLEARTEXT") == true ->
                        "Server requires HTTPS"
                    else -> e.message ?: "Connection failed"
                }
                _uiState.update { it.copy(isLoading = false, error = errorMsg) }
            }
        }
    }

    fun logout() {
        credentialsManager.clearCredentials()
        ApiModule.clearCache()
        _uiState.update {
            LoginUiState(isLoggedIn = false)
        }
    }
}
