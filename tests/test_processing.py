import csv
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from src.processing import event_uid, normalize_accident_type, parse_date, text_similarity
from src.scrapers.article_sources import extract_date_from_soup
from src.storage import EventStore


class ProcessingTests(unittest.TestCase):
    def test_parse_date_handles_iso_english_and_ranges(self):
        self.assertEqual(parse_date("2025-06-01"), "2025-06-01")
        self.assertEqual(parse_date("June 16, 2025"), "2025-06-16")
        self.assertEqual(parse_date("Between June 23-29, 2023"), "2023-06-29")
        self.assertIsNone(parse_date("Not specified"))

    def test_accident_type_normalization(self):
        self.assertEqual(normalize_accident_type("", "submarine cable cut near Yemen"), "Cut")
        self.assertEqual(normalize_accident_type("", "planned maintenance activity"), "Maintenance")
        self.assertEqual(normalize_accident_type("", "internet speed degraded"), "Fluctuate")
        self.assertEqual(normalize_accident_type("", "service outage reported"), "Limit")

    def test_event_uid_is_stable(self):
        event = {
            "url": "https://example.com/a",
            "cable_name": "WACS",
            "occurrence_date": "2025-06-01",
            "accident_location": "Namibia",
        }
        self.assertEqual(event_uid(event), event_uid(dict(event)))

    def test_text_similarity_detects_overlap(self):
        left = "WACS cable fault slows South Africa internet after branching unit issue"
        right = "South Africa internet slowed because WACS branching unit cable fault"
        self.assertGreater(text_similarity(left, right), 0.35)


