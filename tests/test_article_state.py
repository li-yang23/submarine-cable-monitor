import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.pipeline import MonitorPipeline
from src.processing.extractor import EventExtractor
from src.scrapers.article_sources import GoogleNewsArticleScraper
from src.storage import EventStore
from src.storage.article_queue import ArticleQueue
from src.storage.article_state import ArticleState
from src.utils import Config
from src.utils.audit import AuditRun


def article(key="a"):
    return {"source": "fixture", "title": "Submarine cable article", "discovered_url": f"https://news.google.com/{key}", "url": f"https://example.com/{key}", "content": "Public article body"}


def config_for(directory, **kwargs):
    return Config(audit_dir=str(Path(directory) / "runs"), article_state_path=str(Path(directory) / "article-state.json"), article_queue_path=str(Path(directory) / "queue.json"), **kwargs)


def run_fixture(config, extractor, articles=None, store=None, dry_run=False):
    store = store or EventStore(str(Path(config.article_state_path).with_name("events.jsonl")))
    scraper = SimpleNamespace(scrape_articles=lambda **kwargs: articles or [article()])
    with patch("src.pipeline.SCRAPER_CLASSES", {"fixture": lambda config: scraper}):
        return MonitorPipeline(config, store, extractor, health_checker=lambda *args: {"status": "healthy", "available": True}).run(["fixture"], dry_run=dry_run)


def feed(*keys):
    return ("<rss><channel>" + "".join(f"<item><title>{key}</title><link>https://news.google.com/{key}</link><pubDate>Wed, 14 Oct 2026 00:00:00 GMT</pubDate></item>" for key in keys) + "</channel></rss>").encode()


