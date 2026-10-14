import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from bs4 import BeautifulSoup

from src.pipeline import MonitorPipeline
from src.processing.extractor import EventExtractor
from src.scrapers.article_sources import GoogleNewsArticleScraper, article_body_text, parse_rss_entries
from src.storage import EventStore
from src.storage.event_store import should_drop_event
from src.utils import Config
from src.utils.audit import AuditRun, article_fields, search_audit


def rss(*items):
    from xml.sax.saxutils import escape
    return ("<rss version='2.0'><channel><title>News</title>" + "".join(
        f"<item><title>{escape(title)}</title><link>https://example.com/{key}</link><pubDate>{date}</pubDate></item>"
        for key, title, date in items
    ) + "</channel></rss>").encode()


def read_records(audit):
    return [json.loads(line) for line in (audit.path / "audit.jsonl").read_text().splitlines()]


class DiscoveryAuditTests(unittest.TestCase):
    def test_later_queries_are_discovered_and_fetched_with_fair_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(google_news_keywords=["first", "last"], google_news_max_attempts=2, google_news_max_articles=2)
            scraper = GoogleNewsArticleScraper(config)
            feeds = [rss(("old", "Old", "Thu, 01 Oct 2026 01:00:00 GMT"), ("new", "New", "Fri, 02 Oct 2026 01:00:00 GMT")), rss(("globe", "Globe outage", "Sat, 03 Oct 2026 01:00:00 GMT"))]
            audit = AuditRun(tmp)
            audit.activate()
            def fetch(url, title, published, keyword):
                return {"url": url, "discovered_url": url, "title": title, "content": "text", "keyword": keyword}
            with patch.object(scraper, "get", side_effect=[SimpleNamespace(content=feed) for feed in feeds]) as get, patch.object(scraper, "_fetch_news_article", side_effect=fetch):
                articles = scraper.scrape_articles(since="2026-10-01")
            audit.finish({"status": "success"})
            self.assertEqual(get.call_count, 2)
            self.assertEqual([a["title"] for a in articles], ["New", "Globe outage"])
            records = read_records(audit)
            self.assertEqual(len([r for r in records if r["stage"] == "discover"]), 3)
            self.assertTrue(any(r.get("reason") == "article_budget" and r["title"] == "Old" for r in records))

    def test_failed_feed_does_not_abort_later_query_and_preserves_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            scraper = GoogleNewsArticleScraper(Config(google_news_keywords=["bad", "good"]))
            audit = AuditRun(tmp)
            audit.activate()
            with patch.object(scraper, "get", side_effect=[requests.Timeout("SECRET body"), SimpleNamespace(content=rss(("a", "Cable outage", "Thu, 01 Oct 2026 01:00:00 GMT")))]), patch.object(scraper, "_fetch_news_article", return_value=None):
                scraper.scrape_articles(since="2026-10-01")
            audit.finish({"status": "degraded"})
            records = read_records(audit)
            self.assertTrue(any(r["stage"] == "rss" and r["outcome"] == "failed" for r in records))
            self.assertTrue(any(r.get("query") == "good" and r["stage"] == "discover" for r in records))
            self.assertNotIn("SECRET", (audit.path / "audit.jsonl").read_text())

    def test_duplicates_and_old_news_do_not_consume_fetch_attempts(self):
        scraper = GoogleNewsArticleScraper(Config(google_news_keywords=["a", "b"], google_news_max_attempts=2))
        feeds = [rss(("old", "Old", "Wed, 01 Oct 2025 01:00:00 GMT"), ("same", "Same", "Thu, 01 Oct 2026 01:00:00 GMT")), rss(("same", "Same", "Thu, 01 Oct 2026 01:00:00 GMT"), ("later", "Later", "Thu, 01 Oct 2026 01:00:00 GMT"))]
        with patch.object(scraper, "get", side_effect=[SimpleNamespace(content=f) for f in feeds]), patch.object(scraper, "_fetch_news_article", return_value=None) as fetch:
            scraper.scrape_articles(since="2026-10-01")
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ["https://example.com/same", "https://example.com/later"])

    def test_invalid_rss_is_a_parse_error(self):
        with self.assertRaises(ValueError):
            parse_rss_entries(b"<html><body>Access denied</body></html>")

    def test_decode_and_short_body_have_distinct_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            scraper = GoogleNewsArticleScraper(Config())
            audit = AuditRun(tmp)
            audit.activate()
            with patch.object(scraper, "_resolve_google_news_url", return_value=""):
                self.assertIsNone(scraper._fetch_news_article("https://news.google.com/a", "Missing", "2026-10-01", "a"))
            with patch.object(scraper, "_resolve_google_news_url", return_value="https://example.com/a"), patch.object(scraper, "extract_page_text", return_value={"content": "Short"}):
                self.assertIsNone(scraper._fetch_news_article("https://news.google.com/b", "Short", "2026-10-01", "a"))
            audit.finish({})
            reasons = {r.get("reason") for r in read_records(audit)}
            self.assertTrue({"google_news_decode_failed", "short_content"} <= reasons)


