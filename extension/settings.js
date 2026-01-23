// Default settings values
const DEFAULTS = {
    backendUrl: "http://localhost:8080",
    authUsername: "",
    authPassword: "",
    mode: "default",
    pollInterval: 3000,
    pendingOpacity: 0.02,
    filteredOpacity: 0.02
};

// DOM elements
const form = document.getElementById('settings-form');
const backendUrlInput = document.getElementById('backendUrl');
const authUsernameInput = document.getElementById('authUsername');
const authPasswordInput = document.getElementById('authPassword');
const modeSelect = document.getElementById('mode');
const pollIntervalInput = document.getElementById('pollInterval');
const pendingOpacityInput = document.getElementById('pendingOpacity');
const filteredOpacityInput = document.getElementById('filteredOpacity');
const pendingOpacityValue = document.getElementById('pendingOpacity-value');
const filteredOpacityValue = document.getElementById('filteredOpacity-value');
const resetBtn = document.getElementById('reset-btn');
const statusEl = document.getElementById('status');

// Build auth headers if credentials are set
function getAuthHeaders(username, password) {
    if (username && password) {
        const credentials = btoa(`${username}:${password}`);
        return { 'Authorization': `Basic ${credentials}` };
    }
    return {};
}

// Load settings and populate form
async function loadSettings() {
    const settings = await browser.storage.local.get(DEFAULTS);

    backendUrlInput.value = settings.backendUrl;
    authUsernameInput.value = settings.authUsername || "";
    authPasswordInput.value = settings.authPassword || "";
    modeSelect.value = settings.mode;
    pollIntervalInput.value = settings.pollInterval;
    pendingOpacityInput.value = settings.pendingOpacity;
    filteredOpacityInput.value = settings.filteredOpacity;

    updateOpacityDisplay();
    await loadModes(settings.backendUrl, settings.authUsername, settings.authPassword, settings.mode);
}

// Fetch available modes from backend
async function loadModes(backendUrl, username, password, currentMode) {
    try {
        const headers = getAuthHeaders(username, password);
        const response = await fetch(`${backendUrl}/modes`, { headers });
        if (!response.ok) throw new Error('Failed to fetch modes');

        const data = await response.json();
        const modes = data.modes || [];

        // Clear and repopulate mode select
        modeSelect.innerHTML = '';

        if (modes.length === 0) {
            // No modes available, add default
            const option = document.createElement('option');
            option.value = 'default';
            option.textContent = 'Default';
            modeSelect.appendChild(option);
        } else {
            for (const mode of modes) {
                const option = document.createElement('option');
                option.value = mode.id;
                option.textContent = mode.name || mode.id;
                modeSelect.appendChild(option);
            }
        }

        // Restore selected mode
        modeSelect.value = currentMode;

    } catch (err) {
        console.warn('Could not load modes from backend:', err.message);
        // Keep default option
    }
}

// Update opacity display values
function updateOpacityDisplay() {
    pendingOpacityValue.textContent = pendingOpacityInput.value;
    filteredOpacityValue.textContent = filteredOpacityInput.value;
}

// Save settings
async function saveSettings(e) {
    e.preventDefault();

    const settings = {
        backendUrl: backendUrlInput.value.replace(/\/$/, ''), // Remove trailing slash
        authUsername: authUsernameInput.value.trim(),
        authPassword: authPasswordInput.value,
        mode: modeSelect.value,
        pollInterval: parseInt(pollIntervalInput.value, 10),
        pendingOpacity: parseFloat(pendingOpacityInput.value),
        filteredOpacity: parseFloat(filteredOpacityInput.value)
    };

    // Validate
    if (!settings.backendUrl) {
        showStatus('Backend URL is required', 'error');
        return;
    }

    if (settings.pollInterval < 500 || settings.pollInterval > 30000) {
        showStatus('Poll interval must be between 500 and 30000ms', 'error');
        return;
    }

    try {
        await browser.storage.local.set(settings);
        showStatus('Settings saved!', 'success');
    } catch (err) {
        showStatus('Failed to save settings: ' + err.message, 'error');
    }
}

// Reset to defaults
async function resetSettings() {
    await browser.storage.local.set(DEFAULTS);
    await loadSettings();
    showStatus('Settings reset to defaults', 'success');
}

// Show status message
function showStatus(message, type) {
    statusEl.textContent = message;
    statusEl.className = 'status ' + type;

    // Auto-hide after 3 seconds
    setTimeout(() => {
        statusEl.className = 'status';
    }, 3000);
}

// Event listeners
form.addEventListener('submit', saveSettings);
resetBtn.addEventListener('click', resetSettings);
pendingOpacityInput.addEventListener('input', updateOpacityDisplay);
filteredOpacityInput.addEventListener('input', updateOpacityDisplay);

// Reload modes when backend URL or auth changes
let modeLoadTimeout;
function scheduleModesReload() {
    clearTimeout(modeLoadTimeout);
    modeLoadTimeout = setTimeout(() => {
        loadModes(backendUrlInput.value, authUsernameInput.value, authPasswordInput.value, modeSelect.value);
    }, 500);
}
backendUrlInput.addEventListener('input', scheduleModesReload);
authUsernameInput.addEventListener('input', scheduleModesReload);
authPasswordInput.addEventListener('input', scheduleModesReload);

// Initial load
loadSettings();