class ArticleStateTests(unittest.TestCase):
    def test_llm_empty_response_is_successful_and_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            extractor = EventExtractor()
            extractor.api_key = "test-only"
            client = Mock()
            client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"submarine_accidents": []}'))])
            with patch.object(extractor, "_client_instance", return_value=client):
                first = run_fixture(config, extractor)
                run_fixture(config, extractor)
            self.assertEqual(first.status, "success")
            self.assertEqual(first.candidates, 0)
            self.assertEqual(extractor.last_extraction_method, "llm")
            client.chat.completions.create.assert_called_once()
            self.assertTrue(ArticleState.from_config(config).is_processed(article()["url"]))

    def test_zero_candidate_cached_by_both_urls_but_rejections_wait_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            # A lightweight fake extractor need not expose the optional available property.
            extractor = SimpleNamespace(last_extraction_method="llm", extract=Mock(side_effect=[[], [{"cable_name": ""}]]), is_real_event=lambda event: False)
            summary = run_fixture(config, extractor, [article("zero"), article("rejected")])
            self.assertEqual(summary.rejected, 1)
            state = ArticleState.from_config(config)
            for prefix in ("https://news.google.com", "https://example.com"):
                self.assertTrue(state.is_processed(f"{prefix}/zero"))
                self.assertFalse(state.is_processed(f"{prefix}/rejected"))
            self.assertEqual(ArticleQueue.from_config(config).get(article("rejected"))["status"], "review")
            self.assertEqual(summary.queue["review"], 1)
            self.assertEqual(summary.runtime_status, "success")
            self.assertEqual(summary.review_status, "needs_review")
            extractor.extract.reset_mock()
            summary = run_fixture(config, extractor, [article("zero"), article("rejected")])
            extractor.extract.assert_not_called()
            records = [json.loads(line) for line in (Path(summary.audit_path) / "audit.jsonl").read_text().splitlines()]
            self.assertEqual(sum(r.get("reason") == "already_processed" for r in records), 1)
            self.assertEqual(sum(r.get("reason") == "awaiting_review" for r in records), 1)

    def test_fallback_and_extraction_errors_remain_retryable(self):
        for method, result in (("rules_fallback", []), ("llm", RuntimeError("temporary failure")), ("rules", [])):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as tmp:
                config = config_for(tmp)
                extract = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
                extractor = SimpleNamespace(last_extraction_method=method, extract=extract)
                run_fixture(config, extractor)
                self.assertFalse(ArticleState.from_config(config).is_processed(article()["url"]))
                extractor.last_extraction_method = "llm"
                extractor.extract = Mock(return_value=[])
                queue = ArticleQueue.from_config(config)
                self.assertEqual(queue.get(article())["status"], "retry")
                self.assertTrue(queue.requeue(article()))
                queue.persist()
                run_fixture(config, extractor)
                extractor.extract.assert_called_once()
                self.assertTrue(ArticleState.from_config(config).is_processed(article()["url"]))

    def test_store_failure_does_not_persist_pending_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            state = ArticleState.from_config(config)
            state.mark_processed("https://example.com/existing")
            state.save()
            before = state.path.read_bytes()
            extractor = SimpleNamespace(last_extraction_method="llm", extract=Mock(return_value=[]))
            store = EventStore(str(Path(tmp) / "events.jsonl"))
            with patch.object(store, "save", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    run_fixture(config, extractor, store=store)
            self.assertEqual(state.path.read_bytes(), before)
            run_fixture(config, extractor, store=store)
            self.assertEqual(extractor.extract.call_count, 2)

    def test_processing_failure_after_extraction_is_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            event = {"cable_name": "WACS", "occurrence_date": "2026-10-01", "affected_area": "South Africa", "original_text": "WACS cable fault affected South Africa.", "url": article()["url"]}
            extractor = SimpleNamespace(last_extraction_method="llm", extract=Mock(return_value=[event]), is_real_event=lambda event: True)
            store = EventStore(str(Path(tmp) / "events.jsonl"))
            with patch.object(store, "upsert", side_effect=RuntimeError("temporary store failure")):
                self.assertEqual(run_fixture(config, extractor, store=store).errors, 1)
            self.assertFalse(ArticleState.from_config(config).is_processed(article()["url"]))
            queue = ArticleQueue.from_config(config)
            self.assertTrue(queue.requeue(article()))
            queue.persist()
            self.assertEqual(run_fixture(config, extractor, store=store).inserted, 1)
            self.assertTrue(ArticleState.from_config(config).is_processed(article()["url"]))

    def test_dry_run_never_writes_processing_state_or_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            extractor = SimpleNamespace(last_extraction_method="llm", extract=Mock(return_value=[]))
            store = EventStore(str(Path(tmp) / "events.jsonl"))
            with patch.object(store, "save") as save:
                run_fixture(config, extractor, store=store, dry_run=True)
            save.assert_not_called()
            self.assertFalse(Path(config.article_state_path).exists())

    def test_expired_articles_are_refreshed_after_seven_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            state = ArticleState.from_config(config)
            state.processed_urls[article()["url"]] = datetime.now(timezone.utc) - timedelta(days=8)
            state.save()
            extractor = SimpleNamespace(last_extraction_method="llm", extract=Mock(return_value=[]))
            run_fixture(config, extractor)
            extractor.extract.assert_called_once()
            self.assertTrue(ArticleState.from_config(config).is_processed(article()["url"]))

    def test_state_retention_and_url_count_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ArticleState(str(Path(tmp) / "article-state.json"))
            now = datetime.now(timezone.utc)
            state.processed_urls = {f"https://example.com/{i}": now - timedelta(seconds=i) for i in range(2100)}
            state.processed_urls["https://example.com/old"] = now - timedelta(days=31)
            state.save()
            reloaded = ArticleState(str(state.path))
            self.assertEqual(len(reloaded.processed_urls), 2000)
            self.assertNotIn("https://example.com/old", reloaded.processed_urls)
            self.assertNotIn("https://example.com/2099", reloaded.processed_urls)

    def test_processed_rss_urls_do_not_consume_google_fetch_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp, google_news_keywords=["cable"], google_news_max_articles=1, google_news_max_attempts=1)
            scraper = GoogleNewsArticleScraper(config)
            scraper.article_state.mark_processed("https://news.google.com/cached")
            audit = AuditRun(config.audit_dir)
            audit.activate()
            with patch.object(scraper, "get", return_value=SimpleNamespace(content=feed("cached", "fresh"))), patch.object(scraper, "_fetch_news_article", return_value=article("fresh")) as fetch:
                articles = scraper.scrape_articles(since="2026-10-01")
            audit.finish({})
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args[0], "https://news.google.com/fresh")
            self.assertEqual(len(articles), 1)
            records = [json.loads(line) for line in (audit.path / "audit.jsonl").read_text().splitlines()]
            self.assertTrue(any(r.get("reason") == "already_processed" for r in records))
            self.assertEqual(next(r["attempts"] for r in records if r["stage"] == "collection"), 1)

    def test_new_rss_alias_for_processed_publisher_does_not_fetch_body_or_use_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp, google_news_keywords=["cable"], google_news_max_articles=1, google_news_max_attempts=1)
            scraper = GoogleNewsArticleScraper(config)
            scraper.article_state.mark_processed("https://example.com/cached")
            with patch.object(scraper, "get", return_value=SimpleNamespace(content=feed("new-alias", "fresh"))), patch.object(scraper, "_resolve_google_news_url", side_effect=["https://example.com/cached", "https://example.com/fresh"]), patch.object(scraper, "extract_page_text", return_value={"content": "Relevant content. " * 30, "final_url": "https://example.com/fresh"}) as body:
                articles = scraper.scrape_articles(since="2026-10-01")
            body.assert_called_once_with("https://example.com/fresh")
            self.assertEqual([row["url"] for row in articles], ["https://example.com/fresh"])


if __name__ == "__main__":
    unittest.main()
