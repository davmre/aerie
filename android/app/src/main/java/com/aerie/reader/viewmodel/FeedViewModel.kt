package com.aerie.reader.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.aerie.reader.data.api.AerieApi
import com.aerie.reader.data.api.ApiModule
import com.aerie.reader.data.auth.CredentialsManager
import com.aerie.reader.data.models.ChainWrapper
import com.aerie.reader.data.models.Mode
import com.aerie.reader.data.repository.FeedRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class FeedUiState(
    val chains: List<ChainWrapper> = emptyList(),
    val modes: List<Mode> = emptyList(),
    val isLoading: Boolean = false,
    val isLoadingMore: Boolean = false,
    val isRefreshing: Boolean = false,
    val error: String? = null,
    val hasMore: Boolean = true,
    val selectedMode: String = "default",
    val selectedPlatform: String? = null, // null = all platforms
    val oldestCursor: String? = null,
    val newestCursor: String? = null
)

class FeedViewModel(
    private val credentialsManager: CredentialsManager
) : ViewModel() {

    private val _uiState = MutableStateFlow(FeedUiState())
    val uiState: StateFlow<FeedUiState> = _uiState.asStateFlow()

    private var repository: FeedRepository? = null

    init {
        initializeApi()
    }

    private fun initializeApi() {
        val api = ApiModule.getApi(credentialsManager)
        if (api != null) {
            repository = FeedRepository(api)
            loadInitialData()
        }
    }

    private fun loadInitialData() {
        viewModelScope.launch {
            // Load modes first
            repository?.getModes()?.onSuccess { modes ->
                _uiState.update { it.copy(modes = modes) }
            }

            // Then load chains
            loadChains()
        }
    }

    fun loadChains() {
        val repo = repository ?: return

        _uiState.update { it.copy(isLoading = true, error = null) }

        viewModelScope.launch {
            val state = _uiState.value
            repo.getChains(
                mode = state.selectedMode,
                platform = state.selectedPlatform
            ).onSuccess { response ->
                _uiState.update {
                    it.copy(
                        chains = response.chains,
                        isLoading = false,
                        hasMore = response.oldestCursor != null,
                        oldestCursor = response.oldestCursor,
                        newestCursor = response.newestCursor
                    )
                }
            }.onFailure { e ->
                _uiState.update {
                    it.copy(
                        isLoading = false,
                        error = e.message ?: "Failed to load feed"
                    )
                }
            }
        }
    }

    fun loadMoreChains() {
        val repo = repository ?: return
        val state = _uiState.value

        if (state.isLoadingMore || !state.hasMore || state.oldestCursor == null) {
            return
        }

        _uiState.update { it.copy(isLoadingMore = true) }

        viewModelScope.launch {
            repo.getChains(
                mode = state.selectedMode,
                platform = state.selectedPlatform,
                beforeCursor = state.oldestCursor
            ).onSuccess { response ->
                _uiState.update {
                    it.copy(
                        chains = it.chains + response.chains,
                        isLoadingMore = false,
                        hasMore = response.oldestCursor != null && response.chains.isNotEmpty(),
                        oldestCursor = response.oldestCursor
                    )
                }
            }.onFailure { e ->
                _uiState.update {
                    it.copy(
                        isLoadingMore = false,
                        error = e.message ?: "Failed to load more"
                    )
                }
            }
        }
    }

    fun refresh() {
        val repo = repository ?: return

        _uiState.update { it.copy(isRefreshing = true, error = null) }

        viewModelScope.launch {
            val state = _uiState.value
            repo.getChains(
                mode = state.selectedMode,
                platform = state.selectedPlatform
            ).onSuccess { response ->
                _uiState.update {
                    it.copy(
                        chains = response.chains,
                        isRefreshing = false,
                        hasMore = response.oldestCursor != null,
                        oldestCursor = response.oldestCursor,
                        newestCursor = response.newestCursor
                    )
                }
            }.onFailure { e ->
                _uiState.update {
                    it.copy(
                        isRefreshing = false,
                        error = e.message ?: "Failed to refresh"
                    )
                }
            }
        }
    }

    fun setMode(modeId: String) {
        if (modeId != _uiState.value.selectedMode) {
            _uiState.update {
                it.copy(
                    selectedMode = modeId,
                    chains = emptyList(),
                    oldestCursor = null,
                    newestCursor = null
                )
            }
            loadChains()
        }
    }

    fun setPlatform(platform: String?) {
        if (platform != _uiState.value.selectedPlatform) {
            _uiState.update {
                it.copy(
                    selectedPlatform = platform,
                    chains = emptyList(),
                    oldestCursor = null,
                    newestCursor = null
                )
            }
            loadChains()
        }
    }

    fun clearError() {
        _uiState.update { it.copy(error = null) }
    }
}
