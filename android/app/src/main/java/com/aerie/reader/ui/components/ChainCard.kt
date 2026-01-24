package com.aerie.reader.ui.components

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Repeat
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aerie.reader.data.models.ChainWrapper
import com.aerie.reader.data.models.Tweet

/**
 * Displays a conversation chain as a card.
 * Shows connected tweets with thread lines and retweet attribution.
 */
@Composable
fun ChainCard(
    chainWrapper: ChainWrapper,
    modifier: Modifier = Modifier
) {
    val chain = chainWrapper.chain
    if (chain.isEmpty()) return

    val firstTweet = chain.first()
    val retweeters = firstTweet.retweetedBy

    Card(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surface
        ),
        elevation = CardDefaults.cardElevation(defaultElevation = 1.dp)
    ) {
        Column(
            modifier = Modifier.padding(16.dp)
        ) {
            // Retweet attribution (if this was retweeted)
            if (!retweeters.isNullOrEmpty()) {
                RetweetAttribution(retweeters = retweeters)
                Spacer(modifier = Modifier.height(8.dp))
            }

            // Display each tweet in the chain
            chain.forEachIndexed { index, tweet ->
                val isLast = index == chain.lastIndex
                val showConnector = chain.size > 1

                TweetItem(
                    tweet = tweet,
                    showConnector = showConnector,
                    isLast = isLast
                )

                // Add spacing between tweets (but not after the last one)
                if (!isLast) {
                    Spacer(modifier = Modifier.height(8.dp))
                }
            }

            // Hidden replies indicator
            val hiddenReplies = chainWrapper.hiddenReplies
            if (!hiddenReplies.isNullOrEmpty()) {
                val totalHidden = hiddenReplies.values.sum()
                if (totalHidden > 0) {
                    Spacer(modifier = Modifier.height(12.dp))
                    HiddenRepliesIndicator(count = totalHidden)
                }
            }
        }
    }
}

@Composable
private fun RetweetAttribution(
    retweeters: List<com.aerie.reader.data.models.Retweeter>,
    modifier: Modifier = Modifier
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier.padding(start = 48.dp)
    ) {
        Icon(
            imageVector = Icons.Default.Repeat,
            contentDescription = "Retweeted",
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.height(14.dp)
        )
        Spacer(modifier = Modifier.width(8.dp))

        val names = retweeters
            .take(3)
            .mapNotNull { it.displayName ?: it.username }

        val text = when {
            names.isEmpty() -> "Retweeted"
            names.size == 1 -> "${names[0]} retweeted"
            names.size == 2 -> "${names[0]} and ${names[1]} retweeted"
            else -> "${names[0]}, ${names[1]} and ${retweeters.size - 2} others retweeted"
        }

        Text(
            text = text,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            fontWeight = FontWeight.Medium
        )
    }
}

@Composable
private fun HiddenRepliesIndicator(
    count: Int,
    modifier: Modifier = Modifier
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier.padding(start = 48.dp)
    ) {
        Text(
            text = "$count hidden ${if (count == 1) "reply" else "replies"}",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.primary,
            fontWeight = FontWeight.Medium
        )
    }
}
