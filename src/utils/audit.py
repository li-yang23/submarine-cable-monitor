"""Small, append-only collection traces; never persist article bodies or LLM responses."""

import hashlib
import json
import os
import re
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4


_current: ContextVar["AuditRun | None"] = ContextVar("collection_audit", default=None)
_FIELDS = {
    "source", "title", "url", "discovered_url", "published_date", "query", "queries",
    "article_id", "reason", "error_type", "http_status", "entry_count", "content_length", "anchor_count",
    "candidate_count", "candidate_index", "accident_location", "evidence_excerpt", "date_evidence_excerpt", "claimed_occurrence_date", "event_uid", "candidate_uid", "cable_name",
    "occurrence_date", "method", "verification_method", "selected_sources", "since",
    "dry_run", "max_articles", "max_attempts", "attempts", "article_count", "path",
    "github_run_id", "github_run_attempt", "status", "errors", "warnings",
    "queue_id", "provider", "available", "low_balance", "model", "api_operation",
    "prompt_tokens", "completion_tokens", "total_tokens", "parent_queue_id", "content_sha256", "publisher_group", "retry_at",
}
_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
_SECRET = re.compile(r"(secret|password|passwd|token|api.?key|authorization|signature)", re.I)


def safe_url(value: Any) -> str:
    text = str(value or "")
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        query = [(key, "[redacted]" if _SECRET.search(key) else val) for key, val in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit((parts.scheme, host, parts.path, urlencode(query), ""))[:4096]
    except ValueError:
        return "[invalid URL]"


def error_details(exc: Exception) -> dict[str, Any]:
    """Exception messages can contain credentials or full HTTP/LLM response bodies."""
    details: dict[str, Any] = {"error_type": type(exc).__name__}
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None):
        details["http_status"] = response.status_code
    return details


def article_fields(article: dict[str, Any]) -> dict[str, Any]:
    fields = {key: article.get(key) for key in ("source", "title", "url", "discovered_url", "published_date", "queue_id") if article.get(key)}
    identity = article.get("discovered_url") or article.get("url") or article.get("title") or ""
    fields["article_id"] = hashlib.sha256(str(identity).encode()).hexdigest()[:20]
    if article.get("keyword"):
        fields["query"] = article["keyword"]
    return fields


def emit(stage: str, outcome: str, **fields: Any) -> None:
    audit = _current.get()
    if audit:
        audit.emit(stage, outcome, **fields)


