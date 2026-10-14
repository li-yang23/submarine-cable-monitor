import json
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

from src.scrapers.article_sources import GoogleNewsArticleScraper, SubmarineNetworksScraper, SubTelForumScraper, extract_date_from_soup
from src.storage.article_queue import ArticleQueue, utcnow
from src.utils import Config
from src.utils.audit import AuditRun


def feed(*keys):
    return ("<rss><channel>" + "".join(f"<item><title>{key}</title><link>https://example.com/{key}</link><pubDate>Wed, 14 Oct 2026 00:00:00 GMT</pubDate></item>" for key in keys) + "</channel></rss>").encode()


def body(url, *args):
    return {"source": "Google News", "url": url, "title": url, "content": "A submarine cable outage affected services. " * 10, "published_date": "2026-10-14"}


class DurableQueueTests(unittest.TestCase):
    def test_metadata_only_done_survives_refresh_and_explicit_requeue(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ArticleQueue(str(Path(tmp) / "queue.json"))
            item = queue.enqueue({"source": "Google News", "url": "https://example.com/a", "content": "PRIVATE BODY", "initial_since": "2026-10-01"})
            queue.mark_done(item)
            queue.persist()
            reloaded = ArticleQueue(str(queue.path))
            reloaded.enqueue({**item, "initial_since": "2027-01-01"})
            self.assertEqual(reloaded.due(), [])
            self.assertEqual(reloaded.get(item)["initial_since"], "2026-10-01")
            self.assertNotIn("PRIVATE BODY", queue.path.read_text())
            self.assertTrue(reloaded.requeue(item))
            self.assertEqual(len(reloaded.due("Google News")), 1)

    def test_failure_backoff_never_expires_or_drops_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ArticleQueue(str(Path(tmp) / "queue.json"))
            item = queue.enqueue({"source": "Google News", "url": "https://example.com/blocked"})
            for _ in range(20):
                queue.mark_attempt(item)
                queue.mark_retry(item, "article_fetch_error", error_type="HTTPError", http_status=403)
            queue.persist()
            row = ArticleQueue(str(queue.path)).get(item)
            self.assertEqual((row["status"], row["attempts"], row["http_status"]), ("retry", 20, 403))
            self.assertEqual(queue.due(), [])
            queue.mark_retry(item, "article_fetch_error", delay_seconds=0)
            self.assertEqual(len(queue.due()), 1)
            self.assertEqual(queue.stats()["retry"], 1)

    def test_dry_run_never_changes_existing_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "queue.json")
            queue = ArticleQueue(path)
            item = queue.enqueue({"source": "Google News", "url": "https://example.com/a"})
            before = Path(path).read_bytes()
            dry = ArticleQueue(path, dry_run=True)
            dry.mark_done(item)
            dry.enqueue({"source": "Google News", "url": "https://example.com/b"})
            dry.set_cursor("Google News", "next")
            self.assertEqual(Path(path).read_bytes(), before)

    def test_corrupt_queue_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queue.json"
            path.write_text("not JSON")
            with self.assertRaises(ValueError):
                ArticleQueue(str(path))
            self.assertEqual(path.read_text(), "not JSON")


class DurableScraperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(article_queue_path=str(Path(self.tmp.name) / "queue.json"), article_state_path=str(Path(self.tmp.name) / "state.json"), google_news_keywords=["cable"], google_news_max_articles=1)

    def google_run(self, scraper, entries, **kwargs):
        with patch.object(scraper, "get", return_value=SimpleNamespace(content=feed(*entries))), patch.object(scraper, "_fetch_news_article", side_effect=body):
            return scraper.scrape_articles(**kwargs)

    def test_budget_remainder_survives_missing_rss_and_later_since(self):
        first = GoogleNewsArticleScraper(self.config)
        articles = self.google_run(first, ["a", "b"], since="2026-10-01")
        self.assertEqual(len(articles), 1)
        first.article_queue.mark_done(articles[0])
        first.article_queue.persist()
        second = GoogleNewsArticleScraper(self.config)
        remaining = self.google_run(second, [], since="2027-01-01")
        self.assertEqual([row["url"] for row in remaining], ["https://example.com/b"])

    def test_fetch_failure_persists_retry_and_can_be_reprocessed(self):
        scraper = GoogleNewsArticleScraper(self.config)
        with patch.object(scraper, "get", return_value=SimpleNamespace(content=feed("a"))), patch.object(scraper, "_fetch_news_article", side_effect=requests.Timeout("secret body")):
            self.assertEqual(scraper.scrape_articles(since="2026-10-01"), [])
        row = ArticleQueue(self.config.article_queue_path).get("https://example.com/a")
        self.assertEqual((row["status"], row["error_type"]), ("retry", "Timeout"))
        self.assertNotIn("secret body", Path(self.config.article_queue_path).read_text())
        scraper.article_queue.requeue(row)
        scraper.article_queue.persist()
        self.assertEqual(len(self.google_run(GoogleNewsArticleScraper(self.config), [], since="2027-01-01")), 1)

    def test_zero_limits_fetch_more_than_previous_twelve_articles(self):
        self.config.google_news_max_articles = 0
        self.config.google_news_max_attempts = 0
        rows = self.google_run(GoogleNewsArticleScraper(self.config), [str(i) for i in range(15)], since="2026-10-01")
        self.assertEqual(len(rows), 15)

    def test_unavailable_model_and_expired_budget_only_defer(self):
        scraper = GoogleNewsArticleScraper(self.config)
        scraper.fetch_enabled = False
        self.assertEqual(self.google_run(scraper, ["a"], since="2026-10-01"), [])
        self.assertEqual(scraper.article_queue.counts()["pending"], 1)
        scraper.fetch_enabled = True
        scraper.deadline = time.monotonic() - 1
        self.assertEqual(self.google_run(scraper, [], since="2027-01-01"), [])
        self.assertEqual(scraper.article_queue.counts()["pending"], 1)

    def test_dry_scraper_does_not_create_queue(self):
        scraper = GoogleNewsArticleScraper(self.config)
        self.google_run(scraper, ["a"], since="2026-10-01", dry_run=True)
        self.assertFalse(Path(self.config.article_queue_path).exists())

    def test_iterator_fetches_one_at_a_time_without_rediscovery(self):
        scraper = GoogleNewsArticleScraper(self.config)
        self.config.google_news_max_articles = 0
        for key in ["a", "b"]:
            scraper.article_queue.enqueue({"source": scraper.source_name, "url": f"https://example.com/{key}"})
        with patch.object(scraper, "get", side_effect=AssertionError("must not rediscover")), patch.object(scraper, "_fetch_news_article", side_effect=body) as fetch:
            iterator = scraper.iter_queued_articles()
            first = next(iterator)
            self.assertEqual(fetch.call_count, 1)
            scraper.article_queue.mark_done(first)
            scraper.article_queue.persist()
            self.assertEqual(len(list(iterator)), 1)
        self.assertEqual(fetch.call_count, 2)

    def test_google_does_not_discard_subtel_publisher_urls(self):
        scraper = GoogleNewsArticleScraper(self.config)
        with patch.object(scraper, "_resolve_google_news_url", return_value="https://subtelforum.com/news/"), patch.object(scraper, "extract_page_text", return_value=body("https://subtelforum.com/news/")):
            self.assertIsNotNone(scraper._fetch_news_article("https://news.google.com/a", "Cable outage", "2026-10-14", "cable"))

    def test_explicit_requeue_bypasses_cache_and_completed_alias(self):
        scraper = GoogleNewsArticleScraper(self.config)
        url = "https://example.com/a"
        first = scraper.article_queue.enqueue({"source": "SubTel Forum", "url": url})
        scraper.article_queue.mark_done(first)
        manual = scraper.article_queue.enqueue({"source": scraper.source_name, "url": url})
        scraper.article_queue.mark_review(manual, "needs_revision")
        scraper.article_queue.requeue(manual)
        scraper.article_queue.persist()
        scraper.article_state.mark_processed(url)
        with patch.object(scraper, "_resolve_google_news_url", return_value=url), patch.object(scraper, "extract_page_text", return_value=body(url)) as fetch:
            rows = list(scraper.iter_queued_articles())
        fetch.assert_called_once_with(url)
        self.assertEqual([row["queue_id"] for row in rows], [manual["queue_id"]])

    def test_unvisited_source_backlog_keeps_first_collection_scope_after_downtime(self):
        scraper = SubTelForumScraper(self.config)
        initial = (utcnow().date() - timedelta(days=60)).isoformat()
        scraper.article_queue.set_cursor(f"coverage_since:{scraper.source_name}", initial)
        scraper._prepare_queue(False)
        row = scraper._enqueue({"url": "https://subtelforum.com/late-discovery/"})
        self.assertEqual(row["initial_since"], initial)

    def test_google_processes_backlog_even_when_all_feeds_fail(self):
        scraper = GoogleNewsArticleScraper(self.config)
        scraper.article_queue.enqueue({"source": scraper.source_name, "url": "https://example.com/backlog"})
        with patch.object(scraper, "get", side_effect=requests.Timeout()), patch.object(scraper, "_fetch_news_article", side_effect=body):
            self.assertEqual(len(scraper.scrape_articles(since="2027-01-01")), 1)

    def test_truncated_feed_is_visible_and_does_not_advance_discovery_watermark(self):
        scraper = GoogleNewsArticleScraper(self.config)
        scraper.fetch_enabled = False
        audit = AuditRun(str(Path(self.tmp.name) / "runs"))
        audit.activate()
        self.google_run(scraper, [str(i) for i in range(100)], since="2026-10-01")
        audit.finish({})
        records = [json.loads(line) for line in (audit.path / "audit.jsonl").read_text().splitlines()]
        self.assertTrue(any(row.get("reason") == "possible_feed_truncation" for row in records))
        self.assertIsNone(scraper.article_queue.get_cursor("google_query:cable"))
        self.assertEqual(len(scraper.article_queue.items), 100)

    def test_automatic_discovery_covers_long_downtime(self):
        scraper = GoogleNewsArticleScraper(self.config)
        previous = (utcnow().date() - timedelta(days=60)).isoformat()
        scraper.article_queue.set_cursor("google_query:cable", previous)
        with patch.object(scraper, "get", return_value=SimpleNamespace(content=feed())) as get:
            scraper.scrape_articles()
        self.assertIn(f"after:{previous}", unquote(get.call_args.args[0]))

    def test_other_sources_keep_backlog_and_initial_date_range(self):
        for cls in [SubTelForumScraper, SubmarineNetworksScraper]:
            with self.subTest(source=cls.source_name):
                scraper = cls(self.config)
                item = scraper.article_queue.enqueue({"source": cls.source_name, "url": f"https://example.com/{cls.__name__}", "initial_since": "2026-10-01"})
                discovery = "_discover_urls" if cls is SubTelForumScraper else "_load_cable_links"
                with patch.object(scraper, discovery, return_value=[]), patch.object(scraper, "_fetch_article", side_effect=body):
                    rows = scraper.scrape_articles(since="2027-01-01")
                self.assertEqual([row["queue_id"] for row in rows], [item["queue_id"]])
                scraper.article_queue.mark_done(item)
                scraper.article_queue.persist()

    def test_initial_old_archive_is_skipped_before_llm_but_remains_recorded(self):
        scraper = SubTelForumScraper(self.config)
        item = scraper.article_queue.enqueue({"source": scraper.source_name, "url": "https://example.com/old", "initial_since": "2026-10-01"})
        with patch.object(scraper, "_discover_urls", return_value=[]), patch.object(scraper, "_fetch_article", return_value={**body("https://example.com/old"), "published_date": "2020-01-01"}):
            self.assertEqual(scraper.scrape_articles(since="2026-10-01"), [])
        self.assertEqual(scraper.article_queue.get(item)["reason"], "outside_initial_window")

    def test_explicit_requeue_can_reprocess_outside_initial_window(self):
        scraper = SubTelForumScraper(self.config)
        item = scraper.article_queue.enqueue({"source": scraper.source_name, "url": "https://example.com/old", "initial_since": "2026-10-01"})
        scraper.article_queue.mark_done(item, "outside_initial_window")
        scraper.article_queue.requeue(item)
        with patch.object(scraper, "_fetch_article", return_value={**body("https://example.com/old"), "published_date": "2020-01-01"}):
            self.assertEqual(len(list(scraper.iter_queued_articles())), 1)
        self.assertEqual(scraper.article_queue.get(item)["status"], "pending")

    def test_old_incident_date_in_prose_cannot_drop_new_report(self):
        scraper = SubTelForumScraper(self.config)
        item = scraper.article_queue.enqueue({"source": scraper.source_name, "url": "https://example.com/new-report", "initial_since": "2026-10-01"})
        html = "<article><h1>New cable investigation findings</h1><p>New evidence emerged about the cable cut on March 1, 2025.</p></article>"
        with patch.object(scraper, "get", return_value=SimpleNamespace(text=html)):
            rows = list(scraper.iter_queued_articles())
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["published_date"])
        self.assertEqual(scraper.article_queue.get(item)["status"], "pending")

    def test_publication_metadata_wins_over_event_time_element(self):
        event_time = "<article>Incident on <time datetime='2020-01-01'>January 1, 2020</time></article>"
        self.assertIsNone(extract_date_from_soup(BeautifulSoup(event_time, "html.parser")))
        html = event_time + "<meta property='article:published_time' content='2026-10-14'>"
        self.assertEqual(extract_date_from_soup(BeautifulSoup(html, "html.parser")), "2026-10-14")

    def test_scope_exclusion_cannot_suppress_explicit_other_source_article(self):
        scraper = GoogleNewsArticleScraper(self.config)
        url = "https://example.com/old"
        excluded = scraper.article_queue.enqueue({"source": "SubTel Forum", "url": url, "initial_since": "2026-10-01"})
        scraper.article_queue.mark_done(excluded, "outside_initial_window")
        explicit = scraper.article_queue.enqueue({"source": scraper.source_name, "url": url})
        with patch.object(scraper, "_fetch_news_article", side_effect=body) as fetch:
            rows = list(scraper.iter_queued_articles())
        fetch.assert_called_once()
        self.assertEqual([row["queue_id"] for row in rows], [explicit["queue_id"]])

    def test_networks_rotates_seed_discovery_and_enqueues_over_body_budget(self):
        self.config.submarine_networks_max_cables = 1
        self.config.submarine_networks_articles_per_cable = 1
        scraper = SubmarineNetworksScraper(self.config)
        scraper.fetch_enabled = False
        links = [{"href": f"https://example.com/cable{i}", "cable_name": str(i)} for i in range(3)]
        with patch.object(scraper, "_load_cable_links", return_value=links), patch.object(scraper, "_discover_article_urls", side_effect=lambda url: [url + "/a", url + "/b"]) as discover:
            scraper.scrape_articles(since="2026-10-01")
            scraper.scrape_articles(since="2026-10-01")
        self.assertEqual([call.args[0] for call in discover.call_args_list], [links[0]["href"], links[1]["href"]])
        self.assertEqual(scraper.article_queue.counts()["pending"], 4)

    def test_subtel_paginated_discovery_resumes_beyond_front_page(self):
        self.config.subtelforum_max_pages = 1
        scraper = SubTelForumScraper(self.config)
        scraper.fetch_enabled = False
        pages = {
            scraper.start_url: "<h2><a href='https://subtelforum.com/a/'>A</a></h2><a rel='next' href='/page/2/'>next</a>",
            "https://subtelforum.com/page/2/": "<h2><a href='https://subtelforum.com/b/'>B</a></h2><a rel='next' href='/page/3/'>next</a>",
        }
        with patch.object(scraper, "get", side_effect=lambda url: SimpleNamespace(text=pages[url])):
            scraper.scrape_articles(since="2026-10-01")
            scraper.scrape_articles(since="2026-10-01")
        self.assertEqual(scraper.article_queue.counts()["pending"], 2)
        self.assertEqual(scraper.article_queue.get_cursor(scraper.source_name), "https://subtelforum.com/page/3/")

    def test_subtel_does_not_jump_over_new_second_page_to_old_cursor(self):
        scraper = SubTelForumScraper(self.config)
        scraper.fetch_enabled = False
        scraper.article_queue.set_cursor(scraper.source_name, "https://subtelforum.com/page/50/")
        scraper.article_queue.enqueue({"source": scraper.source_name, "url": "https://subtelforum.com/known/"})
        pages = {
            scraper.start_url: "<h2><a href='https://subtelforum.com/new1/'>new1</a></h2><a rel='next' href='/page/2/'>next</a>",
            "https://subtelforum.com/page/2/": "<h2><a href='https://subtelforum.com/new2/'>new2</a></h2><h2><a href='https://subtelforum.com/known/'>known</a></h2><a rel='next' href='/page/3/'>next</a>",
            "https://subtelforum.com/page/50/": "<h2><a href='https://subtelforum.com/old/'>old</a></h2>",
        }
        with patch.object(scraper, "get", side_effect=lambda url: SimpleNamespace(text=pages[url])) as get:
            scraper.scrape_articles(since="2026-10-01")
        self.assertEqual([call.args[0] for call in get.call_args_list], [scraper.start_url, "https://subtelforum.com/page/2/", "https://subtelforum.com/page/50/"])
        self.assertIsNotNone(scraper.article_queue.get("https://subtelforum.com/new2/"))


if __name__ == "__main__":
    unittest.main()
