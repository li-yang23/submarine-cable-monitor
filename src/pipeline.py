from dataclasses import dataclass
from typing import Any

from src.processing import EventExtractor
from src.scrapers import GoogleNewsArticleScraper, SubTelForumScraper, SubmarineNetworksScraper
from src.storage import EventStore
from src.storage.article_state import ArticleState
from src.storage.event_store import event_drop_reason
from src.utils import Config, get_logger
from src.utils.audit import AuditRun, article_fields, error_details


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
    warnings: int = 0
    status: str = "success"
    run_id: str = ""
    audit_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


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
        audit = AuditRun(self.config.audit_dir)
        summary = PipelineSummary(run_id=audit.run_id, audit_path=str(audit.path))
        audit.activate()
        audit.emit("run", "started", selected_sources=selected_sources, since=since, dry_run=dry_run)
        try:
            article_state = ArticleState.from_config(self.config)
            for source in selected_sources:
                scraper_cls = SCRAPER_CLASSES.get(source)
                if not scraper_cls:
                    summary.errors += 1
                    audit.emit("source", "failed", source=source, reason="unknown_source")
                    self.logger.warning("Unknown source skipped: %s", source)
                    continue

                try:
                    scraper = scraper_cls(self.config)
                    scraper.article_state = article_state
                    articles = scraper.scrape_articles(since=since, dry_run=dry_run)
                    summary.articles += len(articles)
                    audit.emit("source", "completed", source=source, article_count=len(articles))
                    self.logger.info("%s returned %s articles", source, len(articles))
                except Exception as exc:
                    summary.errors += 1
                    audit.emit("source", "failed", source=source, reason="scraper_error", **error_details(exc))
                    self.logger.error("Failed scraping %s: %s", source, type(exc).__name__)
                    continue

                for article in articles:
                    fields = article_fields(article)
                    if article_state.is_processed(article.get("discovered_url"), article.get("url")):
                        audit.emit("select", "skipped", **fields, reason="already_processed")
                        continue
                    try:
                        events = self.extractor.extract(article)
                        method = getattr(self.extractor, "last_extraction_method", "llm" if getattr(self.extractor, "available", False) else "rules")
                        summary.candidates += len(events)
                        audit.emit("extract", "completed", **fields, method=method, candidate_count=len(events), reason="no_candidates" if not events else "candidates_found")
                        for index, event in enumerate(events):
                            candidate = {**fields, "candidate_index": index, "cable_name": event.get("cable_name"), "occurrence_date": event.get("occurrence_date"), "method": method}
                            if not self.extractor.is_real_event(event):
                                summary.rejected += 1
                                reason = self.extractor.rejection_reason(event) if hasattr(self.extractor, "rejection_reason") else "verification_rejected"
                                audit.emit("verify", "rejected", **candidate, reason=reason, verification_method="rules")
                                continue
                            # is_real_event is a rule check even when extraction used an LLM.
                            event["verification_status"] = "rule_verified"
                            event = self.store.normalize_event(event)
                            candidate["candidate_uid"] = event["event_uid"]
                            reason = event_drop_reason(event)
                            if reason:
                                summary.rejected += 1
                                audit.emit("quality", "rejected", **candidate, reason=reason)
                                continue
                            audit.emit("verify", "accepted", **candidate, verification_method="rules")
                            if dry_run:
                                audit.emit("store", "skipped", **candidate, reason="dry_run", event_uid=event["event_uid"])
                                continue
                            inserted, stored = self.store.upsert(event)
                            audit.emit("store", "inserted" if inserted else "merged", **candidate, event_uid=stored["event_uid"])
                            if inserted:
                                summary.inserted += 1
                            else:
                                summary.merged += 1
                        if method == "llm" and not dry_run:
                            article_state.mark_processed(article.get("discovered_url"), article.get("url"))
                    except Exception as exc:
                        summary.errors += 1
                        audit.emit("process", "failed", **fields, reason="processing_error", **error_details(exc))
                        self.logger.error("Failed processing article %s: %s", fields["article_id"], type(exc).__name__)

            if not dry_run:
                self.store.save()
                article_state.save()
        except Exception as exc:
            summary.errors += 1
            summary.status = "failed"
            audit.emit("run", "failed", reason="pipeline_error", **error_details(exc))
            raise
        finally:
            summary.warnings = max(0, audit.failures - summary.errors)
            if summary.status != "failed" and (summary.errors or summary.warnings):
                summary.status = "failed" if summary.errors and not summary.articles else "degraded"
            audit.emit("run", "completed", status=summary.status, errors=summary.errors, warnings=summary.warnings)
            audit.finish(summary.to_dict())
            self.logger.info("Audit run %s: %s (%s)", audit.run_id, summary.status, audit.path)
        return summary

    def enabled_sources(self) -> list[str]:
        sources: list[str] = []
        for name in ("google_news", "subtelforum", "submarinenetworks"):
            scraper_config = self.config.scrapers.get(name)
            if scraper_config is None or scraper_config.enabled:
                sources.append(name)
        return sources