class AuditRun:
    def __init__(self, directory: str | Path = "logs/runs"):
        self.root = Path(directory)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = f"{timestamp}-{uuid4().hex[:8]}"
        self.path = self.root / self.run_id
        self.path.mkdir(parents=True, exist_ok=True)
        self._handle = (self.path / "audit.jsonl").open("a", encoding="utf-8")
        self.counts: Counter[str] = Counter()
        self.source_counts: Counter[str] = Counter()
        self.failures = 0
        self._failure_keys: set[tuple[str, str, str]] = set()
        self.token = None
        self.usage_responses = 0
        self.usage_responses_with_usage = 0
        self.usage_tokens: Counter[str] = Counter()
        self.usage_reported: Counter[str] = Counter()

    def activate(self) -> None:
        self.token = _current.set(self)

    def emit(self, stage: str, outcome: str, **fields: Any) -> None:
        record = {"schema_version": 1, "run_id": self.run_id, "timestamp": datetime.now(timezone.utc).isoformat(), "stage": stage, "outcome": outcome}
        for key, value in fields.items():
            if key not in _FIELDS or value is None:
                continue
            if key in _TOKEN_FIELDS and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                continue
            if key in {"url", "discovered_url"}:
                value = safe_url(value)
            elif isinstance(value, str):
                value = value[:1000]
            record[key] = value
        self._handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._handle.flush()
        reason = fields.get("reason")
        self.counts[f"{stage}.{outcome}" + (f".{reason}" if reason else "")] += 1
        if fields.get("source"):
            self.source_counts[f"{fields['source']}:{stage}.{outcome}"] += 1
        if outcome in {"failed", "fallback"}:
            group = ("source_discovery" if stage in {"discover", "discovery_seed", "cable_page", "rss", "source"}
                     else "article_fetch" if stage in {"fetch", "resolve"}
                     else "model" if stage in {"extract", "extraction", "model_health"} else stage)
            source = str(fields.get("source", ""))
            identity = str(fields.get("url") or fields.get("discovered_url") or fields.get("article_id") or "")
            # Lower-layer diagnostics and their propagated error are one problem.
            # A source-wide summary adds no extra failure to its failed requests.
            matching = {key for key in self._failure_keys if key[:2] == (group, source)}
            if identity:
                self._failure_keys.discard((group, source, ""))
            if identity or not matching:
                self._failure_keys.add((group, source, identity))
            self.failures = len(self._failure_keys)
        if stage == "model_usage":
            self.usage_responses += 1
            present = [key for key in _TOKEN_FIELDS if key in record]
            self.usage_responses_with_usage += int(bool(present))
            for key in present:
                self.usage_tokens[key] += record[key]
                self.usage_reported[key] += 1

    def finish(self, summary: dict[str, Any]) -> dict[str, Any]:
        self._handle.close()
        if self.token is not None:
            _current.reset(self.token)
        usage = {"responses": self.usage_responses, "responses_with_usage": self.usage_responses_with_usage,
                 **{key: self.usage_tokens[key] if self.usage_reported[key] else None for key in _TOKEN_FIELDS},
                 "reported_responses": {key: self.usage_reported[key] for key in _TOKEN_FIELDS}}
        failure_summary = {}
        for group, source, identity in sorted(self._failure_keys):
            item = failure_summary.setdefault(group, {"count": 0, "sources": {}})
            item["count"] += 1
            item["sources"][source or "unknown"] = item["sources"].get(source or "unknown", 0) + 1
        data = {"schema_version": 1, "failure_summary": failure_summary, "run_id": self.run_id, "github_run_id": os.getenv("GITHUB_RUN_ID"), "github_run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"), **summary, "model_usage": usage, "counts": dict(sorted(self.counts.items())), "source_counts": dict(sorted(self.source_counts.items())), "audit_file": str(self.path / "audit.jsonl")}
        markdown = f"## Collection audit — {self.run_id}\n\nStatus: **{data.get('status', 'unknown')}**\n\n"
        markdown += " | ".join(f"{key}: {data.get(key, 0)}" for key in ("articles", "candidates", "inserted", "merged", "rejected", "errors", "warnings")) + "\n\n"
        markdown += "Queue: " + json.dumps(data.get("queue", {}), ensure_ascii=False) + "\n\n"
        markdown += "Model health: " + json.dumps(data.get("model_health", {}), ensure_ascii=False) + "\n\n"
        markdown += "Model usage (provider-reported tokens only; null means unavailable): " + json.dumps(usage, ensure_ascii=False) + "\n\n"
        markdown += "| Stage / outcome / reason | Count |\n| --- | ---: |\n"
        markdown += "".join(f"| {key} | {value} |\n" for key, value in sorted(self.counts.items()))
        markdown += "\nSearch this run's audit.jsonl by article title, URL, article_id, or event_uid; bodies and model responses are omitted.\n"
        for filename, content in (("summary.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n"), ("summary.md", markdown)):
            (self.path / filename).write_text(content, encoding="utf-8")
            (self.root / f"latest-{filename}").write_text(content, encoding="utf-8")
        return data


def search_audit(directory: str | Path, query: str, run_id: str | None = None) -> Iterator[dict[str, Any]]:
    """Match title/URL/ID/decision; stream results so large archives stay inexpensive."""
    root = Path(directory)
    needle = query.casefold()
    for path in sorted(root.glob("*/audit.jsonl"), reverse=True):
        if run_id and path.parent.name != run_id:
            continue
        article_ids = set()
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if needle in line.casefold():
                    try:
                        record = json.loads(line)
                        if record.get("article_id"):
                            article_ids.add(record["article_id"])
                    except json.JSONDecodeError:
                        continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    if needle in line.casefold() or record.get("article_id") in article_ids:
                        yield record
                except json.JSONDecodeError:
                    continue  # An interrupted final append must not hide the rest of a run.
