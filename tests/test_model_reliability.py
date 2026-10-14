import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from src.processing.extractor import EventExtractor, ExtractionUnavailable
from src.utils.model_health import check_model_health


ARTICLE = {"title": "C2C cable outage", "url": "https://example.test/outage", "content": "C2C cable outage affected the Philippines."}


class StrictExtractionTests(unittest.TestCase):
    def make_extractor(self, **kwargs):
        with patch.dict(os.environ, {"SILICONFLOW_API_KEY": "private-key"}, clear=True):
            return EventExtractor(**kwargs)

    def test_missing_key_never_silently_uses_rules(self):
        with patch.dict(os.environ, {}, clear=True):
            extractor = EventExtractor()
        with patch.object(extractor, "_extract_with_rules") as rules:
            with self.assertRaises(ExtractionUnavailable) as caught:
                extractor.extract(ARTICLE)
        self.assertEqual(caught.exception.reason, "missing_api_key")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(extractor.last_extraction_method, "unavailable")
        rules.assert_not_called()

    def test_provider_failures_are_safe_and_structured_without_fallback(self):
        for status, reason, retryable in (
            (402, "balance_exhausted", False), (401, "authentication_failed", False),
            (403, "authentication_failed", False), (429, "rate_limited", True),
            (500, "provider_unavailable", True), (503, "provider_unavailable", True),
            (400, "request_rejected", False),
        ):
            with self.subTest(status=status):
                extractor = self.make_extractor()
                exc = RuntimeError("private-key PRIVATE ARTICLE BODY")
                exc.status_code = status
                with patch.object(extractor, "_extract_with_llm", side_effect=exc) as llm, patch.object(extractor, "_extract_with_rules") as rules:
                    with self.assertRaises(ExtractionUnavailable) as caught:
                        extractor.extract(ARTICLE)
                failure = caught.exception
                self.assertEqual((failure.reason, failure.http_status, failure.retryable, failure.provider), (reason, status, retryable, "siliconflow"))
                self.assertNotIn("PRIVATE", str(failure))
                self.assertNotIn("private-key", str(failure))
                self.assertIsNone(failure.__cause__)
                self.assertIsNone(failure.__context__)
                self.assertFalse(hasattr(failure, "response"))
                llm.assert_called_once()
                rules.assert_not_called()

    def test_timeout_and_connection_errors_remain_retryable(self):
        for exc, reason in ((requests.Timeout("secret"), "request_timeout"), (requests.ConnectionError("secret"), "connection_failed")):
            with self.subTest(reason=reason):
                extractor = self.make_extractor()
                with patch.object(extractor, "_extract_with_llm", side_effect=exc):
                    with self.assertRaises(ExtractionUnavailable) as caught:
                        extractor.extract(ARTICLE)
                self.assertEqual(caught.exception.reason, reason)
                self.assertTrue(caught.exception.retryable)

    def test_sdk_retry_is_disabled_and_default_siliconflow_endpoint_is_used(self):
        extractor = self.make_extractor()
        with patch("openai.OpenAI") as client:
            extractor._client_instance()
        self.assertEqual(client.call_args.kwargs["max_retries"], 0)
        self.assertEqual(client.call_args.kwargs["base_url"], "https://api.siliconflow.cn/v1")

    def test_rule_fallback_requires_explicit_opt_in(self):
        extractor = self.make_extractor(allow_rule_fallback=True)
        with patch.object(extractor, "_extract_with_llm", side_effect=RuntimeError("secret")):
            events = extractor.extract(ARTICLE)
        self.assertEqual(events[0]["cable_name"], "C2C")
        self.assertEqual(extractor.last_extraction_method, "rules_fallback")

    def test_malformed_responses_are_not_recorded_as_zero_incidents(self):
        for raw in ("not JSON PRIVATE", "[]", "{}", '{"submarine_accidents": [null]}', '{"submarine_accidents": [{}]}'):
            with self.subTest(raw=raw):
                extractor = self.make_extractor()
                extractor._client = MagicMock()
                extractor._client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])
                with self.assertRaises(ExtractionUnavailable) as caught:
                    extractor.extract(ARTICLE)
                self.assertEqual(caught.exception.reason, "invalid_response")
                self.assertNotIn("PRIVATE", str(caught.exception))

    def test_valid_zero_and_valid_incident_outputs_still_work(self):
        event = {"cable_name": "C2C", "accident_location": None, "reason": None, "accident_type": "Limit", "affected_area": "Philippines", "occurrence_date": None, "repair_date": None, "original_text": ARTICLE["content"]}
        for expected in ([], [event]):
            extractor = self.make_extractor()
            extractor._client = MagicMock()
            extractor._client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"submarine_accidents": expected})))])
            result = extractor.extract(ARTICLE)
            self.assertEqual(len(result), len(expected))
            self.assertEqual(extractor.last_extraction_method, "llm")
            if result:
                self.assertIsNone(result[0]["occurrence_date"])

    def test_truncated_or_filtered_response_is_not_a_successful_empty_result(self):
        for finish_reason in ("length", "content_filter"):
            extractor = self.make_extractor()
            extractor._client = MagicMock()
            extractor._client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content='{"submarine_accidents": []}'))])
            with self.assertRaises(ExtractionUnavailable) as caught:
                extractor.extract(ARTICLE)
            self.assertEqual(caught.exception.reason, "invalid_response")

    def test_invalid_incident_field_types_fail_the_entire_response(self):
        event = {"cable_name": "C2C", "accident_location": None, "reason": None, "accident_type": "Limit", "affected_area": "Philippines", "occurrence_date": None, "repair_date": None, "original_text": ARTICLE["content"]}
        for invalid in ({**event, "cable_name": ["C2C"]}, {**event, "accident_type": "Unknown"}, {**event, "original_text": ""}):
            extractor = self.make_extractor()
            extractor._client = MagicMock()
            extractor._client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"submarine_accidents": [event, invalid]})))])
            with self.assertRaises(ExtractionUnavailable) as caught:
                extractor.extract(ARTICLE)
            self.assertEqual(caught.exception.reason, "invalid_response")


