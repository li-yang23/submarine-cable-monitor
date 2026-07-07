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
                    fieldnames=["cable_name", "accident_location", "accident_type", "occurrence_date", "url", "original_text"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "cable_name": "WACS",
                        "accident_location": "Namibia",
                        "accident_type": "Cut",
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
                    "accident_type": "Cut",
                    "occurrence_date": "2025-06-01",
                    "url": "https://example.com/wacs",
                    "original_text": "WACS cable fault in Namibia.",
                }
            )
            self.assertFalse(inserted)
            self.assertIn("SubTel Forum", event["sources"])
            self.assertEqual(len(store.events), 1)


class HtmlExtractionTests(unittest.TestCase):
    def test_extract_date_from_meta(self):
        soup = BeautifulSoup(
            '<html><head><meta property="article:published_time" content="2025-03-02"></head></html>',
            "html.parser",
        )
        self.assertEqual(extract_date_from_soup(soup), "2025-03-02")


if __name__ == "__main__":
    unittest.main()
