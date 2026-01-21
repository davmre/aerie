#!/usr/bin/env python3
"""
Migration script to add platform support to Aerie database.

This script:
1. Adds platform and platform_metadata columns to tweets table
2. Adds platform column to retweets table
3. Sets all existing tweets to platform='twitter'
4. Shows statistics about the migration

The migration is idempotent - safe to run multiple times.
"""

import sys
from pathlib import Path

from database import DEFAULT_DB_PATH, get_platform_stats, init_database, transaction


def migrate(db_path: Path = DEFAULT_DB_PATH):
    """Run the platform migration."""
    print(f"Migrating database at {db_path}...")
    print()

    # Run init_database which includes the migration logic
    print("Step 1: Adding platform columns...")
    init_database(db_path)
    print("✓ Platform columns added (or already exist)")
    print()

    # Verify all existing tweets have a platform set
    print("Step 2: Verifying existing tweets have platform set...")
    with transaction(db_path) as conn:
        # Count tweets without platform
        null_platform = conn.execute(
            "SELECT COUNT(*) FROM tweets WHERE platform IS NULL"
        ).fetchone()[0]

        if null_platform > 0:
            print(f"  Found {null_platform} tweets without platform, setting to 'twitter'...")
            conn.execute("UPDATE tweets SET platform = 'twitter' WHERE platform IS NULL")
            print(f"✓ Updated {null_platform} tweets")
        else:
            print("✓ All tweets have platform set")
    print()

    # Show platform statistics
    print("Step 3: Platform statistics")
    stats = get_platform_stats(db_path)
    total = sum(stats.values())

    print(f"  Total tweets: {total}")
    for platform, count in sorted(stats.items()):
        percentage = (count / total * 100) if total > 0 else 0
        print(f"  - {platform}: {count} ({percentage:.1f}%)")
    print()

    # Verify retweets table
    print("Step 4: Verifying retweets table...")
    with transaction(db_path) as conn:
        # Count retweets without platform
        null_platform_rt = conn.execute(
            "SELECT COUNT(*) FROM retweets WHERE platform IS NULL"
        ).fetchone()[0]

        if null_platform_rt > 0:
            print(
                f"  Found {null_platform_rt} retweets without platform, setting to 'twitter'..."
            )
            conn.execute("UPDATE retweets SET platform = 'twitter' WHERE platform IS NULL")
            print(f"✓ Updated {null_platform_rt} retweets")
        else:
            print("✓ All retweets have platform set")

        # Show retweet count
        total_retweets = conn.execute("SELECT COUNT(*) FROM retweets").fetchone()[0]
        print(f"  Total retweets: {total_retweets}")
    print()

    print("✓ Migration complete!")
    print()
    print("Next steps:")
    print("  - All existing tweets are now labeled as 'twitter'")
    print("  - New tweets can specify 'platform' field (defaults to 'twitter')")
    print("  - Bluesky posts can be added with platform='bluesky'")
    print("  - Use get_platform_stats() to see platform breakdown")


if __name__ == "__main__":
    # Allow custom db path as argument
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB_PATH

    if not db_path.exists():
        print(f"Error: Database not found at {db_path}")
        sys.exit(1)

    migrate(db_path)
