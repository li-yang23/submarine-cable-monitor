from dataclasses import dataclass
from typing import Any

from src.processing import EventExtractor
from src.scrapers import GoogleNewsArticleScraper, SubTelForumScraper, SubmarineNetworksScraper
from src.storage import EventStore
from src.utils import Config, get_logger


SCRAPER_CLASSES = {
    "google_news": GoogleNewsArticleScraper,
    "subtelforum": SubTelForumScraper,
    "submarinenetworks": SubmarineNetworksScraper,
    "submarine_networks": SubmarineNetworksScraper,
}


@dataclass
class PipelineSummary:
    articles: int = 0
    candidates: int = 0
    inserted: int = 0
    merged: int = 0
    rejected: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "articles": self.articles,
            "candidates": self.candidates,
            "inserted": self.inserted,
            "merged": self.merged,
            "rejected": self.rejected,
            "errors": self.errors,
        }


class MonitorPipeline:
    def __init__(self, config: Config, store: EventStore | None = None, extractor: EventExtractor | None = None):
        self.config = config
        self.store = store or EventStore(config.event_store_path)
        self.extractor = extractor or EventExtractor(config.llm_model)
        self.logger = get_logger("pipeline")

    def run(
        self,
        sources: list[str] | None = None,
        since: str | None = None,
        dry_run: bool = False,
    ) -> PipelineSummary:
        selected_sources = sources or self.enabled_sources()
        summary = PipelineSummary()

        for source in selected_sources:
            scraper_cls = SCRAPER_CLASSES.get(source)
            if not scraper_cls:
                self.logger.warning("Unknown source skipped: %s", source)
                continue

            try:
                scraper = scraper_cls(self.config)
                articles = scraper.scrape_articles(since=since, dry_run=dry_run)
                summary.articles += len(articles)
                self.logger.info("%s returned %s articles", source, len(articles))
            except Exception as exc:
                summary.errors += 1
                self.logger.exception("Failed scraping %s: %s", source, exc)
                continue

            for article in articles:
                try:
                    events = self.extractor.extract(article)
                    summary.candidates += len(events)
                    for event in events:
                        if not self.extractor.is_real_event(event):
                            summary.rejected += 1
                            continue
                        event["verification_status"] = "verified" if self.extractor.available else "rule_verified"
                        if dry_run:
                            continue
                        duplicate_checker = self.extractor.is_duplicate if self.extractor.available else None
                        inserted, _ = self.store.upsert(event, duplicate_checker=duplicate_checker)
                        if inserted:
                            summary.inserted += 1
                        else:
                            summary.merged += 1
                except Exception as exc:
                    summary.errors += 1
                    self.logger.exception("Failed processing article %s: %s", article.get("url"), exc)

        if not dry_run:
            self.store.save()
        return summary

    def enabled_sources(self) -> list[str]:
        sources: list[str] = []
        for name in ("google_news", "subtelforum", "submarinenetworks"):
            scraper_config = self.config.scrapers.get(name)
            if scraper_config is None or scraper_config.enabled:
                sources.append(name)
        return sources