class ModelHealthTests(unittest.TestCase):
    def test_retired_siliconflow_balance_api_is_not_called_or_reported_healthy(self):
        with patch("requests.sessions.Session.request") as request:
            result = check_model_health("private-key", "siliconflow", "https://api.siliconflow.cn/v1", 5)
        request.assert_not_called()
        self.assertEqual(result["status"], "health_unknown")
        self.assertEqual(result["reason"], "balance_api_retired")
        self.assertIsNone(result["available"])
        self.assertIsNone(result["balance"])
        self.assertFalse(result["balance_supported"])
        self.assertFalse(result["low_balance"])
        self.assertNotIn("private-key", json.dumps(result))

    def test_custom_balance_endpoints_never_receive_credentials(self):
        for url in ("http://api.siliconflow.cn/v1", "https://example.com/v1", "https://api.siliconflow.cn.example.com/v1", "https://user:pass@api.siliconflow.cn/v1", "https://api.siliconflow.cn:444/v1", "https://api.siliconflow.cn/v1?other=1"):
            with self.subTest(url=url), patch("requests.sessions.Session.request") as request:
                result = check_model_health("private-key", "siliconflow", url)
                request.assert_not_called()
                self.assertEqual(result["status"], "health_unknown")
                self.assertIsNone(result["available"])
                self.assertNotIn("private-key", json.dumps(result))
                self.assertNotIn("pass@", json.dumps(result))

    def test_other_providers_do_not_guess_a_billing_endpoint(self):
        with patch("requests.sessions.Session.request") as request:
            result = check_model_health("private-key", "openai", "https://api.openai.com/v1")
            missing = check_model_health(None, "siliconflow")
        request.assert_not_called()
        self.assertEqual(result["status"], "health_unknown")
        self.assertEqual(result["reason"], "balance_check_unsupported")
        self.assertEqual(missing["reason"], "missing_api_key")
        self.assertFalse(missing["available"])


if __name__ == "__main__":
    unittest.main()
