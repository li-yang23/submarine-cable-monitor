import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.processing.normalization import (
    clean_text,
    event_uid,
    normalize_accident_type,
    normalize_cable_name,
    parse_date,
    text_similarity,
)
from src.processing.extractor import clean_translation


class EventStore:
    def __init__(self, path: str = "data/events.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.events: list[dict[str, Any]] = []
        self.load()

    def load(self) -> list[dict[str, Any]]:
        self.events = []
        if not self.path.exists():
            return self.events
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    self.events.append(json.loads(line))
        return self.events

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for event in self.events:
            event["evidence_urls"] = clean_url_list(event.get("evidence_urls", []))
        with self.path.open("w", encoding="utf-8") as handle:
            for event in sorted(self.events, key=lambda item: item.get("occurrence_date") or "", reverse=True):
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def import_history_csv(self, csv_path: str) -> int:
        imported = 0
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                event = self._normalize_history_row(row)
                inserted, _ = self.upsert(event)
                if inserted:
                    imported += 1
        self.save()
        return imported

    def upsert(self, event: dict[str, Any], duplicate_checker=None) -> tuple[bool, dict[str, Any]]:
        event = self.normalize_event(event)
        if should_drop_event(event):
            return False, event
        direct = self.find_by_uid(event["event_uid"])
        if direct:
            self.merge(direct, event)
            return False, direct

        duplicate = self.find_duplicate(event)
        if duplicate:
            if duplicate_checker and not duplicate_checker(event, duplicate):
                self.events.append(event)
                return True, event
            self.merge(duplicate, event)
            return False, duplicate

        self.events.append(event)
        return True, event

    def clean_events(self) -> dict[str, int]:
        before = len(self.events)
        normalized_events = [self.normalize_event(event) for event in self.events]
        kept: list[dict[str, Any]] = []
        dropped = 0
        merged = 0

        for event in normalized_events:
            if should_drop_event(event):
                dropped += 1
                continue
            duplicate = self._find_duplicate_in(event, kept)
            if duplicate:
                self.merge(duplicate, event)
                merged += 1
            else:
                kept.append(event)

        self.events = kept
        self.save()
        return {
            "before": before,
            "after": len(self.events),
            "dropped": dropped,
            "merged": merged,
        }

    def translate_missing(self, translator, limit: int | None = None) -> dict[str, int]:
        translated = 0
        skipped = 0
        failed = 0

        for event in self.events:
            if limit is not None and translated >= limit:
                break
            if clean_text(event.get("original_text_zh")):
                skipped += 1
                continue
            original_text = clean_text(event.get("original_text"))
            if not original_text:
                skipped += 1
                continue
            try:
                translated_text = clean_text(translator(original_text))
            except Exception:
                failed += 1
                continue
            if translated_text:
                event["original_text_zh"] = translated_text
                event["updated_at"] = utcnow()
                translated += 1
            else:
                skipped += 1

        if translated:
            self.save()
        return {"translated": translated, "skipped": skipped, "failed": failed}

    def purge_source(self, source: str) -> int:
        source = clean_text(source).lower()
        before = len(self.events)
        self.events = [
            event
            for event in self.events
            if clean_text(event.get("source")).lower() != source
            and source not in {clean_text(item).lower() for item in event.get("sources", [])}
        ]
        deleted = before - len(self.events)
        if deleted:
            self.save()
        return deleted

    def find_by_uid(self, uid: str) -> dict[str, Any] | None:
        return next((event for event in self.events if event.get("event_uid") == uid), None)

    def find_duplicate(self, event: dict[str, Any], threshold: float = 0.72) -> dict[str, Any] | None:
        return self._find_duplicate_in(event, self.events, threshold=threshold)

    def _find_duplicate_in(
        self,
        event: dict[str, Any],
        candidates: list[dict[str, Any]],
        threshold: float = 0.72,
    ) -> dict[str, Any] | None:
        text = event.get("original_text") or event.get("title") or ""
        cable = normalize_cable_name(event.get("cable_name") or event.get("submarine_name"))
        event_date = event.get("occurrence_date")
        for existing in candidates:
            same_url = event.get("url") and event.get("url") == existing.get("url")
            existing_cable = normalize_cable_name(existing.get("cable_name") or existing.get("submarine_name"))
            same_cable = cable and existing_cable and cable == existing_cable
            same_date = event_date and event_date == existing.get("occurrence_date")
            same_cable_date = (
                same_cable
                and same_date
            )
            same_cable_near_date = (
                same_cable
                and dates_within_days(event_date, existing.get("occurrence_date"), days=1)
                and event_text_similarity(event, existing) >= 0.12
            )
            dates_conflict = dates_are_distinct(event_date, existing.get("occurrence_date"), days=1)
            cables_conflict = bool(cable and existing_cable and cable != existing_cable)
            similar = (
                text_similarity(text, existing.get("original_text") or existing.get("title") or "") >= threshold
                and not dates_conflict
                and not cables_conflict
            )
            same_url_same_event = same_url and (same_cable_date or similar)
            if same_url_same_event or same_cable_date or same_cable_near_date or similar:
                return existing
        return None

    def merge(self, target: dict[str, Any], incoming: dict[str, Any]) -> None:
        for key, value in incoming.items():
            if key in {"sources", "evidence_urls", "raw_data"}:
                continue
            if not clean_text(target.get(key)) and clean_text(value):
                target[key] = value
            elif key in {"url", "discovered_url"} and is_better_url(value, target.get(key)):
                target[key] = value
            elif key in {"cable_name", "submarine_name"} and is_better_cable_name(value, target.get(key)):
                target[key] = value
            elif key == "original_text" and is_better_text(value, target.get(key)):
                target[key] = value
            elif key == "original_text_zh" and is_better_text(value, target.get(key)):
                target[key] = value

        target["sources"] = sorted(set(target.get("sources", [])) | set(incoming.get("sources", [])))
        target["evidence_urls"] = clean_url_list(set(target.get("evidence_urls", [])) | set(incoming.get("evidence_urls", [])))
        target["updated_at"] = utcnow()
        target["duplicate_count"] = int(target.get("duplicate_count", 0)) + 1

    def normalize_event(self, event: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        source = clean_text(event.get("source"))
        url = clean_text(event.get("url") or event.get("title"))
        evidence_urls = clean_url_list(event.get("evidence_urls", []))
        if is_google_news_source(source, event.get("sources", [])) and is_google_news_url(url):
            replacement_url = first_non_google_url(evidence_urls)
            if replacement_url:
                url = replacement_url
        normalized = {
            "event_uid": clean_text(event.get("event_uid")),
            "cable_name": clean_text(event.get("cable_name") or event.get("submarine_name")),
            "submarine_name": clean_text(event.get("submarine_name") or event.get("cable_name")),
            "normalized_cable_id": clean_text(event.get("normalized_cable_id") or event.get("id")),
            "accident_location": clean_text(event.get("accident_location") or event.get("location")),
            "reason": clean_text(event.get("reason") or event.get("accident_reason")),
            "accident_type": normalize_accident_type(event.get("accident_type"), event.get("original_text", "")),
            "affected_area": clean_text(event.get("affected_area")),
            "occurrence_date": parse_date(event.get("occurrence_date") or event.get("reported_at")),
            "repair_date": parse_date(event.get("repair_date") or event.get("resolved_at")),
            "published_date": parse_date(event.get("published_date")),
            "source": source,
            "url": url,
            "title": clean_text(event.get("title")),
            "original_text": clean_text(event.get("original_text") or event.get("description")),
            "original_text_zh": clean_translation(event.get("original_text_zh")),
            "verification_status": clean_text(event.get("verification_status")) or "unverified",
            "TrustWorthy": clean_text(event.get("TrustWorthy") or event.get("verification_status")) or "unverified",
            "discovered_url": clean_text(event.get("discovered_url")),
            "raw_data": event.get("raw_data", {}),
            "created_at": clean_text(event.get("created_at")) or now,
            "updated_at": now,
        }
        normalized["submarine_name"] = normalized["submarine_name"] or normalized["cable_name"]
        normalized["normalized_cable_id"] = normalized["normalized_cable_id"] or normalize_cable_name(normalized["cable_name"])
        normalized["event_uid"] = normalized["event_uid"] or event_uid(normalized)
        source_names = set()
        if normalized["source"]:
            source_names.add(normalized["source"])
        if isinstance(event.get("sources"), (list, tuple, set)):
            source_names |= {clean_text(item) for item in event.get("sources", []) if clean_text(item)}
        normalized["sources"] = sorted(source_names)
        normalized["evidence_urls"] = clean_url_list(set(evidence_urls) | ({normalized["url"]} if normalized["url"] else set()))
        return normalized

    def export_json(self, path: str) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        data = sorted(self.events, key=lambda item: item.get("occurrence_date") or item.get("published_date") or "", reverse=True)
        with output.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)

    def export_csv(self, path: str) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "event_uid",
            "submarine_name",
            "cable_name",
            "accident_location",
            "reason",
            "accident_type",
            "affected_area",
            "occurrence_date",
            "repair_date",
            "published_date",
            "source",
            "TrustWorthy",
            "verification_status",
            "url",
            "discovered_url",
            "title",
            "original_text",
            "original_text_zh",
        ]
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for event in self.events:
                writer.writerow({field: event.get(field, "") for field in fields})

    def _normalize_history_row(self, row: dict[str, Any]) -> dict[str, Any]:
        source = clean_text(row.get("source")) or clean_text(row.get("source_csv")) or "history"
        row = dict(row)
        row["source"] = source
        row["verification_status"] = clean_text(row.get("TrustWorthy")) or "imported"
        row["TrustWorthy"] = clean_text(row.get("TrustWorthy")) or "imported"
        if clean_text(row.get("title")).startswith("http") and not clean_text(row.get("url")):
            row["url"] = row["title"]
        return row


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_better_text(candidate: Any, current: Any) -> bool:
    candidate_text = clean_text(candidate)
    current_text = clean_text(current)
    if not candidate_text:
        return False
    current_lower = current_text.lower()
    noisy = any(marker in current_lower for marker in ("read more", "sign up", "straight to your inbox", "tags:"))
    return noisy or len(candidate_text) > len(current_text) + 80


