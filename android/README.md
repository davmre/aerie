# Aerie Reader - Android App

A native Android app for reading your filtered social feed from an Aerie server.

## Features

- View approved posts as conversation chains
- Threaded tweet display with connected lines
- Quoted tweet rendering
- Platform badges (X / Bluesky)
- Retweet attribution
- Engagement metrics (likes, retweets, replies)
- Pull-to-refresh
- Infinite scroll pagination
- Secure credential storage (encrypted with Android Keystore)

## Setup

### Prerequisites

- Android Studio Hedgehog (2023.1.1) or later
- JDK 17+
- Android SDK with API level 34

### Building

1. Open the `android/` directory in Android Studio
2. Wait for Gradle sync to complete
3. Run on an emulator or device

### Connecting to Your Server

1. Launch the app
2. Enter your Aerie server URL (e.g., `https://aerie.example.com`)
3. Enter your HTTP Basic Auth credentials
4. Tap "Connect"

The app will verify credentials by calling the `/stats` endpoint before saving them.

## Architecture

```
app/src/main/java/com/aerie/reader/
├── AerieReaderApp.kt          # Application class
├── MainActivity.kt             # Entry point + navigation
├── data/
│   ├── api/
│   │   ├── AerieApi.kt        # Retrofit interface
│   │   ├── ApiModule.kt       # API factory
│   │   └── AuthInterceptor.kt # Adds auth headers
│   ├── auth/
│   │   └── CredentialsManager.kt  # Encrypted credential storage
│   ├── models/
│   │   ├── Chain.kt           # Chain/Mode data classes
│   │   └── Tweet.kt           # Tweet/Retweeter data classes
│   └── repository/
│       └── FeedRepository.kt  # Data source abstraction
├── ui/
│   ├── components/
│   │   ├── ChainCard.kt       # Conversation chain card
│   │   └── TweetItem.kt       # Individual tweet display
│   ├── screens/
│   │   ├── FeedScreen.kt      # Main feed
│   │   └── LoginScreen.kt     # Credentials entry
│   └── theme/
│       └── Theme.kt           # Material 3 theme
└── viewmodel/
    ├── FeedViewModel.kt       # Feed state management
    └── LoginViewModel.kt      # Login flow
```

## API Endpoints Used

| Endpoint | Purpose |
|----------|---------|
| `GET /stats` | Verify credentials |
| `GET /modes` | List filtering modes |
| `GET /api/ui/chains` | Fetch conversation chains |

## Dependencies

- **Jetpack Compose** - Modern declarative UI
- **Material 3** - Design system
- **Navigation Compose** - Navigation
- **Retrofit + OkHttp** - Networking
- **Gson** - JSON parsing
- **Coil** - Image loading (for future media support)
- **Security Crypto** - Encrypted storage

## Future Work

- [ ] Mode/platform selector in toolbar
- [ ] Hidden replies modal drill-down
- [ ] Room database for offline caching
- [ ] WorkManager for background sync
- [ ] Image/media rendering
- [ ] Share sheet integration
- [ ] Push notifications
