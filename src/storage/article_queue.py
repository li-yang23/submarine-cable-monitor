"""Durable article metadata. Discovered work survives limits and feed expiry."""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArticleQueue:
    """No TTL or size eviction: completed URLs remain deduplicated indefinitely.

    Only discovery and explicit persist write to disk. Consumers commit done/review
    transitions after their event store is safely saved. An empty path is in-memory.
    """

    metadata_fields = {"source", "title", "url", "discovered_url", "published_date", "keyword", "queries", "cable_name", "initial_since"}

    def __init__(self, path: str = "data/article-queue.json", dry_run: bool = False):
        self.path = Path(path) if path else None
        self.dry_run = dry_run
        self.items: dict[str, dict[str, Any]] = {}
        self.cursors: dict[str, Any] = {}
        if self.path and self.path.exists():
            # Fail closed on a corrupt queue; overwriting it would silently lose work.
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("items"), dict):
                raise ValueError("Invalid article queue format; restore its last good copy")
            self.items = data["items"]
            self.cursors = data.get("cursors", {})

    @classmethod
    def from_config(cls, config: Any, dry_run: bool = False) -> "ArticleQueue":
        return cls(getattr(config, "article_queue_path", "data/article-queue.json"), dry_run=dry_run)

    @staticmethod
    def item_id(item: dict[str, Any]) -> str:
        if item.get("queue_id"):
            return str(item["queue_id"])
        url = item.get("discovered_url") or item.get("url") or ""
        return hashlib.sha256(f"{item.get('source', '')}\n{url}".encode()).hexdigest()[:32]

    def get(self, item: dict[str, Any] | str) -> dict[str, Any] | None:
        if isinstance(item, dict):
            found = self.items.get(self.item_id(item))
            if found:
                return found
            urls = {item.get("discovered_url"), item.get("url")} - {None, ""}
        else:
            if item in self.items:
                return self.items[item]
            urls = {item}
        return next((row for row in self.items.values() if urls.intersection({row.get("url"), row.get("discovered_url")})), None)

    def enqueue(self, item: dict[str, Any]) -> dict[str, Any]:
        if not (item.get("discovered_url") or item.get("url")):
            raise ValueError("Article queue requires a URL")
        key = self.item_id(item)
        now = utcnow().isoformat()
        row = self.items.setdefault(key, {"queue_id": key, "status": "pending", "first_seen_at": now, "attempts": 0})
        for name in self.metadata_fields:
            if name == "initial_since" and name in row:
                continue
            value = item.get(name)
            if value is not None and value != "":
                if name == "queries":
                    value = list(dict.fromkeys([*row.get("queries", []), *value]))
                row[name] = value
        row["last_seen_at"] = now
        self.persist()
        return dict(row)

    def update_item(self, item: dict[str, Any] | str, **metadata: Any) -> None:
        row = self.get(item)
        if row:
            row.update({k: v for k, v in metadata.items() if k in self.metadata_fields and v is not None})

    def due(self, source: str | None = None) -> list[dict[str, Any]]:
        now = utcnow().isoformat()
        return [dict(row) for row in sorted(self.items.values(), key=lambda row: row["first_seen_at"])
                if row["status"] in {"pending", "retry"}
                and (not source or row.get("source") == source)
                and (not row.get("next_attempt_at") or row["next_attempt_at"] <= now)]

    def is_done(self, *urls: str | None) -> bool:
        candidates = set(urls) - {None, ""}
        return any(row["status"] == "done" and row.get("reason") != "outside_initial_window"
                   and candidates.intersection({row.get("url"), row.get("discovered_url")}) for row in self.items.values())

    def mark_attempt(self, item: dict[str, Any] | str) -> None:
        row = self.get(item)
        if row:
            row["attempts"] += 1
            row["last_attempt_at"] = utcnow().isoformat()

    def mark_retry(self, item: dict[str, Any] | str, reason: str, *, error_type: str = "", http_status: int | None = None, delay_seconds: float | None = None) -> None:
        row = self.get(item)
        if not row:
            return
        if delay_seconds is None:
            delay_seconds = min(86400, 300 * 2 ** min(max(row["attempts"] - 1, 0), 9))
        row.update(status="retry", reason=reason, error_type=error_type, http_status=http_status,
                   next_attempt_at=(utcnow() + timedelta(seconds=delay_seconds)).isoformat())

    def mark_done(self, item: dict[str, Any] | str, reason: str = "processed") -> None:
        row = self.get(item)
        if row:
            row.update(status="done", reason=reason, completed_at=utcnow().isoformat(), next_attempt_at=None, force_retry=False)

    def mark_review(self, item: dict[str, Any] | str, reason: str) -> None:
        row = self.get(item)
        if row:
            row.update(status="review", reason=reason, review_at=utcnow().isoformat(), next_attempt_at=None, force_retry=False)

    def requeue(self, item: dict[str, Any] | str, reason: str = "manual_retry") -> bool:
        row = self.get(item)
        if not row:
            return False
        row.update(status="pending", reason=reason, next_attempt_at=None, requeued_at=utcnow().isoformat(), force_retry=True)
        return True

    def counts(self) -> dict[str, int]:
        return {status: sum(row["status"] == status for row in self.items.values()) for status in ("pending", "retry", "done", "review")}

    def stats(self) -> dict[str, Any]:
        pending = [row for row in self.items.values() if row["status"] in {"pending", "retry"}]
        oldest = min((datetime.fromisoformat(row["first_seen_at"]) for row in pending), default=utcnow())
        return {**self.counts(), "due": len(self.due()), "oldest_pending_age_hours": round(max(0, (utcnow() - oldest).total_seconds()) / 3600, 2)}

    def get_cursor(self, source: str, default: Any = None) -> Any:
        return self.cursors.get(source, default)

    def set_cursor(self, source: str, value: Any) -> None:
        self.cursors[source] = value
        self.persist()

    def persist(self) -> None:
        if self.dry_run or not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "items": self.items, "cursors": self.cursors}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
