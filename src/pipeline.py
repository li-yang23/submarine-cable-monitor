from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
from src.processing.normalization import clean_text


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
    runtime_status: str = "success"
    review_status: str = "clear"
    queue_items: list = field(default_factory=list)
    queue_changes: dict = field(default_factory=dict)
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
        self.extractor = extractor or EventExtractor(config.llm_model, request_timeout=config.llm_request_timeout)
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
        queue_before = {}
        deadline = time.monotonic() + max(1, self.config.collection_time_budget_seconds)
        recovery_deadline = deadline
        if self.config.cross_source_enabled:
            deadline -= min(self.config.cross_source_time_budget_seconds, self.config.collection_time_budget_seconds / 4)
        try:
            queue = ArticleQueue.from_config(self.config, dry_run=dry_run)
            queue_before = {k: (v["status"], v.get("reason", "")) for k, v in queue.items.items()}
            article_state = ArticleState.from_config(self.config)
            health = self.health_checker(
                getattr(self.extractor, "api_key", None), getattr(self.extractor, "provider", "unknown"),
            )
            model_available = health.get("available") is not False
            # Account/balance preflight is disabled. A configured key permits an
            # attempt; only an actual extraction can establish availability.
            summary.model_health = {
                "status": "not_checked", "available": None, "low_balance": False,
                "reason": "balance_check_disabled", "provider": health.get("provider", "unknown"),
            }
            if not model_available:
                summary.model_health.update({k: health[k] for k in ("status", "available", "reason", "http_status") if k in health})
                summary.errors += 1
            if queue.balance_paused():
                model_available = False
                summary.model_health = {
                    "status": "paused", "available": False, "low_balance": False,
                    "reason": "balance_exhausted", "provider": health.get("provider", "unknown"),
                    **queue.model_pause,
                }
                audit.emit("collection", "paused", reason="balance_exhausted", retry_at=queue.model_pause["retry_at"])
            audit.emit("model_health", "skipped" if model_available or queue.balance_paused() else "failed", **summary.model_health)

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
                    if article is None:
                        # A fetch attempt made durable progress without a body.
                        # Give other sources their turn before retrying this one.
                        continue
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
                        summary.model_health = {
                            "status": "available", "available": True, "low_balance": False,
                            "reason": "inference_succeeded", "provider": getattr(self.extractor, "provider", "unknown"),
                        }
                        queue.model_pause = {}
                        consecutive_model_failures = 0
                        summary.candidates += len(events)
                        audit.emit("extract", "completed", **fields, method=method, candidate_count=len(events), reason="no_candidates" if not events else "candidates_found")
                        row["candidate_reviews"] = []
                        row["candidate_review_count"] = len(events)
                        row["candidate_reviews_truncated"] = len(events) > 100
                        for index, event in enumerate(events):
                            raw = event.get("raw_data", {}).get("extracted", {})
                            review = {"candidate_index": index, "cable_name": clean_text(event.get("cable_name"))[:200],
                                      "accident_location": clean_text(event.get("accident_location"))[:200],
                                      "occurrence_date": event.get("occurrence_date"),
                                      "claimed_occurrence_date": clean_text(raw.get("occurrence_date"))[:40],
                                      "evidence_excerpt": clean_text(event.get("original_text"))[:400],
                                      "date_evidence_excerpt": clean_text(raw.get("occurrence_date_evidence"))[:300],
                                      "outcome": "pending"}
                            if index < 100:
                                row["candidate_reviews"].append(review)
                            candidate = {**fields, **{k:v for k,v in review.items() if k != "outcome"}, "method": method}
                            if not self.extractor.is_real_event(event):
                                summary.rejected += 1
                                rejected = True
                                reason = self.extractor.rejection_reason(event) if hasattr(self.extractor, "rejection_reason") else "verification_rejected"
                                review.update(outcome="rejected", reason=reason)
                                audit.emit("verify", "rejected", **candidate, reason=reason, verification_method="rules")
                                continue
                            event["verification_status"] = "rule_verified"
                            event = self.store.normalize_event(event)
                            candidate["candidate_uid"] = event["event_uid"]
                            reason = event_drop_reason(event)
                            if reason:
                                summary.rejected += 1
                                rejected = True
                                review.update(outcome="rejected", reason=reason)
                                audit.emit("quality", "rejected", **candidate, reason=reason)
                                continue
                            review.update(outcome="accepted", event_uid=event["event_uid"])
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
                        queue.mark_retry(row, exc.reason, error_type=type(exc).__name__, http_status=exc.http_status,
                                         delay_seconds=0 if exc.reason == "balance_exhausted" else None)
                        if exc.reason == "balance_exhausted":
                            queue.pause_for_balance()
                        queue.persist()
                        audit.emit("extract", "failed", **fields, reason=exc.reason, provider=exc.provider, http_status=exc.http_status)
                        summary.model_health.update(status="unavailable", available=False, reason=exc.reason,
                                                    provider=exc.provider, http_status=exc.http_status, low_balance=False)
                        if exc.reason == "balance_exhausted":
                            summary.model_health.update(queue.model_pause)
                        if exc.reason in {"balance_exhausted", "authentication_failed", "missing_api_key", "rate_limited"} or consecutive_model_failures >= 3:
                            model_available = False
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
            if iterators and time.monotonic() >= deadline:
                audit.emit("collection", "deferred", reason="time_budget")
            if self.config.cross_source_enabled and model_available and not queue.balance_paused():
                from src.processing.cross_source import CrossSourceRecovery
                recovery = CrossSourceRecovery(self.config, queue, self.extractor, self.store, article_state,
                                               deadline=recovery_deadline, dry_run=dry_run)
                for row in recovery.eligible():
                    if time.monotonic() >= recovery.deadline:
                        audit.emit("cross_source", "deferred", **article_fields(row), reason="time_budget")
                        break
                    try:
                        result = recovery.process(row)
                        if result.get("model_succeeded"):
                            summary.articles += 1
                            summary.model_health = {"status": "available", "available": True, "low_balance": False,
                                "reason": "inference_succeeded", "provider": getattr(self.extractor, "provider", "unknown")}
                            queue.model_pause = {}
                            queue.persist()
                        summary.candidates += result.get("candidates", 0)
                        summary.inserted += result.get("inserted", 0)
                        summary.merged += result.get("merged", 0)
                    except ExtractionUnavailable as exc:
                        summary.errors += 1
                        summary.articles += 1
                        queue.mark_retry(row, exc.reason, http_status=exc.http_status,
                                         delay_seconds=0 if exc.reason == "balance_exhausted" else None)
                        if exc.reason == "balance_exhausted":
                            queue.pause_for_balance()
                        queue.persist()
                        audit.emit("extract", "failed", **article_fields(row), reason=exc.reason,
                                   provider=exc.provider, http_status=exc.http_status, method="cross_source")
                        summary.model_health.update(status="unavailable", available=False, reason=exc.reason,
                                                    provider=exc.provider, http_status=exc.http_status, low_balance=False)
                        if exc.reason == "balance_exhausted":
                            summary.model_health.update(queue.model_pause)
                        row["cross_source"]["reason"] = exc.reason
                        row["cross_source"]["next_search_at"] = (queue.model_pause["retry_at"] if exc.reason == "balance_exhausted"
                            else (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
                        queue.persist()
                        # One failed recovery call ends recovery for this run.
                        break
        except Exception as exc:
            summary.errors += 1
            summary.status = "failed"
            audit.emit("run", "failed", reason="pipeline_error", **error_details(exc))
            raise
        finally:
            if queue is not None:
                summary.queue = queue.stats()
            summary.warnings = max(0, audit.failures - summary.errors)
            # Work deferred by a bounded scan and evidence awaiting review are
            # durable work states, not crashed executions.
            if queue is not None:
                outstanding = {"pending", "retry", "review"}
                summary.queue_changes = {"new": [], "changed": [], "resolved": [], "closed_unresolved": []}
                for key, row in queue.items.items():
                    item = {k: row.get(k) for k in ("queue_id", "title", "url", "status", "reason")}
                    if row["status"] in outstanding:
                        summary.queue_items.append(item)
                        previous = queue_before.get(key)
                        category = "new" if not previous or previous[0] not in outstanding else "changed"
                        if previous != (row["status"], row.get("reason", "")):
                            summary.queue_changes[category].append(item)
                    elif queue_before.get(key, (None,))[0] in outstanding:
                        summary.queue_changes["closed_unresolved" if row["status"] == "unresolved" else "resolved"].append(item)
                summary.review_status = "needs_review" if summary.queue.get("review") else "unresolved_archived" if summary.queue.get("unresolved") else "clear"
            summary.runtime_status = ("failed" if summary.status == "failed" or summary.errors and not summary.articles
                                      else "degraded" if summary.errors or audit.failures or summary.model_health.get("available") is False
                                      else "success")
            summary.status = summary.runtime_status
            audit.emit("run", "completed", status=summary.status, errors=summary.errors, warnings=summary.warnings)
            audit.finish(summary.to_dict())
            self.logger.info("Audit run %s: %s (%s)", audit.run_id, summary.status, audit.path)
        return summary

    def enabled_sources(self) -> list[str]:
        return [name for name in ("google_news", "subtelforum", "submarinenetworks")
                if self.config.scrapers.get(name) is None or self.config.scrapers[name].enabled]
