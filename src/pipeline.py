from copy import deepcopy
from dataclasses import dataclass, field
import time
from typing import Any

from src.processing import EventExtractor
from src.processing.extractor import ExtractionUnavailable
from src.scrapers import GoogleNewsArticleScraper, SubTelForumScraper, SubmarineNetworksScraper
from src.storage import EventStore
from src.storage.article_queue import ArticleQueue
from src.storage.article_state import ArticleState
from src.storage.event_store import event_drop_reason
from src.utils import Config, get_logger
from src.utils.audit import AuditRun, article_fields, error_details
from src.utils.model_health import check_model_health


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
    model_health: dict[str, Any] = field(default_factory=dict)
    queue: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


class MonitorPipeline:
    def __init__(self, config: Config, store: EventStore | None = None,
                 extractor: EventExtractor | None = None, *, health_checker=check_model_health):
        self.config = config
        self.store = store or EventStore(config.event_store_path)
        self.extractor = extractor or EventExtractor(config.llm_model)
        self.health_checker = health_checker
        self.logger = get_logger("pipeline")

    def run(self, sources: list[str] | None = None, since: str | None = None,
            dry_run: bool = False) -> PipelineSummary:
        selected_sources = sources or self.enabled_sources()
        audit = AuditRun(self.config.audit_dir)
        summary = PipelineSummary(run_id=audit.run_id, audit_path=str(audit.path))
        audit.activate()
        audit.emit("run", "started", selected_sources=selected_sources, since=since, dry_run=dry_run)
        queue = None
        deadline = time.monotonic() + max(1, self.config.collection_time_budget_seconds)
        try:
            queue = ArticleQueue.from_config(self.config, dry_run=dry_run)
            article_state = ArticleState.from_config(self.config)
            health = self.health_checker(
                getattr(self.extractor, "api_key", None), getattr(self.extractor, "provider", "unknown"),
                getattr(self.extractor, "base_url", None), self.config.low_balance_threshold,
            )
            summary.model_health = {k: health[k] for k in ("status", "available", "low_balance", "reason", "provider", "http_status") if k in health}
            audit.emit("model_health", "completed" if health.get("status") == "healthy" else "failed",
                       **summary.model_health)
            model_available = health.get("available") is not False
            if not model_available:
                summary.errors += 1

            # Always discover every source before spending time on extraction. All
            # discovered URLs are persisted even while the model is unavailable.
            iterators = []
            for source in selected_sources:
                scraper_cls = SCRAPER_CLASSES.get(source)
                if not scraper_cls:
                    summary.errors += 1
                    audit.emit("source", "failed", source=source, reason="unknown_source")
                    continue
                try:
                    scraper = scraper_cls(self.config)
                    scraper.article_state = article_state
                    scraper.article_queue = queue
                    scraper.fetch_enabled = False
                    scraper.deadline = min(deadline, time.monotonic() + 120)
                    initial_articles = scraper.scrape_articles(since=since, dry_run=dry_run)
                    scraper.fetch_enabled = model_available
                    scraper.deadline = deadline
                    if hasattr(scraper, "iter_queued_articles"):
                        iterator = scraper.iter_queued_articles(dry_run=dry_run)
                    else:
                        # Small custom adapters must also persist discovery before processing.
                        for article in initial_articles:
                            queue.enqueue(article)
                        iterator = iter(initial_articles)
                    iterators.append((source, iterator))
                    audit.emit("source", "discovered", source=source)
                except Exception as exc:
                    summary.errors += 1
                    audit.emit("source", "failed", source=source, reason="scraper_error", **error_details(exc))
                    self.logger.error("Failed discovering %s: %s", source, type(exc).__name__)

            consecutive_model_failures = 0
            # Fetch one article from each source in turn and acknowledge it only
            # after its events are safely committed. A stopped run loses no URLs.
            while iterators and model_available and time.monotonic() < deadline:
                remaining = []
                for source, iterator in iterators:
                    if not model_available or time.monotonic() >= deadline:
                        break
                    try:
                        article = next(iterator)
                    except StopIteration:
                        audit.emit("source", "completed", source=source)
                        continue
                    except Exception as exc:
                        summary.errors += 1
                        audit.emit("source", "failed", source=source, reason="scraper_error", **error_details(exc))
                        continue
                    remaining.append((source, iterator))
                    fields = article_fields(article)
                    row = queue.get(article) or queue.enqueue(article)
                    if row["status"] in {"done", "review"}:
                        audit.emit("select", "skipped", **fields, reason="already_processed" if row["status"] == "done" else "awaiting_review")
                        continue
                    # Respect retry backoff even for third-party scraper adapters.
                    if row["queue_id"] not in {item["queue_id"] for item in queue.due()}:
                        continue
                    if time.monotonic() >= deadline:
                        audit.emit("process", "deferred", **fields, reason="time_budget")
                        break
                    summary.articles += 1
                    before_events = deepcopy(self.store.events)
                    changes = []
                    rejected = False
                    try:
                        events = self.extractor.extract(article)
                        method = getattr(self.extractor, "last_extraction_method", "unavailable")
                        if method != "llm":
                            raise ExtractionUnavailable("rule_fallback_disabled")
                        consecutive_model_failures = 0
                        summary.candidates += len(events)
                        audit.emit("extract", "completed", **fields, method=method, candidate_count=len(events), reason="no_candidates" if not events else "candidates_found")
                        for index, event in enumerate(events):
                            candidate = {**fields, "candidate_index": index, "cable_name": event.get("cable_name"), "occurrence_date": event.get("occurrence_date"), "method": method}
                            if not self.extractor.is_real_event(event):
                                summary.rejected += 1
                                rejected = True
                                reason = self.extractor.rejection_reason(event) if hasattr(self.extractor, "rejection_reason") else "verification_rejected"
                                audit.emit("verify", "rejected", **candidate, reason=reason, verification_method="rules")
                                continue
                            event["verification_status"] = "rule_verified"
                            event = self.store.normalize_event(event)
                            candidate["candidate_uid"] = event["event_uid"]
                            reason = event_drop_reason(event)
                            if reason:
                                summary.rejected += 1
                                rejected = True
                                audit.emit("quality", "rejected", **candidate, reason=reason)
                                continue
                            audit.emit("verify", "accepted", **candidate, verification_method="rules")
                            if dry_run:
                                audit.emit("store", "skipped", **candidate, reason="dry_run", event_uid=event["event_uid"])
                            else:
                                inserted, stored = self.store.upsert(event)
                                changes.append((inserted, candidate, stored["event_uid"]))
                        if not dry_run:
                            # Never acknowledge a URL before the store is durable.
                            self.store.save()
                            for inserted, candidate, event_uid in changes:
                                audit.emit("store", "inserted" if inserted else "merged", **candidate, event_uid=event_uid)
                                summary.inserted += int(inserted)
                                summary.merged += int(not inserted)
                            if rejected:
                                queue.mark_review(row, "candidates_rejected")
                            else:
                                queue.mark_done(row, "processed" if events else "no_candidates")
                            queue.persist()
                            if not rejected:
                                article_state.mark_processed(article.get("discovered_url"), article.get("url"))
                                article_state.save()
                            audit.emit("queue", "review" if rejected else "done", **{**fields, "queue_id": row["queue_id"]}, reason="candidates_rejected" if rejected else "processed")
                    except ExtractionUnavailable as exc:
                        summary.errors += 1
                        consecutive_model_failures += 1
                        queue.mark_retry(row, exc.reason, error_type=type(exc).__name__, http_status=exc.http_status)
                        queue.persist()
                        audit.emit("extract", "failed", **fields, reason=exc.reason, provider=exc.provider, http_status=exc.http_status)
                        if exc.reason in {"balance_exhausted", "authentication_failed", "missing_api_key", "rate_limited"} or consecutive_model_failures >= 3:
                            model_available = False
                            summary.model_health.update(status="unavailable", available=False, reason=exc.reason,
                                                        provider=exc.provider, http_status=exc.http_status,
                                                        low_balance=exc.reason == "balance_exhausted")
                            audit.emit("model_health", "failed", reason="circuit_open", provider=exc.provider, http_status=exc.http_status)
                    except OSError:
                        # Store/queue disk failures are fatal: leave the durable URL
                        # unacknowledged and prevent later writes of partial state.
                        self.store.events = before_events
                        raise
                    except Exception as exc:
                        self.store.events = before_events
                        summary.errors += 1
                        queue.mark_retry(row, "processing_error", **error_details(exc))
                        queue.persist()
                        audit.emit("process", "failed", **fields, reason="processing_error", **error_details(exc))
                iterators = remaining
            if time.monotonic() >= deadline:
                audit.emit("collection", "deferred", reason="time_budget")
        except Exception as exc:
            summary.errors += 1
            summary.status = "failed"
            audit.emit("run", "failed", reason="pipeline_error", **error_details(exc))
            raise
        finally:
            if queue is not None:
                summary.queue = queue.stats()
            summary.warnings = max(0, audit.failures - summary.errors)
            summary.warnings += sum(count for key, count in audit.counts.items()
                                    if key.startswith(("discover.deferred.", "collection.deferred.")))
            if any(summary.queue.get(key, 0) for key in ("pending", "retry", "review")):
                summary.warnings += 1
            if summary.status != "failed" and (summary.errors or summary.warnings):
                summary.status = "failed" if summary.errors and not summary.articles else "degraded"
            audit.emit("run", "completed", status=summary.status, errors=summary.errors, warnings=summary.warnings)
            audit.finish(summary.to_dict())
            self.logger.info("Audit run %s: %s (%s)", audit.run_id, summary.status, audit.path)
        return summary

    def enabled_sources(self) -> list[str]:
        return [name for name in ("google_news", "subtelforum", "submarinenetworks")
                if self.config.scrapers.get(name) is None or self.config.scrapers[name].enabled]
