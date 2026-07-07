import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.processing.normalization import clean_text, event_uid, normalize_accident_type, parse_date, text_similarity


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

    def upsert(self, event: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        event = self.normalize_event(event)
        direct = self.find_by_uid(event["event_uid"])
        if direct:
            self.merge(direct, event)
            return False, direct

        duplicate = self.find_duplicate(event)
        if duplicate:
            self.merge(duplicate, event)
            return False, duplicate

        self.events.append(event)
        return True, event

    def find_by_uid(self, uid: str) -> dict[str, Any] | None:
        return next((event for event in self.events if event.get("event_uid") == uid), None)

    def find_duplicate(self, event: dict[str, Any], threshold: float = 0.72) -> dict[str, Any] | None:
        text = event.get("original_text") or event.get("title") or ""
        for existing in self.events:
            same_url = event.get("url") and event.get("url") == existing.get("url")
            same_cable_date = (
                event.get("cable_name")
                and existing.get("cable_name")
                and event.get("cable_name", "").lower() == existing.get("cable_name", "").lower()
                and event.get("occurrence_date")
                and event.get("occurrence_date") == existing.get("occurrence_date")
            )
            similar = text_similarity(text, existing.get("original_text") or existing.get("title") or "") >= threshold
            if same_url or same_cable_date or similar:
                return existing
        return None

    def merge(self, target: dict[str, Any], incoming: dict[str, Any]) -> None:
        for key, value in incoming.items():
            if key in {"sources", "evidence_urls", "raw_data"}:
                continue
            if not clean_text(target.get(key)) and clean_text(value):
                target[key] = value

        target["sources"] = sorted(set(target.get("sources", [])) | set(incoming.get("sources", [])))
        target["evidence_urls"] = sorted(set(target.get("evidence_urls", [])) | set(incoming.get("evidence_urls", [])))
        target["updated_at"] = utcnow()
        target["duplicate_count"] = int(target.get("duplicate_count", 0)) + 1

    def normalize_event(self, event: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        normalized = {
            "event_uid": clean_text(event.get("event_uid")),
            "cable_name": clean_text(event.get("cable_name") or event.get("submarine_name")),
            "normalized_cable_id": clean_text(event.get("normalized_cable_id") or event.get("id")),
            "accident_location": clean_text(event.get("accident_location") or event.get("location")),
            "reason": clean_text(event.get("reason") or event.get("accident_reason")),
            "accident_type": normalize_accident_type(event.get("accident_type"), event.get("original_text", "")),
            "affected_area": clean_text(event.get("affected_area")),
            "occurrence_date": parse_date(event.get("occurrence_date") or event.get("reported_at")),
            "repair_date": parse_date(event.get("repair_date") or event.get("resolved_at")),
            "published_date": parse_date(event.get("published_date")),
            "source": clean_text(event.get("source")),
            "url": clean_text(event.get("url") or event.get("title")),
            "title": clean_text(event.get("title")),
            "original_text": clean_text(event.get("original_text") or event.get("description")),
            "verification_status": clean_text(event.get("verification_status")) or "unverified",
            "raw_data": event.get("raw_data", {}),
            "created_at": clean_text(event.get("created_at")) or now,
            "updated_at": now,
        }
        normalized["event_uid"] = normalized["event_uid"] or event_uid(normalized)
        normalized["sources"] = sorted({normalized["source"]} if normalized["source"] else set())
        normalized["evidence_urls"] = sorted({normalized["url"]} if normalized["url"] else set())
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
            "cable_name",
            "accident_location",
            "reason",
            "accident_type",
            "affected_area",
            "occurrence_date",
            "repair_date",
            "published_date",
            "source",
            "verification_status",
            "url",
            "title",
            "original_text",
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
        if clean_text(row.get("title")).startswith("http") and not clean_text(row.get("url")):
            row["url"] = row["title"]
        return row


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
