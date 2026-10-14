"""Bounded retry-aware cache of successfully processed article URLs."""

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class ArticleState:
    def __init__(
        self,
        path: str = "data/article-state.json",
        refresh_days: int = 7,
        retention_days: int = 30,
        max_urls: int = 2000,
    ):
        self.path = Path(path)
        self.refresh = timedelta(days=refresh_days)
        self.retention = timedelta(days=retention_days)
        self.max_urls = max_urls
        self.processed_urls: dict[str, datetime] = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for url, timestamp in data["processed_urls"].items():
                    try:
                        processed_at = datetime.fromisoformat(timestamp)
                        if processed_at.tzinfo is not None:
                            self.processed_urls[url] = processed_at.astimezone(timezone.utc)
                    except (TypeError, ValueError):
                        continue
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                logging.getLogger(__name__).warning("Cannot read article state; articles will be retried.")
        self._prune()

    @classmethod
    def from_config(cls, config: Any) -> "ArticleState":
        return cls(
            path=config.article_state_path,
            refresh_days=config.article_state_refresh_days,
            retention_days=config.article_state_retention_days,
            max_urls=config.article_state_max_urls,
        )

    def is_processed(self, *urls: str | None) -> bool:
        now = datetime.now(timezone.utc)
        return any(
            url in self.processed_urls and timedelta(0) <= now - self.processed_urls[url] < self.refresh
            for url in urls if url
        )

    def mark_processed(self, *urls: str | None) -> None:
        now = datetime.now(timezone.utc)
        for url in urls:
            if url:
                self.processed_urls[url] = now

    def _prune(self) -> None:
        now = datetime.now(timezone.utc)
        recent = ((url, timestamp) for url, timestamp in self.processed_urls.items() if timedelta(0) <= now - timestamp < self.retention)
        self.processed_urls = dict(sorted(recent, key=lambda item: item[1], reverse=True)[: max(0, self.max_urls)])

    def save(self) -> None:
        """Call only after the event store is safely saved, never for a dry run."""
        self._prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "processed_urls": {url: timestamp.isoformat() for url, timestamp in sorted(self.processed_urls.items())}}
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
