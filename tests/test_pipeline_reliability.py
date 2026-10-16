import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from src.pipeline import MonitorPipeline
from src.processing.extractor import EventExtractor, ExtractionUnavailable
from src.scrapers.article_sources import ArticleScraper, GoogleNewsArticleScraper
from src.storage import EventStore
from src.storage.article_queue import ArticleQueue
from src.storage.article_state import ArticleState
from src.utils import Config
from src.utils.audit import emit
from src.utils.model_health import check_model_health


NOT_CHECKED = {"status": "not_checked", "available": None, "low_balance": False, "reason": "balance_check_disabled", "provider": "siliconflow"}


def article(source="first", key="a"):
    return {"source": source, "url": f"https://example.com/{source}/{key}",
            "title": f"WACS cable fault {key}", "published_date": "2026-10-01"}


def event(item):
    return {"source": item["source"], "url": item["url"], "title": item["title"],
            "cable_name": "WACS", "occurrence_date": "2026-10-01", "affected_area": "South Africa",
            "original_text": "WACS cable fault affected South Africa."}


class QueueScraper:
    """A source that can discover nothing while earlier queued URLs remain fetchable."""

    def __init__(self, source, discoveries, trace):
        self.source = source
        self.discoveries = discoveries
        self.trace = trace

    def scrape_articles(self, **kwargs):
        if self.fetch_enabled:
            raise AssertionError("Discovery must not fetch article bodies")
        self.trace.append(("discover", self.source))
        for item in self.discoveries:
            self.article_queue.enqueue(item)
        return []

    def iter_queued_articles(self, **kwargs):
        if not self.fetch_enabled:
            raise AssertionError("Fetch started despite unavailable model")
        for row in self.article_queue.due(self.source):
            self.trace.append(("fetch", row["url"]))
            self.article_queue.mark_attempt(row)
            self.article_queue.persist()
            yield {**row, "content": "WACS cable fault affected South Africa."}


class PipelineReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = Config(audit_dir=str(self.root / "runs"), event_store_path=str(self.root / "events.jsonl"),
                             article_queue_path=str(self.root / "queue.json"), article_state_path=str(self.root / "cache.json"))
        self.store = EventStore(self.config.event_store_path)
        self.trace = []

    def extractor(self, *, result=None, side_effect=None, method="llm", valid=True):
        return SimpleNamespace(api_key="test-only", provider="siliconflow", base_url="https://api.siliconflow.cn/v1",
                               last_extraction_method=method, extract=Mock(return_value=[] if result is None else result, side_effect=side_effect),
                               is_real_event=lambda candidate: valid)

    def run_pipeline(self, discoveries, extractor, health=None, **kwargs):
        classes = {source: (lambda config, source=source, items=items: QueueScraper(source, items, self.trace))
                   for source, items in discoveries.items()}
        checker = check_model_health if health is None else lambda *args: dict(health)
        pipeline = MonitorPipeline(self.config, self.store, extractor, health_checker=checker)
        with patch("src.pipeline.SCRAPER_CLASSES", classes):
            return pipeline.run(list(discoveries), **kwargs)

    def test_rejected_candidate_excerpt_and_claimed_date_remain_traceable(self):
        item = article()
        candidate = event(item)
        candidate.update(cable_name="", occurrence_date=None, original_text="Short evidence. " + "x" * 700,
                         raw_data={"article": {"content": "FULL_BODY_NOT_TO_LOG"},
                                   "extracted": {"occurrence_date": "2025-09-01", "occurrence_date_evidence": "September 2025"}})
        result = self.run_pipeline({"first": [item]}, self.extractor(result=[candidate], valid=False))
        row = ArticleQueue.from_config(self.config).get(item['url'])
        saved = row['candidate_reviews'][0]
        self.assertEqual(row['status'], 'review')
        self.assertEqual(saved['candidate_index'], 0)
        self.assertEqual(saved['outcome'], 'rejected')
        self.assertEqual(saved['claimed_occurrence_date'], '2025-09-01')
        self.assertIsNone(saved['occurrence_date'])
        self.assertEqual(saved['date_evidence_excerpt'], 'September 2025')
        self.assertLessEqual(len(saved['evidence_excerpt']), 400)
        audit = (Path(result.audit_path) / 'audit.jsonl').read_text()
        self.assertIn('Short evidence.', audit)
        self.assertNotIn('FULL_BODY_NOT_TO_LOG', audit)
        self.assertEqual(row['candidate_review_count'], 1)
        self.assertFalse(row['candidate_reviews_truncated'])

    def test_first_402_stops_model_calls_but_keeps_all_other_news_pending(self):
        extractor = self.extractor(side_effect=ExtractionUnavailable("balance_exhausted", http_status=402, provider="siliconflow"))
        discoveries = {source: [article(source, "a"), article(source, "b")] for source in ("first", "second", "third")}
        summary = self.run_pipeline(discoveries, extractor)
        extractor.extract.assert_called_once()
        self.assertEqual(summary.queue["retry"], 1)
        self.assertEqual(summary.queue["pending"], 5)
        self.assertEqual(summary.queue["done"], 0)
        self.assertEqual(summary.model_health["status"], "unavailable")
        self.assertEqual(summary.model_health["reason"], "balance_exhausted")
        self.assertFalse(summary.model_health["low_balance"])
        self.assertEqual(self.trace[:3], [("discover", "first"), ("discover", "second"), ("discover", "third")])
        self.assertEqual(len([entry for entry in self.trace if entry[0] == "fetch"]), 1)
        self.assertFalse(ArticleState.from_config(self.config).is_processed(article()["url"]))

    def test_balance_pause_survives_restart_and_resumes_next_beijing_day(self):
        discoveries = {"first": [article("first", "old"), article("first", "waiting")]}
        failed = self.extractor(side_effect=ExtractionUnavailable("balance_exhausted", http_status=402, provider="siliconflow"))
        # 23:50 Beijing, not the UTC calendar boundary.
        with patch("src.storage.article_queue.utcnow", return_value=datetime(2026, 10, 14, 15, 50, tzinfo=timezone.utc)):
            first = self.run_pipeline(discoveries, failed)
        self.assertEqual(first.model_health["retry_at"], "2026-10-14T16:00:00+00:00")
        queue = ArticleQueue.from_config(self.config)
        queue.requeue(article("first", "old"))
        queue.persist()
        skipped = self.extractor()
        with patch("src.storage.article_queue.utcnow", return_value=datetime(2026, 10, 14, 15, 59, tzinfo=timezone.utc)):
            paused = self.run_pipeline({"first": [article("first", "new")]}, skipped)
        skipped.extract.assert_not_called()
        self.assertEqual(paused.model_health["status"], "paused")
        self.assertEqual(paused.model_health["attempt_id"], first.model_health["attempt_id"])
        self.assertEqual(paused.queue["pending"], 3)
        resumed = self.extractor()
        with patch("src.storage.article_queue.utcnow", return_value=datetime(2026, 10, 14, 16, 17, tzinfo=timezone.utc)):
            result = self.run_pipeline({"first": []}, resumed)
        self.assertEqual(resumed.extract.call_count, 3)
        self.assertEqual(result.queue["done"], 3)
        self.assertEqual(ArticleQueue.from_config(self.config).model_pause, {})

    def test_next_day_still_exhausted_pauses_again_without_losing_backlog(self):
        failed = self.extractor(side_effect=ExtractionUnavailable("balance_exhausted", http_status=402, provider="siliconflow"))
        with patch("src.storage.article_queue.utcnow", return_value=datetime(2026, 10, 14, 0, 17, tzinfo=timezone.utc)):
            first = self.run_pipeline({"first": [article("first", "old"), article("first", "waiting")]}, failed)
        with patch("src.storage.article_queue.utcnow", return_value=datetime(2026, 10, 14, 16, 17, tzinfo=timezone.utc)):
            second = self.run_pipeline({"first": [article("first", "new")]}, failed)
        self.assertEqual(failed.extract.call_count, 2)
        self.assertNotEqual(first.model_health["attempt_id"], second.model_health["attempt_id"])
        self.assertEqual(second.model_health["retry_at"], "2026-10-15T16:00:00+00:00")
        self.assertEqual(second.queue["retry"], 1)
        self.assertEqual(second.queue["pending"], 2)
        self.assertEqual(second.queue["done"], 0)

    def test_dry_run_balance_failure_does_not_persist_pause(self):
        failed = self.extractor(side_effect=ExtractionUnavailable("balance_exhausted", http_status=402, provider="siliconflow"))
        self.run_pipeline({"first": [article()]}, failed, dry_run=True)
        self.assertFalse(Path(self.config.article_queue_path).exists())

    def test_missing_key_still_discovers_all_sources_without_fetching(self):
        extractor = self.extractor()
        extractor.api_key = None
        summary = self.run_pipeline({source: [article(source)] for source in ("first", "second", "third")}, extractor)
        extractor.extract.assert_not_called()
        self.assertEqual(self.trace, [("discover", "first"), ("discover", "second"), ("discover", "third")])
        self.assertEqual(summary.queue["pending"], 3)
        self.assertEqual(ArticleQueue.from_config(self.config).counts()["pending"], 3)
        self.assertEqual(summary.status, "failed")
        self.assertEqual(summary.model_health["reason"], "missing_api_key")
        self.assertNotIn("balance", summary.model_health)

    def test_sources_are_processed_in_round_robin_order_after_discovery(self):
        extractor = self.extractor()
        sources = ("first", "second", "third")
        summary = self.run_pipeline({source: [article(source, "a"), article(source, "b")] for source in sources}, extractor)
        calls = [call.args[0]["url"] for call in extractor.extract.call_args_list]
        self.assertEqual(calls, [article(source, key)["url"] for key in ("a", "b") for source in sources])
        self.assertEqual(summary.queue["done"], 6)
        self.assertEqual(summary.status, "success")

    def test_slow_failing_fetches_yield_before_exhausting_other_source_budget(self):
        # Exercise both actual fetch generators, rather than a fixture that is
        # already cooperative. Each failing request consumes 6 of 10 seconds.
        for scraper_base in (ArticleScraper, GoogleNewsArticleScraper):
            with self.subTest(scraper=scraper_base.__name__), tempfile.TemporaryDirectory() as tmp:
                config = Config(audit_dir=str(Path(tmp) / "runs"), event_store_path=str(Path(tmp) / "events.jsonl"),
                                article_queue_path=str(Path(tmp) / "queue.json"), article_state_path=str(Path(tmp) / "cache.json"),
                                collection_time_budget_seconds=10)
                elapsed = [0.0]
                trace = []

                class Source(scraper_base):
                    def __init__(self, cfg, source):
                        self.source_name = source
                        super().__init__(cfg)

                    def scrape_articles(self, **kwargs):
                        for key in (["a", "b", "c"] if self.source_name == "broken" else ["a"]):
                            self.article_queue.enqueue(article(self.source_name, key))
                        return []

                    def _fetch_article(self, url):
                        trace.append((self.source_name, url))
                        elapsed[0] += 6 if self.source_name == "broken" else 1
                        if self.source_name == "broken":
                            raise requests.Timeout("fixture failure")
                        return {**article(self.source_name), "content": "A cable fault affected services."}

                    def _fetch_news_article(self, url, *args):
                        return self._fetch_article(url)

                extractor = self.extractor()
                classes = {source: (lambda cfg, source=source: Source(cfg, source)) for source in ("broken", "healthy")}
                pipeline = MonitorPipeline(config, EventStore(config.event_store_path), extractor, health_checker=lambda *args: NOT_CHECKED)
                with patch("src.pipeline.SCRAPER_CLASSES", classes), patch("time.monotonic", side_effect=lambda: elapsed[0]):
                    summary = pipeline.run(["broken", "healthy"])
                self.assertEqual([source for source, _ in trace[:2]], ["broken", "healthy"])
                self.assertEqual(summary.articles, 1)
                self.assertEqual(summary.queue["done"], 1)
                self.assertEqual(summary.queue["retry"], 2)
                self.assertEqual(summary.queue["pending"], 1)
                extractor.extract.assert_called_once()
                self.assertEqual(extractor.extract.call_args.args[0]["source"], "healthy")

    def test_store_save_failure_leaves_url_unacknowledged_and_cache_unchanged(self):
        item = article()
        extractor = self.extractor(result=[event(item)])
        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_pipeline({"first": [item]}, extractor)
        queue = ArticleQueue.from_config(self.config)
        self.assertEqual(queue.get(item)["status"], "pending")
        self.assertFalse(ArticleState.from_config(self.config).is_processed(item["url"]))
        self.assertEqual(self.store.events, [])
        self.assertFalse(Path(self.config.event_store_path).exists())
        summary = self.run_pipeline({"first": []}, extractor)
        self.assertEqual(summary.inserted, 1)
        self.assertEqual(summary.queue["done"], 1)
        self.assertTrue(ArticleState.from_config(self.config).is_processed(item["url"]))

    def test_rejected_candidates_remain_reviewable_instead_of_success_cached(self):
        item = article()
        extractor = self.extractor(result=[event(item)], valid=False)
        summary = self.run_pipeline({"first": [item]}, extractor)
        self.assertEqual(summary.rejected, 1)
        self.assertEqual(summary.queue["review"], 1)
        self.assertEqual(summary.runtime_status, "success")
        self.assertEqual(summary.review_status, "needs_review")
        self.assertEqual(summary.warnings, 0)
        self.assertFalse(ArticleState.from_config(self.config).is_processed(item["url"]))
        self.assertEqual(self.store.events, [])
        extractor.extract.reset_mock()
        again = self.run_pipeline({"first": []}, extractor)
        extractor.extract.assert_not_called()
        self.assertEqual(again.queue["review"], 1)
        self.assertEqual(again.runtime_status, "success")
        self.assertEqual(again.review_status, "needs_review")

    def test_queue_recovers_across_runs_even_after_news_disappears_from_rss(self):
        item = article()
        initial = self.extractor()
        self.run_pipeline({"first": [item]}, initial, {**NOT_CHECKED, "status": "unavailable", "available": False, "reason": "missing_api_key"})
        initial.extract.assert_not_called()
        recovered = self.extractor(result=[event(item)])
        # A new store and queue instance mimic a fresh scheduled runner.
        self.store = EventStore(self.config.event_store_path)
        summary = self.run_pipeline({"first": []}, recovered)
        recovered.extract.assert_called_once()
        self.assertEqual(recovered.extract.call_args.args[0]["url"], item["url"])
        self.assertEqual(summary.inserted, 1)
        self.assertEqual(summary.queue["pending"], 0)
        self.assertEqual(summary.status, "success")

    def test_legacy_low_balance_state_does_not_warn_or_disable_extraction(self):
        extractor = self.extractor()
        health = {**NOT_CHECKED, "status": "low_balance", "available": True, "low_balance": True, "reason": "balance_below_threshold"}
        summary = self.run_pipeline({"first": [article()]}, extractor, health)
        extractor.extract.assert_called_once()
        self.assertEqual(summary.queue["done"], 1)
        self.assertEqual(summary.model_health["status"], "available")
        self.assertEqual(summary.model_health["reason"], "inference_succeeded")
        self.assertTrue(summary.model_health["available"])
        self.assertFalse(summary.model_health["low_balance"])
        self.assertEqual(summary.status, "success")
        self.assertEqual(summary.warnings, 0)

    def test_idle_run_does_not_check_balance_or_claim_model_availability(self):
        extractor = self.extractor()
        with patch("requests.sessions.Session.request") as request:
            summary = self.run_pipeline({"first": []}, extractor)
        request.assert_not_called()
        extractor.extract.assert_not_called()
        self.assertEqual(summary.model_health["status"], "not_checked")
        self.assertIsNone(summary.model_health["available"])
        self.assertEqual(summary.status, "success")
        self.assertEqual(summary.warnings, 0)

    def test_legacy_balance_preflight_uncertainty_is_not_a_failed_run(self):
        for reason in ("balance_api_retired", "balance_check_unsupported", "health_unknown"):
            with self.subTest(reason=reason):
                health = {**NOT_CHECKED, "status": "health_unknown", "reason": reason}
                summary = self.run_pipeline({"first": []}, self.extractor(), health)
                self.assertEqual(summary.model_health["status"], "not_checked")
                self.assertIsNone(summary.model_health["available"])
                self.assertEqual(summary.status, "success")
                self.assertEqual(summary.warnings, 0)
                records = [json.loads(line) for line in (Path(summary.audit_path) / "audit.jsonl").read_text().splitlines()]
                self.assertFalse(any(r["stage"] == "model_health" and r["outcome"] == "failed" for r in records))

    def test_actual_model_errors_still_fail_and_keep_article_for_retry(self):
        for index, (reason, status) in enumerate((("authentication_failed", 401), ("rate_limited", 429), ("invalid_response", None))):
            with self.subTest(reason=reason):
                extractor = self.extractor(side_effect=ExtractionUnavailable(reason, http_status=status, provider="siliconflow"))
                summary = self.run_pipeline({"first": [article(key=str(index))]}, extractor)
                extractor.extract.assert_called_once()
                self.assertEqual(summary.model_health["status"], "unavailable")
                self.assertEqual(summary.model_health["reason"], reason)
                self.assertFalse(summary.model_health["available"])
                self.assertEqual(summary.errors, 1)
                self.assertNotEqual(summary.status, "success")
                self.assertEqual(ArticleQueue.from_config(self.config).get(article(key=str(index)))["status"], "retry")

    def test_explicit_rule_fallback_is_never_allowed_to_write_production_events(self):
        item = article()
        extractor = EventExtractor(allow_rule_fallback=True)
        extractor.api_key = "test-only"
        with patch.object(extractor, "_extract_with_llm", side_effect=RuntimeError("provider failure")):
            summary = self.run_pipeline({"first": [item]}, extractor)
        self.assertEqual(extractor.last_extraction_method, "rules_fallback")
        self.assertEqual(summary.inserted, 0)
        self.assertEqual(summary.queue["retry"], 1)
        self.assertEqual(self.store.events, [])
        self.assertFalse(ArticleState.from_config(self.config).is_processed(item["url"]))
        row = ArticleQueue.from_config(self.config).get(item)
        self.assertEqual(row["reason"], "rule_fallback_disabled")

    def test_retry_backoff_and_explicit_requeue_prevent_accidental_charge_loops(self):
        item = article()
        failing = self.extractor(side_effect=ExtractionUnavailable("provider_unavailable", http_status=503, retryable=True))
        self.run_pipeline({"first": [item]}, failing)
        recovered = self.extractor()
        waiting = self.run_pipeline({"first": []}, recovered)
        recovered.extract.assert_not_called()
        self.assertEqual(waiting.queue["retry"], 1)
        queue = ArticleQueue.from_config(self.config)
        self.assertTrue(queue.requeue(item))
        queue.persist()
        result = self.run_pipeline({"first": []}, recovered)
        recovered.extract.assert_called_once()
        self.assertEqual(result.queue["done"], 1)

    def test_three_consecutive_transient_model_failures_open_circuit(self):
        extractor = self.extractor(side_effect=ExtractionUnavailable("provider_unavailable", http_status=503, retryable=True))
        summary = self.run_pipeline({"first": [article("first", str(index)) for index in range(6)]}, extractor)
        self.assertEqual(extractor.extract.call_count, 3)
        self.assertEqual(summary.queue["retry"], 3)
        self.assertEqual(summary.queue["pending"], 3)
        self.assertEqual(summary.model_health["available"], False)

    def test_dry_run_never_acknowledges_or_writes_queued_work(self):
        item = article()
        queue = ArticleQueue.from_config(self.config)
        queue.enqueue(item)
        before = Path(self.config.article_queue_path).read_bytes()
        summary = self.run_pipeline({"first": []}, self.extractor(result=[event(item)]), dry_run=True)
        self.assertEqual(summary.candidates, 1)
        self.assertEqual(Path(self.config.article_queue_path).read_bytes(), before)
        self.assertFalse(Path(self.config.article_state_path).exists())
        self.assertFalse(Path(self.config.event_store_path).exists())
        records = [json.loads(line) for line in (Path(summary.audit_path) / "audit.jsonl").read_text().splitlines()]
        self.assertFalse(any(record["stage"] == "queue" and record["outcome"] == "done" for record in records))

    def test_unfinished_discovery_warns_even_when_no_article_has_been_enqueued(self):
        def discover(**kwargs):
            emit("discover", "deferred", source="first", reason="discovery_budget")
            return []
        scraper = SimpleNamespace(scrape_articles=discover, iter_queued_articles=lambda **kwargs: iter(()))
        pipeline = MonitorPipeline(self.config, self.store, self.extractor(), health_checker=lambda *args: NOT_CHECKED)
        with patch("src.pipeline.SCRAPER_CLASSES", {"first": lambda config: scraper}):
            summary = pipeline.run(["first"])
        self.assertEqual(summary.queue["pending"], 0)
        self.assertEqual(summary.warnings, 0)
        self.assertEqual(summary.runtime_status, "success")


if __name__ == "__main__":
    unittest.main()
