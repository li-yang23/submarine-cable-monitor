import json
import os
import re
from typing import Any, Iterable

from .normalization import clean_text, normalize_accident_type, parse_date


ACCIDENT_KEYWORDS = (
    "submarine cable fault",
    "undersea cable fault",
    "cable fault",
    "cable cut",
    "cable break",
    "cable damage",
    "internet outage",
    "service disruption",
    "repair",
    "海缆故障",
    "海底电缆故障",
)


class EventExtractor:
    def __init__(self, model: str | None = None):
        self.model = model or os.getenv("LLM_MODEL", "deepseek-ai/DeepSeek-V3")
        self.api_key = os.getenv("SILICONFLOW_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("SILICONFLOW_BASE_URL") or "https://api.siliconflow.cn/v1/"
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def extract(self, article: dict[str, Any]) -> list[dict[str, Any]]:
        if self.available:
            try:
                return self._extract_with_llm(article)
            except Exception:
                return self._extract_with_rules(article)
        return self._extract_with_rules(article)

    def is_real_event(self, event: dict[str, Any]) -> bool:
        text = " ".join(
            clean_text(event.get(key))
            for key in ("title", "original_text", "reason", "accident_type", "affected_area")
        ).lower()
        return any(keyword in text for keyword in ACCIDENT_KEYWORDS)

    def is_duplicate(self, new_event: dict[str, Any], existing_event: dict[str, Any]) -> bool:
        if self.available:
            try:
                return self._duplicate_with_llm(new_event, existing_event)
            except Exception:
                pass
        return False

    def _client_instance(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def _extract_with_llm(self, article: dict[str, Any]) -> list[dict[str, Any]]:
        content = clean_text(article.get("content"))[:5000]
        prompt = f"""
Extract submarine cable accident events from the article. Return JSON directly, without markdown fences.

Article URL: {article.get("url", "")}
Article title: {article.get("title", "")}
Article text:
{content}

Return schema:
{{
  "submarine_accidents": [
    {{
      "cable_name": "",
      "accident_location": "",
      "reason": "",
      "accident_type": "Cut|Limit|Fluctuate|Maintenance|Unknown",
      "affected_area": "",
      "occurrence_date": "",
      "repair_date": "",
      "original_text": ""
    }}
  ]
}}
"""
        response = self._client_instance().chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content or ""
        data = self._parse_json(raw)
        events = data.get("submarine_accidents", []) if isinstance(data, dict) else []
        return [self._decorate_event(event, article) for event in events if isinstance(event, dict)]

    def _duplicate_with_llm(self, new_event: dict[str, Any], existing_event: dict[str, Any]) -> bool:
        prompt = f"""
Determine whether these two submarine cable accident records describe the same real-world event.
Directly answer YES or NO.

New event:
{json.dumps(new_event, ensure_ascii=False)}

Existing event:
{json.dumps(existing_event, ensure_ascii=False)}
"""
        response = self._client_instance().chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = (response.choices[0].message.content or "").strip().lower()
        return answer.startswith("yes")

    def _extract_with_rules(self, article: dict[str, Any]) -> list[dict[str, Any]]:
        title = clean_text(article.get("title"))
        content = clean_text(article.get("content"))
        text = f"{title}. {content}"
        if not any(keyword in text.lower() for keyword in ACCIDENT_KEYWORDS):
            return []

        cable_name = clean_text(article.get("cable_name"))
        if not cable_name:
            cable_match = re.search(r"\b([A-Z][A-Z0-9-]{2,}(?:\s+[A-Z0-9-]{2,}){0,3})\b", text)
            cable_name = cable_match.group(1) if cable_match else ""

        candidate = {
            "cable_name": cable_name,
            "accident_location": "",
            "reason": "",
            "accident_type": normalize_accident_type("", text),
            "affected_area": "",
            "occurrence_date": clean_text(article.get("published_date")),
            "repair_date": "",
            "original_text": text[:1200],
        }
        return [self._decorate_event(candidate, article)]

    def _decorate_event(self, event: dict[str, Any], article: dict[str, Any]) -> dict[str, Any]:
        original_text = clean_text(event.get("original_text")) or clean_text(article.get("content"))[:1200]
        event = {
            "cable_name": clean_text(event.get("cable_name") or article.get("cable_name")),
            "accident_location": clean_text(event.get("accident_location")),
            "reason": clean_text(event.get("reason")),
            "accident_type": normalize_accident_type(event.get("accident_type"), original_text),
            "affected_area": clean_text(event.get("affected_area")),
            "occurrence_date": parse_date(event.get("occurrence_date")) or parse_date(article.get("published_date")),
            "repair_date": parse_date(event.get("repair_date")),
            "original_text": original_text,
            "url": clean_text(event.get("url") or article.get("url")),
            "title": clean_text(article.get("title")),
            "source": clean_text(article.get("source")),
            "published_date": parse_date(article.get("published_date")),
            "raw_data": {"article": article, "extracted": event},
        }
        return event

    def _parse_json(self, raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?", "", text).strip()
            text = re.sub(r"```$", "", text).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.S)
            if match:
                return json.loads(match.group(0))
            raise
