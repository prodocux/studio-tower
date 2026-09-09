"""
Migration CLI to backfill `project_tags: List[str]` array on legacy activity events.
Supports dry-run verification and idempotent live execution in Firestore and local stores.

Usage:
  python -m app.cli.migrate_activity_tags [--dry-run | --execute] [--batch-size 100]
"""

import argparse
import logging
import sys

from app.core.config import settings
from app.services.storage import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_activity_tags")


def run_migration(dry_run: bool = True, batch_size: int = 100) -> int:
    logger.info(f"Starting activity_tags backfill migration (mode={'DRY_RUN' if dry_run else 'EXECUTE'})...")
    migrated_count = 0

    if type(store).__name__ == "FirestoreStore" and getattr(store, "client", None):
        from google.cloud import firestore
        client = store.client

        docs = list(client.collection("activity_events").stream())
        logger.info(f"Scanned {len(docs)} activity events in Firestore.")

        batch = client.batch()
        batch_ops = 0

        for doc in docs:
            data = doc.to_dict()
            current_tags = data.get("project_tags")
            legacy_tag = data.get("project_tag")

            if current_tags is None or len(current_tags) == 0:
                target_tags = [legacy_tag] if legacy_tag and legacy_tag != "all" else ["general"]
                migrated_count += 1
                logger.info(f"Event '{doc.id}': Backfilling project_tags={target_tags} (from legacy={legacy_tag})")

                if not dry_run:
                    batch.update(doc.reference, {"project_tags": target_tags})
                    batch_ops += 1
                    if batch_ops >= batch_size:
                        batch.commit()
                        batch = client.batch()
                        batch_ops = 0

        if not dry_run and batch_ops > 0:
            batch.commit()
    else:
        # In-memory / local disk store
        with store._lock:
            for space_id, ev_list in store.activity_events.items():
                for ev in ev_list:
                    if not getattr(ev, "project_tags", None):
                        target_tags = [ev.project_tag] if ev.project_tag and ev.project_tag != "all" else ["general"]
                        migrated_count += 1
                        logger.info(f"Local Event '{ev.event_id}': Backfilling project_tags={target_tags}")
                        if not dry_run:
                            ev.project_tags = target_tags

        if not dry_run and store.persist_path:
            store._save_state_to_disk()

    logger.info(f"Migration completed successfully. Total records migrated: {migrated_count} (dry_run={dry_run})")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Backfill project_tags array in activity_events.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True, help="Simulate migration without modifying documents (default)")
    group.add_argument("--execute", action="store_true", help="Execute writes to the database")
    parser.add_argument("--batch-size", type=int, default=100, help="Batch size for Firestore write batches")
    args = parser.parse_args()

    dry_run = not args.execute
    sys.exit(run_migration(dry_run=dry_run, batch_size=args.batch_size))


if __name__ == "__main__":
    main()
