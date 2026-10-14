import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from src.processing.extractor import EventExtractor, ExtractionUnavailable
from src.utils.audit import AuditRun


ARTICLE = {"title": "Cable outage", "url": "https://example.com/news", "content": "PRIVATE ARTICLE BODY"}


class ModelUsageTests(unittest.TestCase):
    def run_responses(self, responses, *, expect_error=False):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditRun(tmp)
            audit.activate()
            extractor = EventExtractor(model="requested-model")
            extractor.api_key = "PRIVATE_API_KEY"
            extractor._client = MagicMock()
            extractor._client.chat.completions.create.side_effect = responses
            try:
                for _ in responses:
                    if expect_error:
                        with self.assertRaises(ExtractionUnavailable):
                            extractor.extract(ARTICLE)
                    else:
                        self.assertEqual(extractor.extract(ARTICLE), [])
            finally:
                summary = audit.finish({"status": "success"})
            raw = (audit.path / "audit.jsonl").read_text()
            records = [json.loads(line) for line in raw.splitlines()]
            markdown = (audit.path / "summary.md").read_text()
            stored = json.loads((audit.path / "summary.json").read_text())
            self.assertEqual(stored["model_usage"], summary["model_usage"])
            self.assertNotIn("PRIVATE ARTICLE BODY", raw + markdown)
            self.assertNotIn("PRIVATE_API_KEY", raw + markdown)
            self.assertNotIn("PRIVATE RESPONSE", raw + markdown)
            return records, summary["model_usage"], markdown

    def response(self, **changes):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"submarine_accidents": []}'))], **changes)

    def test_reported_tokens_aggregate_and_actual_model_is_traceable(self):
        responses = [self.response(model="actual-model", usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120)),
                     self.response(usage={"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50, "private_raw": "PRIVATE RESPONSE"})]
        records, usage, markdown = self.run_responses(responses)
        self.assertEqual([record["stage"] for record in records], ["model_usage", "model_usage"])
        self.assertEqual([record["model"] for record in records], ["actual-model", "requested-model"])
        self.assertTrue(all(record["api_operation"] == "chat.completions.create" for record in records))
        self.assertTrue(all(record["article_id"] and record["outcome"] == "reported" for record in records))
        self.assertEqual(usage, {"responses": 2, "responses_with_usage": 2, "prompt_tokens": 140,
                                 "completion_tokens": 30, "total_tokens": 170,
                                 "reported_responses": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 2}})
        self.assertIn('"total_tokens": 170', markdown)

    def test_missing_or_mock_usage_is_unknown_not_zero(self):
        records, usage, _ = self.run_responses([self.response(), self.response(usage=None), self.response(usage=MagicMock())])
        self.assertTrue(all(record["outcome"] == "unavailable" for record in records))
        self.assertTrue(all("total_tokens" not in record for record in records))
        self.assertEqual(usage["responses"], 3)
        self.assertEqual(usage["responses_with_usage"], 0)
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            self.assertIsNone(usage[name])
            self.assertEqual(usage["reported_responses"][name], 0)

    def test_partial_usage_never_invents_total_or_counts_invalid_values(self):
        records, usage, _ = self.run_responses([self.response(usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": "7"}),
                                               self.response(usage={"prompt_tokens": False, "completion_tokens": -1, "total_tokens": 3.0})])
        self.assertEqual(records[0]["prompt_tokens"], 5)
        self.assertNotIn("total_tokens", records[0])
        self.assertEqual(records[1]["outcome"], "unavailable")
        self.assertEqual(usage["responses_with_usage"], 1)
        self.assertEqual(usage["prompt_tokens"], 5)
        self.assertIsNone(usage["total_tokens"])
        self.assertEqual(usage["reported_responses"]["total_tokens"], 0)

    def test_usage_is_recorded_even_if_response_cannot_be_parsed(self):
        response = self.response(usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10))
        response.choices[0].message.content = "PRIVATE RESPONSE NOT JSON"
        records, usage, _ = self.run_responses([response], expect_error=True)
        self.assertEqual(records[0]["total_tokens"], 10)
        self.assertEqual(usage["total_tokens"], 10)

    def test_explicit_zero_usage_is_preserved_as_provider_reported(self):
        records, usage, _ = self.run_responses([self.response(usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})])
        self.assertEqual(records[0]["outcome"], "reported")
        self.assertEqual(usage["total_tokens"], 0)
        self.assertEqual(usage["reported_responses"]["total_tokens"], 1)


if __name__ == "__main__":
    unittest.main()