def is_better_url(candidate: Any, current: Any) -> bool:
    candidate_url = clean_text(candidate)
    current_url = clean_text(current)
    if not candidate_url.startswith("http"):
        return False
    if not current_url:
        return True
    return is_google_news_url(current_url) and not is_google_news_url(candidate_url)


def is_better_cable_name(candidate: Any, current: Any) -> bool:
    candidate_text = clean_text(candidate)
    current_text = clean_text(current)
    if not candidate_text:
        return False
    if not current_text:
        return True
    return len(candidate_text) > len(current_text) and normalize_cable_name(candidate_text) == normalize_cable_name(current_text)


def should_drop_event(event: dict[str, Any]) -> bool:
    has_cable = bool(clean_text(event.get("cable_name") or event.get("submarine_name")))
    has_area = bool(clean_text(event.get("affected_area")))
    has_reason = bool(clean_text(event.get("reason")))
    if not has_cable:
        return True
    if not has_area and not has_reason:
        return True
    if is_google_news_source(event.get("source"), event.get("sources", [])) and is_google_news_url(event.get("url")):
        return True
    return False


def is_google_news_source(source: Any, sources: Any) -> bool:
    source_names = {clean_text(source).lower()}
    if isinstance(sources, (list, tuple, set)):
        source_names |= {clean_text(item).lower() for item in sources}
    return "google news" in source_names