class PipelineAuditTests(unittest.TestCase):
    def test_rejections_fallback_and_merge_are_traceable_without_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(audit_dir=str(Path(tmp) / "runs"), article_state_path=str(Path(tmp) / "article-state.json"))
            extractor = EventExtractor()
            extractor.api_key = "not-a-real-key"
            articles = [
                {"source": "fixture", "title": "WACS cable fault", "url": "https://example.com/good", "content": "WACS cable fault affected South Africa.", "published_date": "2026-10-01"},
                {"source": "fixture", "title": "Internet outage", "url": "https://example.com/rejected", "content": "Internet outage in the Philippines.", "published_date": "2026-10-01"},
            ]
            store = EventStore(str(Path(tmp) / "events.jsonl"))
            pipeline = MonitorPipeline(config, store, extractor)
            fake_scraper = SimpleNamespace(scrape_articles=lambda **kwargs: articles + [articles[0]])
            with patch("src.pipeline.SCRAPER_CLASSES", {"fixture": lambda config: fake_scraper}), patch.object(extractor, "_extract_with_llm", side_effect=RuntimeError("SECRET LLM response")):
                summary = pipeline.run(["fixture"])
            records = [json.loads(line) for line in (Path(summary.audit_path) / "audit.jsonl").read_text().splitlines()]
            self.assertEqual(summary.status, "degraded")
            self.assertEqual((summary.inserted, summary.merged, summary.rejected), (1, 1, 1))
            self.assertTrue(any(r.get("method") == "rules_fallback" for r in records))
            self.assertTrue(any(r.get("reason") == "unnamed_missing_date" for r in records))
            self.assertTrue(any(r["outcome"] == "merged" and r.get("event_uid") for r in records))
            self.assertEqual(store.events[0]["verification_status"], "rule_verified")
            text = (Path(summary.audit_path) / "audit.jsonl").read_text()
            self.assertNotIn("SECRET", text)
            self.assertNotIn('"content":', text)
            self.assertTrue((Path(tmp) / "runs/latest-summary.md").exists())

    def test_source_failure_still_finalizes_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = MonitorPipeline(Config(audit_dir=tmp, article_state_path=str(Path(tmp) / "article-state.json")), EventStore(str(Path(tmp) / "events.jsonl")))
            with patch("src.pipeline.SCRAPER_CLASSES", {"bad": lambda config: (_ for _ in ()).throw(RuntimeError("secret"))}):
                summary = pipeline.run(["bad"])
            self.assertEqual(summary.status, "failed")
            self.assertEqual(summary.errors, 1)
            self.assertTrue((Path(summary.audit_path) / "summary.json").exists())

    def test_search_expands_url_match_to_all_stages_of_article(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditRun(tmp)
            fields = article_fields({"discovered_url": "https://news.google.com/a", "title": "A"})
            audit.emit("discover", "found", **fields)
            audit.emit("resolve", "completed", **fields, url="https://example.com/globe")
            audit.emit("store", "inserted", **fields, event_uid="evt_example")
            audit.finish({})
            self.assertEqual(len(list(search_audit(tmp, "example.com/globe"))), 3)

    def test_audit_redacts_credentials_and_omits_unapproved_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditRun(tmp)
            audit.emit("fetch", "failed", url="https://user:password@example.com/a?token=verysecret&topic=cable", content="private body", api_key="private key")
            audit.finish({})
            text = (audit.path / "audit.jsonl").read_text()
            self.assertNotIn("verysecret", text)
            self.assertNotIn("password", text)
            self.assertNotIn("private", text)
            self.assertIn("topic=cable", text)


class UnnamedIncidentTests(unittest.TestCase):
    def setUp(self):
        self.extractor = EventExtractor()
        self.extractor.api_key = None

    def test_globe_incident_is_kept_without_inventing_cable_name(self):
        article = {"source": "Google News", "url": "https://example.com/globe", "title": "Globe internet disrupted in Philippines", "published_date": "2026-10-04", "content": "An undersea cable disruption caused about 300Gbps of capacity loss in the Philippines."}
        event = self.extractor._decorate_event({"cable_name": "", "occurrence_date": "2026-10-03", "affected_area": "Philippines", "original_text": article["content"]}, article)
        self.assertEqual(event["cable_name"], "")
        self.assertEqual(event["occurrence_date"], "2026-10-03")
        self.assertTrue(self.extractor.is_real_event(event))
        self.assertFalse(should_drop_event(event))

    def test_unknown_or_incomplete_incident_dates_never_use_publication_date(self):
        article = {"published_date": "2026-10-04", "content": "WACS cable fault affected South Africa."}
        for value in (None, "", "unknown", "June 2026", "Jun 2026", "2026-06", "2026"):
            with self.subTest(value=value):
                event = self.extractor._decorate_event({"occurrence_date": value, "repair_date": value}, article)
                self.assertIsNone(event["occurrence_date"])
                self.assertIsNone(event["repair_date"])
                self.assertEqual(event["published_date"], "2026-10-04")
        rule_event = self.extractor.extract(article)[0]
        self.assertIsNone(rule_event["occurrence_date"])
        self.assertIsNone(rule_event["repair_date"])

    def test_explicit_incident_and_repair_dates_are_preserved(self):
        event = self.extractor._decorate_event(
            {"occurrence_date": "2026-09-04", "repair_date": "September 12, 2026"},
            {"published_date": "2026-10-04"},
        )
        self.assertEqual(event["occurrence_date"], "2026-09-04")
        self.assertEqual(event["repair_date"], "2026-09-12")

    def test_general_outage_and_unsubstantiated_incident_are_rejected(self):
        base = {"title": "Internet outage", "original_text": "Internet outage in Philippines after a router failure.", "affected_area": "Philippines", "occurrence_date": "2026-10-04"}
        self.assertFalse(self.extractor.is_real_event(base))
        self.assertTrue(should_drop_event(base))
        for missing in ("occurrence_date", "affected_area"):
            candidate = {**base, "original_text": "A submarine cable outage caused internet problems in Philippines.", missing: ""}
            self.assertFalse(self.extractor.is_real_event(candidate))
            self.assertTrue(should_drop_event(candidate))
        hypothetical = {**base, "original_text": "A submarine cable outage could affect internet connectivity."}
        self.assertFalse(self.extractor.is_real_event(hypothetical))

    def test_unknown_cables_from_different_reports_are_not_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(str(Path(tmp) / "events.jsonl"))
            base = {"title": "Undersea cable outage", "original_text": "An undersea cable outage affected Philippines.", "affected_area": "Philippines", "occurrence_date": "2026-10-04", "source": "fixture"}
            self.assertTrue(store.upsert({**base, "url": "https://example.com/a"})[0])
            self.assertTrue(store.upsert({**base, "url": "https://example.com/b"})[0])
            self.assertFalse(store.upsert({**base, "url": "https://example.com/a"})[0])
            self.assertTrue(store.upsert({**base, "url": "https://example.com/a", "affected_area": "Hong Kong", "original_text": "Another undersea cable outage affected Hong Kong."})[0])
            self.assertEqual(len(store.events), 3)

    def test_article_body_excludes_wacs_recommendation_and_recognizes_c2c(self):
        soup = BeautifulSoup("""<article><h1>Philippines-Singapore C2C Link Restored</h1><div class='post-content'><p>A C2C cable fault affected Philippines. Repairs restored the connection.</p><div class='related-posts'>New WACS outage details: WACS cable fault affected South Africa.</div>Share This Story, Choose Your Platform! you might also like WACS cable fault.</div><aside>WACS cable damage</aside></article>""", "html.parser")
        content = article_body_text(soup)
        self.assertNotIn("WACS", content)
        event = self.extractor.extract({"title": "Philippines-Singapore C2C Link Restored", "content": content, "published_date": "2026-10-05"})[0]
        self.assertEqual(event["cable_name"], "C2C")
        self.assertEqual(event["affected_area"], "Philippines")

    def test_c2c_restoration_uses_actual_body_without_recommendation_fault_words(self):
        content = "InfiniVAN said the Philippines-Singapore segment of the C2C submarine cable was restored and under monitoring. The announcement followed the previous day's disruption."
        event = self.extractor.extract({"title": "Philippines-Singapore C2C Link Restored", "content": content, "published_date": "2026-10-05"})[0]
        self.assertEqual(event["cable_name"], "C2C")
        self.assertIn("InfiniVAN", event["original_text"])
        self.assertTrue(self.extractor.is_real_event(event))

    def test_subtelforum_embedded_recommendation_sections_are_excluded(self):
        soup = BeautifulSoup("""<article><div class='post-content'>
            <p>The SEA-ME-WE 5 cable fault was repaired and Pakistan services restored.</p>
            <section aria-labelledby='stf-related-cable-systems-title' class='stf-related-content stf-related-cable-systems' data-stf-related-cable-systems='1'>
                <h2 id='stf-related-cable-systems-title'>Related cable systems</h2>
                <article><a>Interactive cable map WACS Submarine Cable System</a></article>
            </section>
            <section aria-labelledby='stf-related-magazine-articles-title' class='stf-related-content stf-related-magazine-articles'>
                <h2 id='stf-related-magazine-articles-title'>Related magazine articles</h2>
                <article><h3>Complex Challenges: Solutions Made Simple</h3><p>SubTel Forum Magazine #98</p>
                <div class='stf-related-content__topics'><span>Topics</span><a>Africa Connectivity</a></div></article>
            </section>
            <p>PTA continued monitoring connectivity after the restoration.</p>
            </div></article>""", "html.parser")
        content = article_body_text(soup)
        for recommendation in ("Related cable systems", "WACS", "Related magazine articles", "Complex Challenges", "Topics", "Africa Connectivity"):
            self.assertNotIn(recommendation, content)
        self.assertIn("SEA-ME-WE 5 cable fault", content)
        self.assertIn("PTA continued monitoring", content)

    def test_related_cable_and_magazine_phrases_in_article_prose_are_preserved(self):
        soup = BeautifulSoup("""<article><p>Related cable systems also carried rerouted traffic.</p>
            <p>The report cited related magazine articles about repair delays.</p>
            <p>Topics covered by the operator included outage duration and restoration.</p></article>""", "html.parser")
        content = article_body_text(soup)
        self.assertIn("Related cable systems also carried rerouted traffic.", content)
        self.assertIn("related magazine articles about repair delays.", content)
        self.assertIn("Topics covered by the operator", content)

    def test_vocus_confirmed_fault_on_full_cable_name_is_accepted(self):
        event = {
            "cable_name": "Australia-Singapore Cable",
            "title": "Vocus ASC Fault Disrupts Australia-Singapore Traffic",
            "occurrence_date": "2026-09-04",
            "affected_area": "Australia, Singapore, Christmas Island, Indonesia",
            "original_text": "Vocus has confirmed a fault on its Australia-Singapore Cable, disrupting international internet and transmission services between Australia and Singapore and requiring affected traffic to be moved onto alternate routes.",
        }
        self.assertTrue(self.extractor.is_real_event(event))
        self.assertFalse(should_drop_event(event))
        testing_only = {**event, "original_text": "Emergency cable testing from Perth temporarily affected services on Christmas Island during the testing window."}
        self.assertFalse(self.extractor.is_real_event(testing_only))

    def test_named_cable_semantics_reject_hypothetical_policy_and_drill(self):
        base = {"cable_name": "Australia-Singapore Cable", "occurrence_date": "2026-09-04", "affected_area": "Australia"}
        for evidence in (
            "Vocus could confirm a fault on its Australia-Singapore Cable, disrupting international internet services.",
            "A policy review discussed a fault on the Australia-Singapore Cable and international internet resilience.",
            "An exercise simulated a fault on the Australia-Singapore Cable, disrupting internet services.",
            "A fault on a Vocus router disrupted international internet services.",
        ):
            with self.subTest(evidence=evidence):
                self.assertFalse(self.extractor.is_real_event({**base, "original_text": evidence}))

    def test_fitburg_seabed_cutting_telecom_cables_is_explicit_incident(self):
        event = {
            "cable_name": "",
            "title": "Fitburg Trial Opens Over Baltic Cable Damage",
            "occurrence_date": "2025-12-31",
            "affected_area": "Finland and Estonia",
            "original_text": "Prosecutors allege the vessel dragged a damaged anchor for at least 130 kilometers across the Gulf of Finland seabed, cutting two telecom cables and attempting to damage eight additional subsea connections.",
        }
        self.assertTrue(self.extractor.is_real_event(event))
        self.assertFalse(should_drop_event(event))
        alternate = {**event, "original_text": "The vessel damaged two subsea telecommunications cables linking Finland and Estonia."}
        self.assertTrue(self.extractor.is_real_event(alternate))
        for evidence in (
            "An exercise simulated an anchor dragging across the seabed, cutting two telecom cables.",
            "An anchor could drag across the seabed, cutting two telecom cables.",
            "A policy review discussed damage to two subsea telecommunications cables.",
            "A vessel crossed the seabed while a fault disrupted a land-based telecom router.",
        ):
            with self.subTest(evidence=evidence):
                self.assertFalse(self.extractor.is_real_event({**event, "original_text": evidence}))


if __name__ == "__main__":
    unittest.main()
