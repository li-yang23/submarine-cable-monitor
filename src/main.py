#!/usr/bin/env python3
"""Submarine Cable Monitor CLI."""

import argparse
import os
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.storage import EventStore
from src.utils import Config, get_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submarine Cable Monitor - collect, deduplicate, verify, and export cable events."
    )
    parser.add_argument("--config", default="config.yaml", help="Path to configuration file.")
    parser.add_argument("--init-db", action="store_true", help="Initialize the event store and exit.")
    parser.add_argument("--run", action="store_true", help="Run the article collection and event extraction pipeline.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and process a small sample without writing data.")
    parser.add_argument("--since", help="Only collect articles/events on or after YYYY-MM-DD when source dates are known.")
    parser.add_argument(
        "--sources",
        "--scrapers",
        nargs="+",
        help="Sources to run: google_news subtelforum submarinenetworks.",
    )
    parser.add_argument("--import-history", metavar="CSV", help="Import historical extractor CSV into the canonical store.")
    parser.add_argument("--purge-source", metavar="SOURCE", help="Remove events from a source before export or rerun.")
    parser.add_argument("--clean-store", action="store_true", help="Drop low-value events and merge duplicates in the canonical store.")
    parser.add_argument("--translate-missing", action="store_true", help="Fill missing Chinese translations for original_text with the configured LLM.")
    parser.add_argument("--translate-limit", type=int, help="Maximum number of missing translations to fill in one run.")
    parser.add_argument("--export-json", metavar="PATH", help="Export canonical events to JSON.")
    parser.add_argument("--export-csv", metavar="PATH", help="Export canonical events to CSV.")
    return parser.parse_args()


def resolve_path(path: str) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        return str(candidate)
    return str(project_root / candidate)


def main() -> int:
    args = parse_args()
    logger = get_logger("main")
    config = Config.from_yaml(resolve_path(args.config))
    store = EventStore(resolve_path(config.event_store_path))

    if args.init_db:
        store.save()
        logger.info("Event store initialized at %s", store.path)
        return 0

    if args.import_history:
        imported = store.import_history_csv(resolve_path(args.import_history))
        logger.info("Imported %s historical events into %s", imported, store.path)

    if args.purge_source:
        deleted = store.purge_source(args.purge_source)
        logger.info("Deleted %s events for source %s", deleted, args.purge_source)

    if args.clean_store:
        summary = store.clean_events()
        logger.info("Cleaned event store: %s", summary)

    if args.translate_missing:
        from src.processing import EventExtractor

        extractor = EventExtractor(config.llm_model)
        summary = store.translate_missing(extractor.translate_original_text, limit=args.translate_limit)
        logger.info("Translated missing snippets: %s", summary)

    should_run = args.run or not any(
        [
            args.import_history,
            args.purge_source,
            args.clean_store,
            args.translate_missing,
            args.export_json,
            args.export_csv,
            args.init_db,
        ]
    )
    if should_run:
        from src.pipeline import MonitorPipeline

        pipeline = MonitorPipeline(config, store=store)
        summary = pipeline.run(sources=normalize_sources(args.sources), since=args.since, dry_run=args.dry_run)
        logger.info("Pipeline summary: %s", summary.to_dict())

    if args.export_json:
        store.export_json(resolve_path(args.export_json))
        logger.info("Exported JSON to %s", args.export_json)

    if args.export_csv:
        store.export_csv(resolve_path(args.export_csv))
        logger.info("Exported CSV to %s", args.export_csv)

    return 0


def normalize_sources(sources: list[str] | None) -> list[str] | None:
    if not sources:
        return None
    aliases = {
        "google": "google_news",
        "google-news": "google_news",
        "subtel": "subtelforum",
        "subtel_forum": "subtelforum",
        "submarine-networks": "submarinenetworks",
        "submarine_networks": "submarinenetworks",
    }
    return [aliases.get(source.lower(), source.lower()) for source in sources]


if __name__ == "__main__":
    sys.exit(main())