class EventStoreTests(unittest.TestCase):
    def test_import_history_and_merge_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "events.csv"
            store_path = Path(tmp) / "events.jsonl"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "cable_name",
                        "accident_location",
                        "reason",
                        "accident_type",
                        "affected_area",
                        "occurrence_date",
                        "url",
                        "original_text",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "cable_name": "WACS",
                        "accident_location": "Namibia",
                        "reason": "Cable fault",
                        "accident_type": "Cut",
                        "affected_area": "Namibia",
                        "occurrence_date": "2025-06-01",
                        "url": "https://example.com/wacs",
                        "original_text": "WACS cable fault in Namibia.",
                    }
                )

            store = EventStore(str(store_path))
            self.assertEqual(store.import_history_csv(str(csv_path)), 1)
            inserted, event = store.upsert(
                {
                    "source": "SubTel Forum",
                    "cable_name": "WACS",
                    "accident_location": "Namibia",
                    "reason": "Cable fault",
                    "accident_type": "Cut",
                    "affected_area": "Namibia",
                    "occurrence_date": "2025-06-01",
                    "url": "https://example.com/wacs",
                    "original_text": "WACS cable fault in Namibia.",
                }
            )
            self.assertFalse(inserted)
            self.assertIn("SubTel Forum", event["sources"])
            self.assertEqual(len(store.events), 1)

    def test_clean_events_drops_low_value_google_rss_and_merges_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(str(Path(tmp) / "events.jsonl"))
            store.events = [
                {
                    "source": "Google News",
                    "url": "https://news.google.com/rss/articles/bad",
                    "title": "Unresolved RSS item",
                    "original_text": "Submarine cable fault reported.",
                },
                {
                    "source": "SubTel Forum",
                    "url": "https://example.com/no-core",
                    "title": "Cable damage mentioned",
                    "original_text": "Damage was mentioned without cable, area, or reason.",
                },
                {
                    "source": "Google News",
                    "url": "https://www.dawn.com/news/2012491",
                    "title": "SMW5 submarine cable fault",
                    "cable_name": "SMW5",
                    "occurrence_date": "2026-07-02",
                    "affected_area": "Pakistan",
                    "original_text": "SMW5 submarine cable fault may disrupt Pakistan internet.",
                },
                {
                    "source": "SubTel Forum",
                    "url": "https://subtelforum.com/smw5-fault",
                    "title": "SEA-ME-WE 5 fault affects Pakistan",
                    "cable_name": "SEA-ME-WE 5",
                    "occurrence_date": "2026-07-02",
                    "reason": "Cable fault",
                    "original_text": "SEA-ME-WE 5 cable fault may disrupt internet services in Pakistan.",
                },
            ]

            summary = store.clean_events()

            self.assertEqual(summary["dropped"], 2)
            self.assertEqual(summary["merged"], 1)
            self.assertEqual(len(store.events), 1)
            self.assertEqual(store.events[0]["normalized_cable_id"], "SEA-ME-WE 5")
            self.assertIn("SubTel Forum", store.events[0]["sources"])

    def test_clean_events_merges_near_date_resolution_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(str(Path(tmp) / "events.jsonl"))
            store.events = [
                {
                    "source": "Google News",
                    "url": "https://www.dawn.com/news/2012491",
                    "title": "Submarine cable fault may cause intermittent internet disruption",
                    "cable_name": "SMW5",
                    "occurrence_date": "2026-07-02",
                    "affected_area": "Pakistan",
                    "original_text": "SEA-ME-WE 5 submarine cable fault may cause intermittent internet disruption in Pakistan.",
                },
                {
                    "source": "Google News",
                    "url": "https://www.dawn.com/news/2012653",
                    "title": "PTA says submarine cable fault resolved, internet services back to normal",
                    "cable_name": "SEA-ME-WE 5",
                    "occurrence_date": "2026-07-03",
                    "affected_area": "Pakistan",
                    "original_text": "SEA-ME-WE 5 submarine cable fault resolved and internet services returned to normal in Pakistan.",
                },
            ]

            summary = store.clean_events()

            self.assertEqual(summary["merged"], 1)
            self.assertEqual(len(store.events), 1)

    def test_clean_events_drops_events_without_area_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(str(Path(tmp) / "events.jsonl"))
            store.events = [
                {
                    "source": "SubTel Forum",
                    "url": "https://example.com/weak",
                    "title": "Cable item without useful fields",
                    "cable_name": "WACS",
                    "occurrence_date": "2026-01-01",
                    "original_text": "WACS was mentioned in a cable article.",
                },
                {
                    "source": "SubTel Forum",
                    "url": "https://example.com/strong",
                    "title": "WACS outage affects users",
                    "cable_name": "WACS",
                    "occurrence_date": "2026-01-02",
                    "affected_area": "South Africa",
                    "original_text": "WACS outage affected South Africa.",
                },
            ]

            summary = store.clean_events()

            self.assertEqual(summary["dropped"], 1)
            self.assertEqual(len(store.events), 1)
            self.assertEqual(store.events[0]["url"], "https://example.com/strong")

    def test_same_history_url_can_contain_multiple_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(str(Path(tmp) / "events.jsonl"))
            url = "./data/articles/aag-history.txt"
            first_inserted, _ = store.upsert(
                {
                    "source": "history",
                    "url": url,
                    "title": url,
                    "cable_name": "AAG",
                    "occurrence_date": "2011-08-06",
                    "affected_area": "Vietnam",
                    "reason": "Cable fault",
                    "original_text": "AAG cable fault affected Vietnam in August 2011.",
                }
            )
            second_inserted, _ = store.upsert(
                {
                    "source": "history",
                    "url": url,
                    "title": url,
                    "cable_name": "AAG",
                    "occurrence_date": "2011-10-02",
                    "affected_area": "Vietnam",
                    "reason": "Cable fault",
                    "original_text": "AAG cable fault affected Vietnam in October 2011.",
                }
            )

            self.assertTrue(first_inserted)
            self.assertTrue(second_inserted)
            self.assertEqual(len(store.events), 2)

    def test_same_article_can_contain_multiple_cables(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(str(Path(tmp) / "events.jsonl"))
            url = "Internet Disruptions in Africa & Asia from AAE-1 Fault.txt"
            first_inserted, _ = store.upsert(
                {
                    "source": "history",
                    "url": url,
                    "title": url,
                    "cable_name": "AAE-1",
                    "occurrence_date": "2022-06-09",
                    "affected_area": "Africa and Asia",
                    "reason": "Cable fault",
                    "original_text": "Internet disruptions in Africa and Asia from cable fault.",
                }
            )
            second_inserted, _ = store.upsert(
                {
                    "source": "history",
                    "url": url,
                    "title": url,
                    "cable_name": "SEA-ME-WE 5",
                    "occurrence_date": "2022-06-09",
                    "affected_area": "Africa and Asia",
                    "reason": "Cable fault",
                    "original_text": "Internet disruptions in Africa and Asia from cable fault.",
                }
            )

            self.assertTrue(first_inserted)
            self.assertTrue(second_inserted)
            self.assertEqual(len(store.events), 2)


class HtmlExtractionTests(unittest.TestCase):
    def test_extract_date_from_meta(self):
        soup = BeautifulSoup(
            '<html><head><meta property="article:published_time" content="2025-03-02"></head></html>',
            "html.parser",
        )
        self.assertEqual(extract_date_from_soup(soup), "2025-03-02")


if __name__ == "__main__":
    unittest.main()
