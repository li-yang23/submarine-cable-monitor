#!/usr/bin/env python3
"""
Submarine Cable Monitor - Main entry point.
Scrapes various sources for submarine cable events and stores them in a database.
"""

import os
import sys
import argparse
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.utils import get_logger, Config
from src.storage import Database
from src.scrapers import (
    TeleGeographyScraper,
    InfrapediaScraper,
    CableFaultsScraper,
    GoogleNewsScraper,
    GitHubScraper
)


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Submarine Cable Monitor - Global submarine cable event monitoring"
    )

    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to configuration file (default: config.yaml)"
    )

    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Initialize the database and exit"
    )

    parser.add_argument(
        "--export-json",
        metavar="PATH",
        help="Export database to JSON file and exit"
    )

    parser.add_argument(
        "--export-csv",
        metavar="PATH",
        help="Export database to CSV file and exit"
    )

    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Clean up old events beyond retention period"
    )

    parser.add_argument(
        "--scrapers",
        nargs="+",
        help="Specific scrapers to run (default: all enabled)"
    )

    return parser.parse_args()


def get_scrapers(config: Config):
    """
    Get list of scraper instances.

    Args:
        config: Configuration object

    Returns:
        List of scraper instances
    """
    scrapers = [
        TeleGeographyScraper(config),
        InfrapediaScraper(config),
        CableFaultsScraper(config),
        GoogleNewsScraper(config),
        GitHubScraper(config),
    ]

    # Filter by enabled in config
    enabled = []
    for scraper in scrapers:
        scraper_config = config.scrapers.get(scraper.source_name.lower().replace(" ", "_"))
        if scraper_config is None or scraper_config.enabled:
            enabled.append(scraper)

    return enabled


def main():
    """Main entry point."""
    args = parse_args()
    logger = get_logger("main")

    # Load config
    config_path = os.path.join(project_root, args.config)
    config = Config.from_yaml(config_path)

    # Initialize database
    db_path = os.path.join(project_root, config.database_path)
    db = Database(db_path)
    db.initialize()

    if args.init_db:
        logger.info("Database initialized successfully")
        return 0

    if args.export_json:
        export_path = os.path.join(project_root, args.export_json)
        logger.info(f"Exporting to JSON: {export_path}")
        db.export_to_json(export_path)
        return 0

    if args.export_csv:
        export_path = os.path.join(project_root, args.export_csv)
        logger.info(f"Exporting to CSV: {export_path}")
        db.export_to_csv(export_path)
        return 0

    if args.cleanup:
        logger.info(f"Cleaning up old events (retention: {config.data_retention_days} days)")
        deleted = db.cleanup_old_events(config.data_retention_days)
        logger.info(f"Deleted {deleted} old events")
        return 0

    # Run all scrapers
    logger.info("=" * 60)
    logger.info("Submarine Cable Monitor")
    logger.info("=" * 60)

    all_scrapers = get_scrapers(config)

    # Filter if specific scrapers requested
    if args.scrapers:
        requested = [s.lower() for s in args.scrapers]
        all_scrapers = [
            s for s in all_scrapers
            if s.source_name.lower().replace(" ", "_") in requested
            or s.source_name.lower() in requested
        ]
        if not all_scrapers:
            logger.error(f"No matching scrapers found for: {args.scrapers}")
            return 1

    total_events = 0

    for scraper in all_scrapers:
        logger.info(f"Running scraper: {scraper.source_name}")
        try:
            with scraper:
                events = scraper.scrape()
                if events:
                    ids = db.insert_events(events)
                    inserted = len([i for i in ids if i > 0])
                    logger.info(f"  - Found {len(events)} events, inserted {inserted} new")
                    total_events += inserted
                else:
                    logger.info(f"  - No events found")
        except Exception as e:
            logger.error(f"Error running {scraper.source_name}: {e}")

    logger.info("=" * 60)
    logger.info(f"Run complete. Total new events: {total_events}")
    logger.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
