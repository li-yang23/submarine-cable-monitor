import json
import logging
import os
import re
from datetime import date, timedelta
from typing import Any

from .normalization import clean_text, normalize_accident_type, parse_date
from src.utils.audit import article_fields, emit


ACCIDENT_KEYWORDS = (
    "submarine cable fault",
    "undersea cable fault",
    "cable fault",
    "cable cut",
    "cable break",
    "cable damage",
    "internet outage",
    "service disruption",
    "subsea cable disruption",
    "subsea cable outage",
    "subsea cable damage",
    "undersea cable disruption",
    "undersea cable outage",
    "undersea cable damage",
    "submarine cable disruption",
    "submarine cable outage",
    "submarine cable damage",
    "repair",
    "海缆故障",
    "海底电缆故障",
)

CABLE_PATTERNS = (
    r"\bC2C\b",
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


class ExtractionUnavailable(Exception):
    """Safe, structured failure: never retain response bodies, prompts, or keys."""

    def __init__(self, reason: str, *, http_status: int | None = None, retryable: bool = False, provider: str = "unknown"):
        self.reason = reason
        self.http_status = http_status
        self.retryable = retryable
        self.provider = provider
        super().__init__(reason)


def extraction_failure(exc: Exception, provider: str) -> ExtractionUnavailable:
    if isinstance(exc, ExtractionUnavailable):
        return exc
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if not isinstance(status, int):
        status = None
    if status == 402:
        reason, retryable = "balance_exhausted", False
    elif status in (401, 403):
        reason, retryable = "authentication_failed", False
    elif status == 429:
        reason, retryable = "rate_limited", True
    elif status is not None and status >= 500:
        reason, retryable = "provider_unavailable", True
    elif status is not None:
        reason, retryable = "request_rejected", False
    elif isinstance(exc, (ValueError, TypeError, IndexError, AttributeError)):
        reason, retryable = "invalid_response", True
    elif "timeout" in type(exc).__name__.lower():
        reason, retryable = "request_timeout", True
    elif "connection" in type(exc).__name__.lower():
        reason, retryable = "connection_failed", True
    else:
        reason, retryable = "extraction_failed", True
    return ExtractionUnavailable(reason, http_status=status, retryable=retryable, provider=provider)


class EventExtractor:
    def __init__(self, model: str | None = None, *, allow_rule_fallback: bool = False):
        self.siliconflow_key = os.getenv("SILICONFLOW_API_KEY")
        self.openai_key = os.getenv("OPENAI_API_KEY")
        self.api_key = self.siliconflow_key or self.openai_key
        self.model = model or os.getenv("LLM_MODEL") or ("deepseek-ai/DeepSeek-V3" if self.siliconflow_key else "gpt-4o-mini")
        self.provider = "siliconflow" if self.siliconflow_key else "openai"
        self.base_url = (os.getenv("SILICONFLOW_BASE_URL") or "https://api.siliconflow.cn/v1") if self.siliconflow_key else os.getenv("OPENAI_BASE_URL")
        self.allow_rule_fallback = allow_rule_fallback
        self._client = None
        self.logger = logging.getLogger(self.__class__.__name__)
        self.last_extraction_method = "unavailable"

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def extract(self, article: dict[str, Any]) -> list[dict[str, Any]]:
        failure = None
        if self.available:
            try:
                self.last_extraction_method = "llm"
                return self._extract_with_llm(article)
            except Exception as exc:
                failure = extraction_failure(exc, self.provider)
        else:
            failure = ExtractionUnavailable("missing_api_key", provider=self.provider)
        if self.allow_rule_fallback:
            self.last_extraction_method = "rules_fallback" if self.available else "rules"
            emit("extract", "fallback", **article_fields(article), reason=failure.reason,
                 method=self.last_extraction_method, http_status=failure.http_status)
            self.logger.warning("Explicit rule fallback enabled: %s", failure.reason)
            return self._extract_with_rules(article)
        self.last_extraction_method = "unavailable"
        # The original SDK exception may contain a full request or provider body.
        # Raise outside the except block without attaching that exception.
        raise failure from None

    def is_real_event(self, event: dict[str, Any]) -> bool:
        return not self.rejection_reason(event)

    def rejection_reason(self, event: dict[str, Any]) -> str:
        if event.get("evidence_supported") is False:
            return "evidence_not_in_article"
        cable_name = clean_text(event.get("cable_name") or event.get("submarine_name"))
        if not cable_name:
            return unnamed_event_rejection_reason(event)
        # A publisher may use a full system name absent from our acronym list,
        # e.g. "a fault on its Australia-Singapore Cable". Require the name and
        # incident in the quoted evidence, not merely the model's generated fields.
        if has_named_cable_incident(clean_text(event.get("original_text")), cable_name):
            return ""
        text = " ".join(
            clean_text(event.get(key))
            for key in ("title", "original_text", "reason", "accident_type", "affected_area")
        ).lower()
        return "" if any(keyword in text for keyword in ACCIDENT_KEYWORDS) or has_named_cable_incident(text) else "no_incident_keyword"

    def is_duplicate(self, new_event: dict[str, Any], existing_event: dict[str, Any]) -> bool:
        if self.available:
            try:
                return self._duplicate_with_llm(new_event, existing_event)
            except Exception:
                pass
        return False

    def translate_original_text(self, text: str) -> str:
        text = clean_text(text)
        if not text or not self.available:
            return ""
        prompt = f"""
Translate the following submarine cable incident evidence snippet into concise Chinese.
Keep cable names, organization names, place names, and dates accurate. Output only the Chinese translation.

Text:
{text[:1800]}
"""
        response = self._client_instance().chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        return clean_translation(response.choices[0].message.content)

    def _client_instance(self):
        if self._client is None:
            from openai import OpenAI

            # Failed articles remain queued for a future run. SDK retries can
            # multiply charges on timeouts, so one attempt is made per run.
            kwargs = {"api_key": self.api_key, "timeout": 45, "max_retries": 0}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = OpenAI(**kwargs)
        return self._client

    def _extract_with_llm(self, article: dict[str, Any]) -> list[dict[str, Any]]:
        content = clean_text(article.get("content"))[:5000]
        prompt = f"""
Extract information about any submarine cable accidents described in the following article. For each submarine cable accident, provide the details in JSON format.

Evidence rules:
- Include only incidents that actually occurred or are ongoing. Do not create incidents from policy discussions, hypothetical risks, drills, planned maintenance, or precautionary testing alone. If there are no supported incidents, return {{"submarine_accidents": []}}.
- Use only cable names explicitly identified in the article. If no specific cable is named, leave cable_name empty; never infer a name from the operator, route, destinations, or a related article.
- Do not invent a cause. Keep reason null when the root cause is unknown, and preserve attribution or uncertainty for alleged causes.
- Choose Cut only when this incident's quoted evidence explicitly reports physical cable/fiber cutting, breakage, severing, or damage. A segment being affected/down is Limit; slow/degraded connectivity is Fluctuate. A possible fiber cut elsewhere in the article is not proof of a cut for this incident.
- Return occurrence_date and repair_date as YYYY-MM-DD only when the full date is supported. Otherwise use null. Never invent a missing month or day, and never substitute the publication date for the incident date.
- original_text must be an exact quote from this article's body, not a paraphrase or a quote invented to support a claim.
- For any non-null occurrence_date, also return occurrence_date_evidence: the shortest exact quote from the article body describing only when this incident happened. Do not quote the publication/update header or mix in repair/restoration/expected dates. A vague interval such as "over the weekend" cannot establish a single day; use null.
- A repair estimate, expected restoration date, or repair work starting is not a completed repair. Keep repair_date null unless actual restoration or repair completion is reported with a supported full date.
- The publication date below is context only for explicitly stated relative dates (such as "yesterday"). Resolve a relative date only when its reference is unambiguous; otherwise leave it null.

Article URL: {article.get("url", "")}
Article title: {article.get("title", "")}
Article publication date (relative-date context only): {article.get("published_date") or "unknown"}
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
      "occurrence_date": null,
      "occurrence_date_evidence": "",
      "repair_date": null,
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
        usage = getattr(response, "usage", None)
        tokens = {}
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            # Missing usage and mock SDK attributes are unknown, never fabricated
            # zeroes. Do not calculate total_tokens from partially returned fields.
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                tokens[name] = value
        model = getattr(response, "model", None)
        if not isinstance(model, str) or not model.strip():
            model = self.model
        emit("model_usage", "reported" if tokens else "unavailable", **article_fields(article),
             provider=self.provider, model=model, api_operation="chat.completions.create", **tokens)
        choice = response.choices[0]
        if getattr(choice, "finish_reason", None) in {"length", "content_filter"}:
            raise ValueError("Incomplete model response")
        raw = choice.message.content or ""
        data = self._parse_json(raw)
        if not isinstance(data, dict) or not isinstance(data.get("submarine_accidents"), list):
            raise ValueError("LLM response is missing the submarine_accidents array")
        events = data["submarine_accidents"]
        fields = {"cable_name", "accident_location", "reason", "accident_type", "affected_area", "occurrence_date", "repair_date", "original_text"}
        for event in events:
            if not isinstance(event, dict) or not fields <= event.keys():
                raise ValueError("Invalid incident object schema")
            if any(value is not None and not isinstance(value, str) for key, value in event.items() if key in fields | {"original_text_zh", "chinese_translation", "occurrence_date_evidence"}):
                raise ValueError("Invalid incident field type")
            if not clean_text(event.get("original_text")):
                raise ValueError("Incident evidence is missing")
            if event.get("accident_type") not in {"Limit", "Cut", "Fluctuate"}:
                raise ValueError("Invalid incident type")
        result = []
        for index, event in enumerate(events):
            candidate = dict(event)
            evidence = clean_text(event.get("original_text"))
            evidence_supported = evidence.casefold() in clean_text(article.get("content")).casefold()
            if event["accident_type"] == "Cut" and not (evidence_supported and has_physical_cut_evidence(evidence)):
                supported_type = normalize_accident_type("", evidence)
                candidate["accident_type"] = supported_type if supported_type in {"Limit", "Fluctuate", "Maintenance"} else "Limit"
                emit("verify", "corrected", **article_fields(article), candidate_index=index,
                     reason="unsupported_physical_cut")
            candidate["occurrence_date"] = supported_occurrence_date(
                event.get("occurrence_date"), event.get("occurrence_date_evidence") or evidence, article,
            )
            if parse_incident_date(event.get("occurrence_date")) and not candidate["occurrence_date"]:
                emit("verify", "corrected", **article_fields(article), candidate_index=index,
                     reason="unsupported_occurrence_date")
            decorated = self._decorate_event(candidate, article)
            decorated["evidence_supported"] = evidence_supported
            # Preserve the original model fields for review, while only the
            # evidence-supported values enter normalized event fields.
            decorated["raw_data"]["extracted"] = event
            result.append(decorated)
        return result

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
        if not any(keyword in text.lower() for keyword in ACCIDENT_KEYWORDS) and not has_explicit_subsea_incident(text) and not has_named_cable_incident(text):
            return []

        evidence = best_evidence_sentence(text)
        cable_name = clean_text(article.get("cable_name")) or extract_cable_name(evidence) or extract_cable_name(text)

        candidate = {
            "cable_name": cable_name,
            "accident_location": extract_location(evidence),
            "reason": extract_reason(evidence),
            "accident_type": normalize_accident_type("", text),
            "affected_area": extract_affected_area(evidence) or extract_affected_area(text),
            # Rule extraction cannot reliably distinguish incident dates from
            # publication, repair estimates, or other dates mentioned in prose.
            "occurrence_date": None,
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
            "occurrence_date": parse_incident_date(event.get("occurrence_date")),
            "repair_date": parse_incident_date(event.get("repair_date")),
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


def parse_incident_date(value: Any) -> str | None:
    text = clean_text(value)
    # The general date parser accepts month/year for historical display, but its
    # implicit first day is not evidence of an incident or repair completion date.
    if re.fullmatch(r"[A-Za-z]+\s+\d{4}", text):
        return None
    return parse_date(text)


def has_physical_cut_evidence(text: str) -> bool:
    for sentence in re.split(r"(?<=[.!?])\s+", clean_text(text)):
        cable = r"(?:cables?|fibers?|fibres?|" + "|".join(CABLE_PATTERNS) + ")"
        physical = r"(?:cut(?:s|ting)?|sever(?:ed|ing)?|broken|break(?:s|age)?|damag(?:e|ed|ing))"
        for match in re.finditer(rf"\b{physical}\b", sentence, re.I):
            before = sentence[max(0, match.start() - 80):match.start()]
            after = sentence[match.end():match.end() + 80]
            if not re.search(rf"\b{cable}\b", before + " " + after, re.I):
                continue
            # Qualify the physical-damage assertion itself. A month named May
            # or a subsequently unknown root cause is not uncertainty about it.
            if re.search(r"\b(?:may|might|could|would)\b[^\d.!?]{0,40}$|\b(?:possible|possibly|potential|suspected|hypothetical|whether|either)\b[^.!?]{0,45}$|\b(?:no evidence|not confirmed|cannot confirm|can't confirm)\b[^.!?]{0,60}$", before, re.I):
                continue
            if re.search(r"\bnot(?:\s+\w+){0,2}\s*$|\b(?:no|without)\s+(?:(?:a|physical|submarine|undersea|subsea|cable|fiber|fibre)\s+)*$", before, re.I):
                continue
            if re.search(r"^\s+(?:is|was|remains|has been)\s+(?:possible|unconfirmed|unclear|unknown|ruled out|not confirmed|denied)\b", after, re.I):
                continue
            return True
        for match in re.finditer(r"(?:海缆|海底光缆|光纤)(.{0,30}?)(?:切断|断裂|损坏|斩断)", sentence):
            prefix = sentence[max(0, match.start() - 12):match.start()]
            if re.search(r"(?:没有|并无|无法确认|不能确认|尚未确认)$", prefix):
                continue
            if not re.search(r"未|没有|并无|不曾|不会|不确定|可能|或许|疑似|是否", match.group(1)):
                return True
    return False


def supported_occurrence_date(value: Any, evidence: Any, article: dict[str, Any]) -> str | None:
    """Keep a date only when a real incident-time quote can support that day."""
    parsed = parse_incident_date(value)
    quote = clean_text(evidence)
    content = clean_text(article.get("content"))
    if not parsed or not quote or quote.casefold() not in content.casefold():
        return None
    if re.search(r"\b(?:published|updated|publication date|last modified|media release|press release)\b", quote, re.I):
        return None  # Metadata is never evidence of an incident's occurrence.
    if re.search(r"\b(?:repair\w*|restor\w*|expected|estimated|scheduled|planned|forecast)\b|预计|预期|计划|修复|恢复", quote, re.I):
        return None  # Mixed occurrence/restoration/planning dates need review.
    if not re.search(r"\b(?:outage|disrupt\w*|disconnect\w*|interrupt\w*|degrad\w*|slow\w*|down|affected|offline|cut|broken|break\w*|damag\w*|fault|occur\w*)\b|中断|故障|断裂|损坏", quote, re.I):
        return None
    day = date.fromisoformat(parsed)
    month_name = rf"(?:{day.strftime('%B')}|{day.strftime('%b')}\.?)"
    full_patterns = (
        rf"\b{day.year}[-/.]0?{day.month}[-/.]0?{day.day}\b",
        rf"\b{month_name}\s+0?{day.day}(?:st|nd|rd|th)?[,]?\s+{day.year}\b",
        rf"\b0?{day.day}(?:st|nd|rd|th)?\s+{month_name}[,]?\s+{day.year}\b",
        rf"{day.year}年0?{day.month}月0?{day.day}日",
    )
    if any(re.search(pattern, quote, re.I) for pattern in full_patterns):
        return parsed
    published = parse_date(article.get("published_date"))
    if not published:
        return None
    reference = date.fromisoformat(published)
    # A recent month/day mention can use the publication year as context. An
    # explicit conflicting year must never be silently replaced.
    years = re.findall(r"\b(?:19|20)\d{2}\b", quote)
    short_pattern = rf"\b(?:{month_name}\s+0?{day.day}(?:st|nd|rd|th)?|0?{day.day}(?:st|nd|rd|th)?\s+{month_name})\b"
    if not years and 0 <= (reference - day).days <= 31 and re.search(short_pattern, quote, re.I):
        return parsed
    # Do not use a weekday to override an explicit but different calendar date.
    months = "|".join(date(2000, number, 1).strftime("%B") + "|" + date(2000, number, 1).strftime("%b") for number in range(1, 13))
    if re.search(rf"\b(?:{months})\.?\s+\d{{1,2}}\b|\b\d{{1,2}}\s+(?:{months})\b|\b\d{{4}}[-/.]\d{{1,2}}[-/.]\d{{1,2}}\b", quote, re.I):
        return None
    for phrase, offset in (("day before yesterday", 2), ("yesterday", 1), ("today", 0)):
        if re.search(rf"\b{phrase}\b", quote, re.I):
            return parsed if day == reference - timedelta(days=offset) else None
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    for match in re.finditer(r"\b(on|since|last|this)\s+(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", quote, re.I):
        prefix, weekday = (part.lower() for part in match.groups())
        index = weekdays.index(weekday)
        if prefix == "this" and reference.weekday() < index:
            continue
        distance = (reference.weekday() - index) % 7
        if prefix == "last" and distance == 0:
            distance = 7
        if day == reference - timedelta(days=distance):
            return parsed
    return None


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


def clean_translation(value: Any) -> str:
    text = clean_text(value)
    text = re.sub(r"^[\"'“”]+|[\"'“”]+$", "", text).strip()
    text = re.sub(r"\s*[（(]注[:：].*$", "", text, flags=re.S).strip()
    text = re.sub(r"\s*注[:：].*$", "", text, flags=re.S).strip()
    return clean_text(text)


def best_evidence_sentence(text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", clean_text(text))
    scored = []
    for sentence in sentences:
        lower = sentence.lower()
        if any(noise in lower for noise in ("read more", "sign up", "straight to your inbox", "tags:")):
            continue
        score = sum(1 for keyword in ACCIDENT_KEYWORDS if keyword in lower)
        score += 3 if has_explicit_subsea_incident(sentence) else 0
        score += 2 if extract_cable_name(sentence) else 0
        score += 3 if has_named_cable_incident(sentence) else 0
        if len(sentence) < 60:
            score -= 1
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
    if re.search(r"\bPhilippines\b|\bFilipino\b", text, re.I):
        return "Philippines"
    if re.search(r"\bHong Kong\b", text, re.I):
        return "Hong Kong"
    if re.search(r"\bSingapore\b", text, re.I):
        return "Singapore"
    if re.search(r"\bPakistan\b", text, re.I):
        return "Pakistan"
    if re.search(r"\bVietnam\b", text, re.I):
        return "Vietnam"
    if re.search(r"\bSouth Africa\b", text, re.I):
        return "South Africa"
    return ""


def has_explicit_subsea_incident(text: str) -> bool:
    """Require a cable-specific factual incident, not a general network outage."""
    telecom = r"(?:telecom(?:munications?)?|communications?|internet|fiber[ -]?optic|fibre[ -]?optic)"
    cable = rf"(?:submarine|undersea|subsea)\s+(?:{telecom}\s+)?cables?"
    incident = r"(?:disrupt\w*|outages?|damag\w*|faults?|cut(?:s|ting)?|breaks?|broken|sever\w*)"
    for sentence in re.split(r"(?<=[.!?])\s+", clean_text(text)):
        if re.search(r"\b(?:may|might|could|would|potential|hypothetical|risk of|if there|no evidence|no reported|no submarine|no undersea|no subsea|does not establish|drills?|exercises?|simulat\w*|scenarios?|policy (?:review|proposal|discussion))\b", sentence, re.I):
            continue
        if re.search(rf"\b{cable}\b[^.!?]{{0,80}}\b{incident}\b|\b{incident}\b[^.!?]{{0,80}}\b{cable}\b", sentence, re.I):
            return True
        # Some reports establish the underwater setting through a seabed anchor
        # drag, then explicitly describe cutting telecom cables (e.g. Fitburg).
        if re.search(rf"\b(?:seabed|sea[- ]?floor)\b[^.!?]{{0,80}}\b(?:cut(?:s|ting)?|sever(?:ed|ing)?|damag(?:ed|ing))\b[^.!?]{{0,30}}\b{telecom}\s+cables?\b", sentence, re.I):
            return True
        if re.search(r"(?:海缆|海底(?:光缆|电缆)).{0,30}(?:中断|故障|断裂|损坏)", sentence):
            return True
    return False


def has_named_cable_incident(text: str, cable_name: str = "") -> bool:
    for sentence in re.split(r"(?<=[.!?])\s+", clean_text(text)):
        known_name = extract_cable_name(sentence)
        if cable_name:
            # Unknown full names need explicit cable and communications context;
            # a model-generated name alone cannot turn a generic fault into news.
            if not re.search(re.escape(cable_name), sentence, re.I):
                continue
            if not known_name and not (re.search(r"\bcable\b", cable_name, re.I) and re.search(r"\b(?:internet|telecom\w*|ethernet|traffic|connectivity|bandwidth)\b", sentence, re.I)):
                continue
        elif not known_name:
            continue
        if re.search(r"\b(?:may|might|could|would|potential|hypothetical|risk of|does not establish|no evidence|no reported|drills?|exercises?|simulat\w*|scenarios?|policy (?:review|proposal|discussion))\b", sentence, re.I):
            continue
        incident = r"(?:outages?|disrupt\w*|offline|down|restor\w*|repair\w*|faults?|cuts?|breaks?|damag\w*)"
        name = re.escape(cable_name) if cable_name else "(?:" + "|".join(CABLE_PATTERNS) + ")"
        if re.search(rf"\b{name}\b[^.!?]{{0,80}}\b{incident}\b|\b{incident}\b[^.!?]{{0,80}}\b{name}\b", sentence, re.I):
            return True
    return False


def unnamed_event_rejection_reason(event: dict[str, Any]) -> str:
    if not parse_date(event.get("occurrence_date")):
        return "unnamed_missing_date"
    if not clean_text(event.get("affected_area")):
        return "unnamed_missing_area"
    # Evidence must come from the article, not only the model's generated reason.
    evidence = " ".join(clean_text(event.get(key)) for key in ("title", "original_text"))
    if not has_explicit_subsea_incident(evidence):
        return "unnamed_no_explicit_subsea_incident"
    return ""


def extract_location(text: str) -> str:
    match = re.search(r"\b(?:near|off|in|between)\s+([A-Z][A-Za-z .-]{2,80})", text)
    return clean_text(match.group(1)) if match else ""
