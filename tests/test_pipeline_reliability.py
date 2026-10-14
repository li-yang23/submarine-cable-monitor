import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.pipeline import MonitorPipeline
from src.processing.extractor import EventExtractor, ExtractionUnavailable
from src.storage import EventStore
from src.storage.article_queue import ArticleQueue
from src.storage.article_state import ArticleState
from src.utils import Config
from src.utils.audit import emit


HEALTHY = {"status": "healthy", "available": True, "low_balance": False, "reason": "balance_available", "provider": "siliconflow"}


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
        pipeline = MonitorPipeline(self.config, self.store, extractor, health_checker=lambda *args: dict(HEALTHY if health is None else health))
        with patch("src.pipeline.SCRAPER_CLASSES", classes):
            return pipeline.run(list(discoveries), **kwargs)

    def test_first_402_stops_model_calls_but_keeps_all_other_news_pending(self):
        extractor = self.extractor(side_effect=ExtractionUnavailable("balance_exhausted", http_status=402, provider="siliconflow"))
        discoveries = {source: [article(source, "a"), article(source, "b")] for source in ("first", "second", "third")}
        summary = self.run_pipeline(discoveries, extractor)
        extractor.extract.assert_called_once()
        self.assertEqual(summary.queue["retry"], 1)
        self.assertEqual(summary.queue["pending"], 5)
        self.assertEqual(summary.queue["done"], 0)
        self.assertEqual(summary.model_health["status"], "unavailable")
        self.assertEqual(self.trace[:3], [("discover", "first"), ("discover", "second"), ("discover", "third")])
        self.assertEqual(len([entry for entry in self.trace if entry[0] == "fetch"]), 1)
        self.assertFalse(ArticleState.from_config(self.config).is_processed(article()["url"]))

    def test_zero_balance_still_discovers_all_sources_without_fetching(self):
        extractor = self.extractor()
        health = {**HEALTHY, "status": "unavailable", "available": False, "low_balance": True, "reason": "balance_exhausted", "balance": "secret-account-amount"}
        summary = self.run_pipeline({source: [article(source)] for source in ("first", "second", "third")}, extractor, health)
        extractor.extract.assert_not_called()
        self.assertEqual(self.trace, [("discover", "first"), ("discover", "second"), ("discover", "third")])
        self.assertEqual(summary.queue["pending"], 3)
        self.assertEqual(ArticleQueue.from_config(self.config).counts()["pending"], 3)
        self.assertEqual(summary.status, "failed")
        self.assertNotIn("balance", summary.model_health)
        self.assertNotIn("secret-account-amount", (Path(summary.audit_path) / "summary.json").read_text())

    def test_sources_are_processed_in_round_robin_order_after_discovery(self):
        extractor = self.extractor()
        sources = ("first", "second", "third")
        summary = self.run_pipeline({source: [article(source, "a"), article(source, "b")] for source in sources}, extractor)
        calls = [call.args[0]["url"] for call in extractor.extract.call_args_list]
        self.assertEqual(calls, [article(source, key)["url"] for key in ("a", "b") for source in sources])
        self.assertEqual(summary.queue["done"], 6)
        self.assertEqual(summary.status, "success")

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
        self.assertEqual(summary.status, "degraded")
        self.assertGreater(summary.warnings, 0)
        self.assertFalse(ArticleState.from_config(self.config).is_processed(item["url"]))
        self.assertEqual(self.store.events, [])
        extractor.extract.reset_mock()
        again = self.run_pipeline({"first": []}, extractor)
        extractor.extract.assert_not_called()
        self.assertEqual(again.queue["review"], 1)
        self.assertNotEqual(again.status, "success")

    def test_queue_recovers_across_runs_even_after_news_disappears_from_rss(self):
        item = article()
        initial = self.extractor()
        self.run_pipeline({"first": [item]}, initial, {**HEALTHY, "status": "unavailable", "available": False})
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

    def test_low_balance_warns_without_disabling_available_model(self):
        extractor = self.extractor()
        health = {**HEALTHY, "status": "low_balance", "low_balance": True, "reason": "balance_below_threshold"}
        summary = self.run_pipeline({"first": [article()]}, extractor, health)
        extractor.extract.assert_called_once()
        self.assertEqual(summary.queue["done"], 1)
        self.assertEqual(summary.model_health["status"], "low_balance")
        self.assertEqual(summary.status, "degraded")
        self.assertGreater(summary.warnings, 0)

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
        pipeline = MonitorPipeline(self.config, self.store, self.extractor(), health_checker=lambda *args: HEALTHY)
        with patch("src.pipeline.SCRAPER_CLASSES", {"first": lambda config: scraper}):
            summary = pipeline.run(["first"])
        self.assertEqual(summary.queue["pending"], 0)
        self.assertGreater(summary.warnings, 0)
        self.assertEqual(summary.status, "degraded")


if __name__ == "__main__":
    unittest.main()
