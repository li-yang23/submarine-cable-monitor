import json
import logging
import os
import re
from typing import Any

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

CABLE_PATTERNS = (
    r"SEA[-\s]?ME[-\s]?WE\s*\d+",
    r"SMW\s*\d+",
    r"AAE[-\s]?\d+",
    r"APG",
    r"AAG",
    r"WACS",
    r"PEACE",
    r"SEACOM",
    r"EASSy",
    r"IMEWE",
)


class EventExtractor:
    def __init__(self, model: str | None = None):
        self.siliconflow_key = os.getenv("SILICONFLOW_API_KEY")
        self.openai_key = os.getenv("OPENAI_API_KEY")
        self.api_key = self.siliconflow_key or self.openai_key
        self.model = model or os.getenv("LLM_MODEL") or ("deepseek-ai/DeepSeek-V3" if self.siliconflow_key else "gpt-4o-mini")
        self.base_url = os.getenv("SILICONFLOW_BASE_URL") if self.siliconflow_key else os.getenv("OPENAI_BASE_URL")
        self._client = None
        self.logger = logging.getLogger(self.__class__.__name__)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def extract(self, article: dict[str, Any]) -> list[dict[str, Any]]:
        if self.available:
            try:
                return self._extract_with_llm(article)
            except Exception as exc:
                self.logger.warning("LLM extraction failed, falling back to rules: %s", exc)
                return self._extract_with_rules(article)
        return self._extract_with_rules(article)

    def is_real_event(self, event: dict[str, Any]) -> bool:
        if not clean_text(event.get("cable_name") or event.get("submarine_name")):
            return False
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

            kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = OpenAI(**kwargs)
        return self._client

    def _extract_with_llm(self, article: dict[str, Any]) -> list[dict[str, Any]]:
        content = clean_text(article.get("content"))[:5000]
        prompt = f"""
Extract information about any submarine cable accidents described in the following article. For each submarine cable accident, provide the details in JSON format.

Article URL: {article.get("url", "")}
Article title: {article.get("title", "")}
Article Text:
{content}

Options:
- Limit
- Cut
- Fluctuate

For each submarine cable accident, include the following key information:
1. Cable Name
2. Accident Location
3. Reason for the Accident
4. Accident Type [Choose one of the options above]
5. Affected Area
6. Occurrence Date
7. Repair Date
8. Original Text Relate to the Accident
9. Chinese Translation of the Original Text

Example Response:
{{
  "submarine_accidents": [
    {{
      "cable_name": "Cable Name",
      "accident_location": "Accident Location",
      "reason": "Reason for the Accident",
      "accident_type": "Cut",
      "affected_area": "Affected Area",
      "occurrence_date": "Occurrence Date",
      "repair_date": "Repair Date",
      "original_text": "Original Text Relate to the Accident",
      "original_text_zh": "与事故相关的原文中文翻译"
    }}
  ]
}}

Output accident in JSON format DIRECTLY, do not use symbols like ```json.
"""
        response = self._client_instance().chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content or ""
        data = self._parse_json(raw)
        events = data.get("submarine_accidents", []) if isinstance(data, dict) else []
        return [self._decorate_event(event, article) for event in events if isinstance(event, dict)]

    def check_event_against_context(self, context: str, accident: dict[str, Any]) -> bool:
        if not self.available:
            return self.is_real_event(accident)
        prompt = f"""
You are tasked with determining if a given message describes an accident that matches the details provided in a JSON format. Your goal is to generate a response indicating whether the accident described in the message aligns with the information in the JSON description.

Message: {context}

Accident Description (JSON Format):
{json.dumps(accident, ensure_ascii=False)}

Instructions:
1. Read the message describing the accident carefully.
2. Examine the details provided in the JSON format for the accident.
3. Consider factors such as location, time, type of accident, and any specific events or characteristics mentioned.
4. Generate a response indicating whether the accident described in the message matches the details provided in the JSON format. Directly output YES if match, NO otherwise.
"""
        response = self._client_instance().chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = (response.choices[0].message.content or "").strip().lower()
        return answer.startswith("yes")

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

        evidence = best_evidence_sentence(text)
        cable_name = clean_text(article.get("cable_name")) or extract_cable_name(evidence) or extract_cable_name(text)

        candidate = {
            "cable_name": cable_name,
            "accident_location": extract_location(evidence),
            "reason": extract_reason(evidence),
            "accident_type": normalize_accident_type("", text),
            "affected_area": extract_affected_area(evidence),
            "occurrence_date": clean_text(article.get("published_date")),
            "repair_date": "",
            "original_text": evidence or text[:1200],
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
            "original_text_zh": clean_text(event.get("original_text_zh") or event.get("chinese_translation")),
            "url": clean_text(event.get("url") or article.get("url")),
            "discovered_url": clean_text(article.get("discovered_url")),
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


def extract_cable_name(text: str) -> str:
    for pattern in CABLE_PATTERNS:
        match = re.search(pattern, text, re.I)
        if match:
            value = re.sub(r"\s+", " ", match.group(0)).strip()
            if re.fullmatch(r"smw\s*\d+", value, re.I):
                number = re.search(r"\d+", value)
                return f"SEA-ME-WE {number.group(0)}" if number else value
            return value.upper() if value.upper() in {"APG", "AAG", "WACS", "PEACE", "SEACOM", "EASSY", "IMEWE"} else value
    return ""


def best_evidence_sentence(text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", clean_text(text))
    scored = []
    for sentence in sentences:
        lower = sentence.lower()
        if any(noise in lower for noise in ("read more", "sign up", "straight to your inbox", "tags:")):
            continue
        score = sum(1 for keyword in ACCIDENT_KEYWORDS if keyword in lower)
        score += 2 if extract_cable_name(sentence) else 0
        if score:
            scored.append((score, sentence))
    if not scored:
        return clean_text(text)[:1200]
    scored.sort(key=lambda item: item[0], reverse=True)
    chosen = scored[0][1]
    return chosen[:1200]


def extract_reason(text: str) -> str:
    match = re.search(r"(fault|damage|cut|break|outage|disruption)[^.;,]*", text, re.I)
    return clean_text(match.group(0)) if match else ""


def extract_affected_area(text: str) -> str:
    if re.search(r"\bPakistan\b", text, re.I):
        return "Pakistan"
    if re.search(r"\bVietnam\b", text, re.I):
        return "Vietnam"
    if re.search(r"\bSouth Africa\b", text, re.I):
        return "South Africa"
    return ""


def extract_location(text: str) -> str:
    match = re.search(r"\b(?:near|off|in|between)\s+([A-Z][A-Za-z .-]{2,80})", text)
    return clean_text(match.group(1)) if match else ""