def is_google_news_url(url: Any) -> bool:
    text = clean_text(url).lower()
    return "news.google.com/rss/articles" in text or "news.google.com/articles" in text


def first_non_google_url(urls: Any) -> str:
    for url in clean_url_list(urls):
        if not is_google_news_url(url):
            return url
    return ""


def dates_within_days(left: Any, right: Any, days: int) -> bool:
    left_date = parse_date(left)
    right_date = parse_date(right)
    if not left_date or not right_date:
        return False
    left_dt = datetime.fromisoformat(left_date)
    right_dt = datetime.fromisoformat(right_date)
    return abs((left_dt - right_dt).days) <= days


def dates_are_distinct(left: Any, right: Any, days: int) -> bool:
    left_date = parse_date(left)
    right_date = parse_date(right)
    if not left_date or not right_date:
        return False
    left_dt = datetime.fromisoformat(left_date)
    right_dt = datetime.fromisoformat(right_date)
    return abs((left_dt - right_dt).days) > days


def event_text_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_text = " ".join(clean_text(left.get(key)) for key in ("title", "original_text", "affected_area", "reason"))
    right_text = " ".join(clean_text(right.get(key)) for key in ("title", "original_text", "affected_area", "reason"))
    return text_similarity(left_text, right_text)


def clean_url_list(urls: Any) -> list[str]:
    cleaned = sorted({clean_text(url) for url in urls if clean_text(url).startswith("http")})
    result: list[str] = []
    for url in cleaned:
        if any(other != url and other.startswith(url) for other in cleaned):
            continue
        result.append(url)
    return result
