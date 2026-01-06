#!/usr/bin/env python3
"""
Migration script to convert legacy classification data to the new prompt_responses format.

This migrates tweets that have classification_status='completed' in the old schema
to prompt_responses entries for the 'binary_filter_v1' prompt.
"""

import json
from pathlib import Path

from database import (
    DEFAULT_DB_PATH,
    init_database,
    transaction,
    store_prompt_response,
)
from classifier import setup_prompts_and_modes


def migrate_legacy_classifications(db_path: Path = DEFAULT_DB_PATH, dry_run: bool = False):
    """
    Migrate legacy classification_result fields to prompt_responses.

    For each tweet with classification_status='completed':
    1. Create a prompt_response entry with the binary_filter_v1 prompt
    2. The response will have {"approved": bool, "reason": str}
    """
    # Ensure new schema exists
    setup_prompts_and_modes(db_path)

    # Find tweets with legacy classification that don't have prompt_responses yet
    with transaction(db_path) as conn:
        rows = conn.execute("""
            SELECT t.id, t.classification_result, t.classification_reason, t.classified_at
            FROM tweets t
            LEFT JOIN prompt_responses pr
                ON t.id = pr.tweet_id AND pr.prompt_id = 'binary_filter_v1'
            WHERE t.classification_status = 'completed'
              AND pr.tweet_id IS NULL
        """).fetchall()

    if not rows:
        print("No legacy classifications to migrate.")
        return

    print(f"Found {len(rows)} tweets with legacy classification to migrate.")

    if dry_run:
        print("(Dry run - no changes will be made)")
        for row in rows[:5]:
            status = "approved" if row["classification_result"] == 1 else "filtered"
            reason = row["classification_reason"] or "no reason"
            print(f"  {row['id']}: {status} - {reason[:50]}...")
        if len(rows) > 5:
            print(f"  ... and {len(rows) - 5} more")
        return

    # Migrate each row
    migrated = 0
    for row in rows:
        response = {
            "approved": row["classification_result"] == 1,
            "reason": row["classification_reason"] or "migrated from legacy",
        }
        store_prompt_response(
            tweet_id=row["id"],
            prompt_id="binary_filter_v1",
            model="legacy",  # Mark as legacy migration
            response=response,
            db_path=db_path,
        )
        migrated += 1

    print(f"Migrated {migrated} classifications to prompt_responses.")
    print("These are marked with model='legacy' to distinguish from new classifications.")


def check_migration_status(db_path: Path = DEFAULT_DB_PATH):
    """Check how many tweets have been migrated vs still in legacy format."""
    with transaction(db_path) as conn:
        # Total tweets
        total = conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]

        # Legacy classified
        legacy = conn.execute("""
            SELECT COUNT(*) FROM tweets
            WHERE classification_status = 'completed'
        """).fetchone()[0]

        # New format (prompt_responses)
        new_format = conn.execute("""
            SELECT COUNT(DISTINCT tweet_id) FROM prompt_responses
        """).fetchone()[0]

        # Migrated (has both)
        migrated = conn.execute("""
            SELECT COUNT(*) FROM tweets t
            JOIN prompt_responses pr ON t.id = pr.tweet_id
            WHERE t.classification_status = 'completed'
              AND pr.prompt_id = 'binary_filter_v1'
        """).fetchone()[0]

    print(f"Total tweets: {total}")
    print(f"Legacy classified: {legacy}")
    print(f"New format (prompt_responses): {new_format}")
    print(f"Migrated (in both): {migrated}")
    print(f"Legacy only (need migration): {legacy - migrated}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Migrate legacy classifications")
    parser.add_argument("--status", action="store_true", help="Check migration status")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Don't make changes")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Database path")

    args = parser.parse_args()

    if args.status:
        check_migration_status(args.db)
    else:
        migrate_legacy_classifications(args.db, args.dry_run)
