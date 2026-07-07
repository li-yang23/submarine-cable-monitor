import hashlib
import re
from datetime import date, datetime
from typing import Any, Optional


UNKNOWN_VALUES = {"", "nan", "none", "null", "unknown", "not specified", "notmentioned", "not mentioned"}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in UNKNOWN_VALUES:
        return ""
    return re.sub(r"\s+", " ", text)


def parse_date(value: Any) -> Optional[str]:
    text = clean_text(value)
    if not text or text.lower() in {"ongoing", "unmentioned", "unconfirmed"}:
        return None

    range_match = re.search(
        r"([A-Za-z]+)\s+\d{1,2}\s*[-–]\s*(\d{1,2}),?\s*(\d{4})",
        text,
    )
    if range_match:
        month, end_day, year = range_match.groups()
        text = f"{month} {end_day}, {year}"

    iso_match = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if iso_match:
        y, m, d = (int(part) for part in iso_match.groups())
        try:
            return date(y, m, d).isoformat()
        except ValueError:
            return None

    for fmt in (
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%B %Y",
        "%b %Y",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.date().isoformat()
        except ValueError:
            continue
    return None


def normalize_accident_type(value: Any, text: str = "") -> str:
    raw = clean_text(value).lower()
    haystack = f"{raw} {text.lower()}"
    if any(word in haystack for word in ("maintenance", "upgrade", "planned work")):
        return "Maintenance"
    if any(word in haystack for word in ("fluctuate", "slow", "degrad", "latency", "speed")):
        return "Fluctuate"
    if any(word in haystack for word in ("limit", "outage", "disrupt", "down", "unavailable")):
        return "Limit"
    if any(word in haystack for word in ("cut", "break", "fault", "damage", "sever")):
        return "Cut"
    return "Unknown"


def normalize_key(value: Any) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def event_uid(event: dict[str, Any]) -> str:
    parts = [
        normalize_key(event.get("url")),
        normalize_key(event.get("cable_name") or event.get("submarine_name")),
        normalize_key(event.get("occurrence_date")),
        normalize_key(event.get("accident_location") or event.get("location")),
    ]
    key = "|".join(part for part in parts if part)
    if not key:
        key = normalize_key(event.get("original_text") or event.get("description") or event.get("title"))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return f"evt_{digest[:20]}"


def tokenize(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", text.lower())
        if token not in {"the", "and", "for", "with", "that", "this", "from", "was", "were"}
    }


def text_similarity(left: str, right: str) -> float:
    left_tokens = tokenize(clean_text(left))
    right_tokens = tokenize(clean_text(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
