import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.processing.extractor import EventExtractor, has_physical_cut_evidence


class ExtractionEvidenceTests(unittest.TestCase):
    def extract(self, content, *, evidence=None, time_evidence=None, occurrence="2026-10-02", published="2026-10-02", cable="", accident_type="Cut"):
        event = {
            "cable_name": cable, "accident_location": "Philippines-Hong Kong",
            "reason": None, "accident_type": accident_type, "affected_area": "Philippines",
            "occurrence_date": occurrence, "repair_date": None,
            "original_text": evidence or content,
        }
        if time_evidence is not None:
            event["occurrence_date_evidence"] = time_evidence
        extractor = EventExtractor()
        extractor._client = MagicMock()
        extractor._client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"submarine_accidents": [event]})))],
        )
        article = {"content": content, "published_date": published, "source": "fixture", "url": "https://example.test/news", "title": "Submarine cable outage"}
        return extractor._extract_with_llm(article)[0]

    def test_named_baltic_and_indigo_incidents_are_not_rejected_by_registry(self):
        extractor = EventExtractor()
        for cable, text in (
            ("C-Lion1 and BCS East-West Interlink", "C-Lion1 and BCS East-West Interlink cables were cut in the Baltic Sea."),
            ("Indigo West and Indigo Central", "Indigo West and Indigo Central cables off Perth were damaged."),
        ):
            event = {"cable_name": cable, "original_text": text, "evidence_supported": True}
            self.assertEqual(extractor.rejection_reason(event), "")
            event["original_text"] = text.replace("were", "could be")
            self.assertEqual(extractor.rejection_reason(event), "no_incident_keyword")

    def test_affected_hong_kong_segment_is_not_a_confirmed_cut(self):
        evidence = "They also reported that the segment connecting the Philippines and Hong Kong was likewise affected."
        timing = "Internet users experienced slow connectivity on Friday following the reported outage of submarine cable segments."
        body = f"{timing} {evidence} The cause is unknown. Either a fiber cut through natural causes or equipment failure is possible."
        event = self.extract(body, evidence=evidence, time_evidence=timing)
        self.assertEqual(event["accident_type"], "Limit")
        self.assertEqual(event["occurrence_date"], "2026-10-02")
        self.assertEqual(event["raw_data"]["extracted"]["accident_type"], "Cut")

    def test_weekend_does_not_support_an_invented_precise_date(self):
        quote = "Internet services were disrupted over the weekend after a submarine cable between the Philippines and Singapore was cut."
        event = self.extract(quote, occurrence="2026-10-01", published="2026-10-04")
        self.assertIsNone(event["occurrence_date"])
        self.assertEqual(EventExtractor().rejection_reason(event), "unnamed_missing_date")
        self.assertEqual(event["raw_data"]["extracted"]["occurrence_date"], "2026-10-01")

    def test_published_header_is_not_incident_time_evidence(self):
        header = "Published October 2, 2026."
        quote = "A submarine cable outage affected the Philippines."
        for timing in (header, header + " " + quote):
            with self.subTest(timing=timing):
                event = self.extract(header + " " + quote, evidence=quote, time_evidence=timing)
                self.assertIsNone(event["occurrence_date"])

    def test_fabricated_time_quote_is_not_accepted(self):
        event = self.extract("A submarine cable outage affected the Philippines over the weekend.", time_evidence="A submarine cable outage occurred on October 2, 2026.")
        self.assertIsNone(event["occurrence_date"])

    def test_press_release_day_is_not_incident_time(self):
        quote = "The operator discussed the cable disruption in a media release on Sunday."
        event = self.extract(quote, occurrence="2026-10-04", published="2026-10-04")
        self.assertIsNone(event["occurrence_date"])

    def test_explicit_physical_cut_and_full_dates_are_preserved(self):
        for date_text in ("2026-10-02", "October 2, 2026", "2 October 2026", "2026年10月2日"):
            with self.subTest(date=date_text):
                quote = f"The C2C cable was cut on {date_text}."
                event = self.extract(quote, cable="C2C")
                self.assertEqual(event["accident_type"], "Cut")
                self.assertEqual(event["occurrence_date"], "2026-10-02")

    def test_month_day_or_unique_relative_date_can_use_publication_context(self):
        for timing, published in (("on October 2", "2026-10-04"), ("yesterday", "2026-10-03"), ("on Friday", "2026-10-04"), ("today", "2026-10-02")):
            with self.subTest(timing=timing):
                quote = f"A submarine cable outage affected the Philippines {timing}."
                event = self.extract(quote, published=published, accident_type="Limit")
                self.assertEqual(event["occurrence_date"], "2026-10-02")

    def test_weekday_does_not_override_different_explicit_date(self):
        event = self.extract("A submarine cable outage affected the Philippines on Friday, October 9, 2026.")
        self.assertIsNone(event["occurrence_date"])

    def test_possible_cut_and_non_cable_cut_are_not_physical_evidence(self):
        for quote in (
            "A possible fiber cut affected connectivity.",
            "The cable might have been cut or equipment may have failed.",
            "Crews cut repair times by working overnight.",
            "The operator confirmed there was no fiber cut.",
            "The cable was not cut.",
            "海缆并未断裂。",
            "海缆可能断裂。",
            "The affected segment connects the Philippines and Hong Kong.",
        ):
            with self.subTest(quote=quote):
                self.assertFalse(has_physical_cut_evidence(quote))

    def test_confirmed_cut_is_not_negated_by_month_may_or_unknown_root_cause(self):
        for quote in ("The C2C cable was cut on May 2, 2026.", "On May 2, 2026 the C2C cable was cut.", "The C2C cable was cut but the cause remains unknown.", "海缆已经断裂，但原因未知。"):
            with self.subTest(quote=quote):
                self.assertTrue(has_physical_cut_evidence(quote))

    def test_fabricated_physical_cut_quote_is_flagged_for_review(self):
        event = self.extract("There was an outage on Friday.", evidence="The C2C cable was cut on October 2, 2026.", cable="C2C")
        self.assertNotEqual(event["accident_type"], "Cut")
        self.assertEqual(EventExtractor().rejection_reason(event), "evidence_not_in_article")

    def test_repair_or_planned_date_is_not_an_occurrence_date(self):
        for quote in ("The C2C cable outage was repaired on October 2, 2026.", "The C2C cable was affected on September 30 and restored on October 2, 2026.", "The C2C cable outage is expected on October 2, 2026."):
            with self.subTest(quote=quote):
                event = self.extract(quote)
                self.assertIsNone(event["occurrence_date"])


if __name__ == "__main__":
    unittest.main()
