import email.utils
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from src.processing.normalization import clean_text, parse_date
from src.storage.article_state import ArticleState
from src.storage.article_queue import ArticleQueue
from src.utils import Config, get_logger
from src.utils.audit import article_fields, emit, error_details


class ArticleScraper:
    source_name = "article"

    def __init__(self, config: Config):
        self.config = config
        self.article_state = ArticleState.from_config(config)
        self.article_queue = ArticleQueue.from_config(config)
        self.fetch_enabled = True
        self.deadline: float | None = None
        self.logger = get_logger(self.__class__.__name__)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            }
        )

    def scrape_articles(self, since: str | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _prepare_queue(self, dry_run: bool, since: str | None = None) -> None:
        self.article_queue.dry_run = dry_run
        self.discovery_since = since or (datetime.now(timezone.utc).date() - timedelta(days=self.config.google_news_lookback_days)).isoformat()
        if self.source_name != "Google News":
            coverage_key = f"coverage_since:{self.source_name}"
            coverage_since = self.article_queue.get_cursor(coverage_key)
            if not coverage_since:
                self.article_queue.set_cursor(coverage_key, self.discovery_since)
            elif not since:
                self.discovery_since = min(self.discovery_since, coverage_since)

    def _already_processed(self, item: dict[str, Any] | str, *urls: str) -> bool:
        row = self.article_queue.get(item)
        if row and row.get("force_retry"):
            return False
        return self.article_queue.is_done(*urls) or self.article_state.is_processed(*urls)

    def _expired(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def _discovery_expired(self, started: float) -> bool:
        # Leave most of the run for body processing instead of scanning archives.
        discovery_seconds = min(120, max(1, self.config.collection_time_budget_seconds / 4))
        return self._expired() or time.monotonic() - started >= discovery_seconds

    def _enqueue(self, item: dict[str, Any]) -> dict[str, Any]:
        item = {"source": self.source_name, "initial_since": self.discovery_since, **item}
        emit("discover", "found", **article_fields(item))
        return self.article_queue.enqueue(item)

    def iter_queued_articles(self, dry_run: bool = False):
        self._prepare_queue(dry_run)
        max_articles = 3 if dry_run else (self.config.subtelforum_max_articles if self.source_name == "SubTel Forum" else 0)
        per_cable = (1 if dry_run else self.config.submarine_networks_articles_per_cable) if self.source_name == "Submarine Networks" else 0
        article_count = 0
        cable_counts: dict[str, int] = {}
        for item in self.article_queue.due(self.source_name):
            fields = article_fields(item)
            url = item.get("discovered_url") or item["url"]
            if self._already_processed(item, url):
                self.article_queue.mark_done(item, reason="already_processed")
                self.article_queue.persist()
                emit("select", "skipped", **fields, reason="already_processed")
                continue
            cable = item.get("cable_name", "")
            reason = ("discovery_only" if not self.fetch_enabled else "time_budget" if self._expired()
                      else "article_budget" if max_articles > 0 and article_count >= max_articles
                      else "article_budget" if per_cable > 0 and cable_counts.get(cable, 0) >= per_cable else "")
            if reason:
                emit("select", "deferred", **fields, reason=reason)
                continue
            self.article_queue.mark_attempt(item)
            self.article_queue.persist()
            emit("select", "selected", **fields)
            try:
                article = self._fetch_article(url)
                if not clean_text(article.get("content")):
                    raise ValueError("empty_article_content")
            except Exception as exc:
                details = error_details(exc)
                emit("fetch", "failed", **fields, reason="article_fetch_error", **details)
                self.article_queue.mark_retry(item, reason="article_fetch_error", **details)
                self.article_queue.persist()
                continue
            article.update(queue_id=item["queue_id"], discovered_url=url)
            if not item.get("force_retry") and item.get("initial_since") and article.get("published_date") and article["published_date"] < item["initial_since"]:
                self.article_queue.mark_done(item, reason="outside_initial_window")
                self.article_queue.persist()
                emit("select", "skipped", **article_fields(article), reason="outside_initial_window")
                continue
            if cable:
                article["cable_name"] = cable
            self.article_queue.update_item(item, url=article["url"], title=article.get("title"), published_date=article.get("published_date"))
            self.article_queue.persist()
            emit("fetch", "completed", **article_fields(article), content_length=len(article.get("content", "")))
            article_count += 1
            cable_counts[cable] = cable_counts.get(cable, 0) + 1
            yield article

    def get(self, url: str) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(int(getattr(self.config, "max_retries", 3))):
            try:
                response = self.session.get(url, timeout=self.config.request_timeout)
                response.raise_for_status()
                return response
            except Exception as exc:
                last_error = exc
                if attempt < int(getattr(self.config, "max_retries", 3)) - 1:
                    time.sleep(1 + attempt)
        raise last_error or RuntimeError(f"Failed to fetch {url}")

    def extract_page_text(self, url: str) -> dict[str, Any]:
        response = self.get(url)
        soup = BeautifulSoup(response.text, "html.parser")
        title = clean_text((soup.select_one("h1") or soup.select_one("title") or soup).get_text(" "))
        published = extract_date_from_soup(soup)
        return {
            "title": title,
            "content": article_body_text(soup),
            "published_date": published,
            "final_url": response.url,
        }


class GoogleNewsArticleScraper(ArticleScraper):
    source_name = "Google News"
    keywords = [
        "submarine cable fault",
        "submarine cable outage",
        "submarine cable break",
        "undersea cable damage",
        "subsea cable disruption",
        "undersea cable outage",
        "海缆故障",
        "海底电缆故障",
    ]

    def __init__(self, config: Config):
        super().__init__(config)
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                )
            }
        )

    def get(self, url: str) -> requests.Response:
        response = self.session.get(url, timeout=(5, 10), allow_redirects=True)
        response.raise_for_status()
        return response

    def scrape_articles(self, since: str | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
        self._prepare_queue(dry_run, since)
        requested_since = since
        since = self.discovery_since
        keywords = self.config.google_news_keywords or self.keywords
        queues: dict[str, list[dict[str, Any]]] = {}
        seen: dict[str, dict[str, Any]] = {}
        successful_feeds = 0
        # Discovery is independent of the body-fetch budget: later keywords always run.
        for keyword in keywords:
            last_discovered = self.article_queue.get_cursor(f"google_query:{keyword}")
            query_since = since if requested_since or not last_discovered else min(since, last_discovered)
            search_query = f"{keyword} after:{query_since}"
            url = f"https://news.google.com/rss/search?q={quote(search_query)}&hl=en-US&gl=US&ceid=US:en"
            self.logger.info("Fetching Google News RSS for %s", keyword)
            try:
                response = self.get(url)
                entries = parse_rss_entries(response.content)
            except Exception as exc:
                emit("rss", "failed", source=self.source_name, query=keyword, url=url, reason="rss_parse_error" if isinstance(exc, (ValueError, ElementTree.ParseError)) else "rss_fetch_error", **error_details(exc))
                self.logger.warning("Google News RSS failed for %s: %s", keyword, type(exc).__name__)
                continue
            successful_feeds += 1
            emit("rss", "completed", source=self.source_name, query=keyword, entry_count=len(entries), since=query_since)
            truncated = len(entries) >= 100
            if truncated:
                emit("rss", "failed", source=self.source_name, query=keyword, entry_count=len(entries), reason="possible_feed_truncation")
            queues[keyword] = []
            for entry in entries:
                item = {"source": self.source_name, "title": clean_text(entry.get("title")), "discovered_url": clean_text(entry.get("link")), "published_date": entry.get("published_date"), "keyword": keyword}
                fields = article_fields(item)
                emit("discover", "found", **fields)
                if not item["discovered_url"]:
                    emit("select", "skipped", **fields, reason="missing_url")
                    continue
                if item["published_date"] and item["published_date"] < query_since:
                    emit("select", "skipped", **fields, reason="before_since")
                    continue
                key = item["discovered_url"]
                if key in seen:
                    seen[key]["queries"].append(keyword)
                    self.article_queue.enqueue(seen[key])
                    emit("select", "skipped", **fields, reason="duplicate_discovery")
                    continue
                item["queries"] = [keyword]
                item["initial_since"] = query_since
                self.article_queue.enqueue(item)
                seen[key] = item
                queues[keyword].append(item)
            queues[keyword].sort(key=lambda item: item.get("published_date") or "", reverse=True)
            if not truncated:
                self.article_queue.set_cursor(f"google_query:{keyword}", datetime.now(timezone.utc).date().isoformat())
        if not successful_feeds and not self.article_queue.due(self.source_name):
            raise RuntimeError("All Google News RSS queries failed")

        # Each query gets a turn; within a query fetch newest first. Date filtering and
        # duplicate RSS hits do not consume attempts. Every remainder has a reason.
        ordered: list[dict[str, Any]] = []
        while any(queues.values()):
            for queue in queues.values():
                if queue:
                    ordered.append(queue.pop(0))
        self._discovered_order = ordered
        return list(self.iter_queued_articles(dry_run=dry_run))

    def iter_queued_articles(self, dry_run: bool = False):
        self._prepare_queue(dry_run)
        max_articles = 3 if dry_run else int(self.config.google_news_max_articles)
        max_attempts = 8 if dry_run else int(self.config.google_news_max_attempts)
        article_count = 0
        ordered = getattr(self, "_discovered_order", [])
        due = self.article_queue.due(self.source_name)
        due_ids = {row["queue_id"] for row in due}
        discovered_ids = {self.article_queue.item_id(item) for item in ordered}
        # Older unfinished work goes first; today's discovery remains fair by query.
        ordered = [item for item in due if item["queue_id"] not in discovered_ids] + [
            self.article_queue.get(item) for item in ordered
            if self.article_queue.item_id(item) in due_ids]
        attempts = 0
        real_urls: set[str] = set()
        for item in ordered:
            fields = article_fields(item)
            discovered_url = item.get("discovered_url") or item["url"]
            if self._already_processed(item, discovered_url):
                self.article_queue.mark_done(item, reason="already_processed")
                self.article_queue.persist()
                emit("select", "skipped", **fields, queries=item.get("queries", []), reason="already_processed")
                continue
            reason = ("discovery_only" if not self.fetch_enabled else "time_budget" if self._expired()
                      else "article_budget" if max_articles > 0 and article_count >= max_articles
                      else "attempt_budget" if max_attempts > 0 and attempts >= max_attempts else "")
            if reason:
                emit("select", "deferred", **fields, queries=item.get("queries", []), reason=reason)
                continue
            attempts += 1
            self._last_fetch_already_processed = False
            self._last_fetch_failure = {}
            self.article_queue.mark_attempt(item)
            self.article_queue.persist()
            emit("select", "selected", **fields, queries=item.get("queries", []))
            try:
                article = self._fetch_news_article(discovered_url, item.get("title", ""), item.get("published_date"), item.get("keyword", "manual"))
            except Exception as exc:
                emit("fetch", "failed", **fields, reason="article_fetch_error", **error_details(exc))
                self.article_queue.mark_retry(item, "article_fetch_error", **error_details(exc))
                self.article_queue.persist()
                continue
            if self._last_fetch_already_processed:
                attempts -= 1
                self.article_queue.mark_done(item, reason="already_processed")
                self.article_queue.persist()
                continue
            if article:
                article["queue_id"] = item["queue_id"]
                article["discovered_url"] = discovered_url
                self.article_queue.update_item(item, url=article["url"])
                self.article_queue.persist()
                if article["url"] in real_urls:
                    self.article_queue.mark_retry(item, "duplicate_final_url", delay_seconds=0)
                    self.article_queue.persist()
                    emit("select", "deferred", **article_fields(article), reason="duplicate_final_url")
                    continue
                real_urls.add(article["url"])
                article_count += 1
                yield article
            else:
                failure = self._last_fetch_failure or {"reason": "article_unavailable"}
                self.article_queue.mark_retry(item, **failure)
                self.article_queue.persist()
        emit("collection", "completed", source=self.source_name, attempts=attempts, article_count=article_count, max_articles=max_articles, max_attempts=max_attempts)

    def _fetch_news_article(
        self,
        discovered_url: str,
        rss_title: str,
        published: str | None,
        keyword: str,
    ) -> dict[str, Any] | None:
        fields = article_fields({"source": self.source_name, "title": rss_title, "discovered_url": discovered_url, "published_date": published, "keyword": keyword})
        queue_ref = {"source": self.source_name, "discovered_url": discovered_url}
        real_url = self._resolve_google_news_url(discovered_url)
        if not real_url:
            self._last_fetch_failure = {"reason": "google_news_decode_failed"}
            emit("resolve", "failed", **fields, reason="google_news_decode_failed")
            return None
        fields["url"] = real_url
        emit("resolve", "completed", **fields)
        self.article_queue.update_item(queue_ref, url=real_url)
        if self._already_processed(queue_ref, real_url):
            self._last_fetch_already_processed = True
            emit("select", "skipped", **fields, reason="already_processed")
            return None
        try:
            page = self.extract_page_text(real_url)
        except Exception as exc:
            self._last_fetch_failure = {"reason": "article_fetch_error", **error_details(exc)}
            emit("fetch", "failed", **fields, reason="article_fetch_error", **error_details(exc))
            self.logger.warning("Skipping Google News article %s: %s", fields["article_id"], type(exc).__name__)
            return None
        content = clean_text(page.get("content"))
        if len(content) < 300:
            self._last_fetch_failure = {"reason": "short_content"}
            emit("fetch", "skipped", **fields, reason="short_content", content_length=len(content))
            return None
        article = {
            "source": self.source_name,
            "url": clean_text(page.get("final_url") or real_url),
            "discovered_url": discovered_url,
            "title": clean_text(page.get("title")) or rss_title,
            "content": content,
            "published_date": page.get("published_date") or published,
            "keyword": keyword,
        }
        if self._already_processed(queue_ref, article["url"]):
            self._last_fetch_already_processed = True
            emit("select", "skipped", **article_fields(article), reason="already_processed")
            return None
        emit("fetch", "completed", **article_fields(article), content_length=len(content))
        return article

    def _resolve_google_news_url(self, url: str) -> str:
        parsed = urlparse(clean_text(url))
        if "news.google." not in parsed.netloc:
            return url

        query_url = resolve_google_news_url(url)
        if query_url and query_url != url:
            return query_url

        try:
            response = self.session.get(url, timeout=(5, 8), allow_redirects=False)
            if response.is_redirect and response.headers.get("location"):
                response = self.session.get(response.headers["location"], timeout=(5, 8), allow_redirects=False)
            response.raise_for_status()
            html = response.text
            article_id_match = re.search(r'data-n-a-id="([^"]+)"', html)
            timestamp_match = re.search(r'data-n-a-ts="([^"]+)"', html)
            signature_match = re.search(r'data-n-a-sg="([^"]+)"', html)
            if not article_id_match or not timestamp_match or not signature_match:
                return ""
            article_id = article_id_match.group(1)
            timestamp = timestamp_match.group(1)
            signature = signature_match.group(1)
            request_payload = [
                "garturlreq",
                [
                    [
                        "en-US",
                        "US",
                        ["FINANCE_TOP_INDICES", "WEB_TEST_1_0_0"],
                        None,
                        None,
                        1,
                        1,
                        "US:en",
                        None,
                        180,
                        None,
                        None,
                        None,
                        None,
                        None,
                        0,
                        None,
                        None,
                        [1608992183, 723341000],
                    ],
                    "en-US",
                    "US",
                    1,
                    [2, 3, 4, 8],
                    1,
                    0,
                    "655000234",
                    0,
                    0,
                    None,
                    0,
                ],
                article_id,
                int(timestamp),
                signature,
            ]
            f_req = json.dumps(
                [[["Fbv4je", json.dumps(request_payload, separators=(",", ":")), None, "generic"]]],
                separators=(",", ":"),
            )
            decoded = self.session.post(
                "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je",
                data={"f.req": f_req},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "Referer": url,
                    "User-Agent": self.session.headers.get("User-Agent", ""),
                },
                timeout=self.config.request_timeout,
            )
            decoded.raise_for_status()
            urls = re.findall(r'https?://[^\\\"\]]+', decoded.text)
            for candidate in urls:
                if "news.google." not in candidate and "google.com" not in candidate:
                    return candidate.replace("\\u003d", "=").replace("\\u0026", "&")
            return ""
        except Exception as exc:
            self.logger.warning("Unable to decode Google News URL: %s", type(exc).__name__)
            return ""


class SubTelForumScraper(ArticleScraper):
    source_name = "SubTel Forum"
    start_url = "https://subtelforum.com/category/cable-faults-maintenance/"
    feed_url = "https://subtelforum.com/category/cable-faults-maintenance/feed/"

    def scrape_articles(self, since: str | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
        self._prepare_queue(dry_run, since)
        try:
            self._discover_urls(since=since, dry_run=dry_run)
        except Exception as exc:
            emit("discover", "failed", source=self.source_name, reason="source_discovery_error", **error_details(exc))
        return list(self.iter_queued_articles(dry_run=dry_run))

    def _discover_urls(self, since: str | None, dry_run: bool) -> list[str]:
        urls: list[str] = []
        next_url = self.start_url
        resume_url = self.article_queue.get_cursor(self.source_name)
        max_pages = 1 if dry_run else int(self.config.subtelforum_max_pages)
        page_budget = max_pages + int(bool(resume_url and resume_url != self.start_url)) if max_pages else 0
        visited: set[str] = set()
        started = time.monotonic()
        while next_url and next_url not in visited:
            if (page_budget > 0 and len(visited) >= page_budget) or self._discovery_expired(started):
                self.article_queue.set_cursor(self.source_name, next_url)
                emit("discover", "deferred", source=self.source_name, url=next_url, reason="discovery_budget")
                break
            visited.add(next_url)
            self.logger.info("Discovering SubTel Forum articles from %s", next_url)
            try:
                soup = BeautifulSoup(self.get(next_url).text, "html.parser")
            except Exception as exc:
                emit("discover", "fallback", source=self.source_name, url=next_url, reason="category_fetch_error", **error_details(exc))
                self.logger.warning("SubTel Forum category page failed, falling back to feed: %s", type(exc).__name__)
                self.article_queue.set_cursor(self.source_name, next_url)
                return self._discover_urls_from_feed(max_articles=0)
            overlaps_known = False
            for link in soup.select("h2 a, h3 a, h4 a, .fusion-title a"):
                href = clean_text(link.get("href"))
                title = clean_text(link.get_text(" "))
                if not href or "subtelforum.com" not in href:
                    continue
                if title and href not in urls:
                    overlaps_known = overlaps_known or self.article_queue.get({"source": self.source_name, "url": href}) is not None
                    self._enqueue({"url": href, "title": title})
                    urls.append(href)

            next_link = soup.select_one("a.pagination-next, .pagination-next a, a[rel='next']")
            if not next_link or not next_link.get("href"):
                self.article_queue.set_cursor(self.source_name, None)
                break
            if overlaps_known and resume_url and resume_url not in visited:
                next_url = resume_url
                resume_url = None
            else:
                next_url = urljoin(next_url, next_link["href"])
            self.article_queue.set_cursor(self.source_name, next_url)
        return urls

    def _discover_urls_from_feed(self, max_articles: int) -> list[str]:
        response = self.get(self.feed_url)
        urls: list[str] = []
        for entry in parse_rss_entries(response.content):
            href = clean_text(entry.get("link"))
            if href and "subtelforum.com" in href and href not in urls:
                self._enqueue({"url": href, "title": entry.get("title"), "published_date": entry.get("published_date")})
                urls.append(href)
        return urls

    def _fetch_article(self, url: str) -> dict[str, Any]:
        soup = BeautifulSoup(self.get(url).text, "html.parser")
        title = clean_text((soup.select_one("h1") or soup.select_one("title") or soup).get_text(" "))
        published = extract_date_from_soup(soup)
        return {
            "source": self.source_name,
            "url": url,
            "title": title,
            "content": article_body_text(soup),
            "published_date": published,
        }


class SubmarineNetworksScraper(ArticleScraper):
    source_name = "Submarine Networks"

    def __init__(self, config: Config):
        super().__init__(config)
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.submarinenetworks.com/",
            }
        )

    def scrape_articles(self, since: str | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
        self._prepare_queue(dry_run, since)
        links = self._load_cable_links()
        max_cables = 3 if dry_run else int(self.config.submarine_networks_max_cables)
        cursor = int(self.article_queue.get_cursor(self.source_name, 0) or 0) % max(1, len(links))
        started = time.monotonic()
        for offset in range(len(links)):
            index = (cursor + offset) % len(links)
            if (max_cables > 0 and offset >= max_cables) or self._discovery_expired(started):
                self.article_queue.set_cursor(self.source_name, index)
                emit("discover", "deferred", source=self.source_name, reason="discovery_budget")
                break
            cable = links[index]
            cable_url = clean_text(cable.get("href"))
            cable_name = clean_text(cable.get("cable_name"))
            if not cable_url:
                self.article_queue.set_cursor(self.source_name, (index + 1) % len(links))
                continue
            try:
                discovered_urls = self._discover_article_urls(cable_url)
            except Exception as exc:
                emit("discover", "failed", source=self.source_name, url=cable_url, reason="cable_page_fetch_error", **error_details(exc))
                self.logger.warning("Skipping cable page: %s", type(exc).__name__)
                # Retry this cable on the next traversal; failures are never removed.
                self.article_queue.set_cursor(self.source_name, (index + 1) % len(links))
                continue
            for url in discovered_urls:
                self._enqueue({"url": url, "cable_name": cable_name})
            self.article_queue.set_cursor(self.source_name, (index + 1) % len(links))
        return list(self.iter_queued_articles(dry_run=dry_run))

    def _load_cable_links(self) -> list[dict[str, Any]]:
        path = Path(getattr(self.config, "submarine_networks_links_path", "data/cable-links.json"))
        if not path.exists():
            self.logger.warning("Missing Submarine Networks cable link seed file: %s", path)
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def _discover_article_urls(self, cable_url: str) -> list[str]:
        soup = BeautifulSoup(self.get(cable_url).text, "html.parser")
        urls: list[str] = []
        for link in soup.select("a[href]"):
            href = urljoin(cable_url, link.get("href", ""))
            text = clean_text(link.get_text(" "))
            if not text or href == cable_url:
                continue
            if "/articles/" in href or re.search(r"(fault|repair|cut|outage|damage|disrupt)", text, re.I):
                if "submarinenetworks.com" in href and href not in urls:
                    urls.append(href)
        return urls

    def _fetch_article(self, url: str) -> dict[str, Any]:
        soup = BeautifulSoup(self.get(url).text, "html.parser")
        title = clean_text((soup.select_one("h1") or soup.select_one("title") or soup).get_text(" "))
        published = extract_date_from_soup(soup)
        return {
            "source": self.source_name,
            "url": url,
            "title": title,
            "content": article_body_text(soup),
            "published_date": published,
        }


def article_body_text(soup: BeautifulSoup) -> str:
    # SubTel Forum embeds cable-map and magazine recommendations inside article
    # bodies. Match their template classes, preserving the same words in prose.
    for node in soup.select("script, style, nav, footer, header, aside, .related-posts, .fusion-related-posts, .stf-related-cable-systems, .stf-related-magazine-articles, .fusion-sharing-box, .sharing-box, .social-share, .newsletter, .comments-area, #respond, [class*='related-post'], [id*='related-post']"):
        node.decompose()
    node = (soup.select_one("[itemprop='articleBody']") or soup.select_one(".article-body")
            or soup.select_one(".post-content") or soup.select_one(".entry-content")
            or soup.select_one("article") or soup.select_one("main") or soup.body or soup)
    content = clean_text(node.get_text(" "))
    # Some publisher themes append recommendations inside the same body element.
    return re.split(r"\b(?:news via inbox|sign up to get the latest updates straight to your inbox|share this story,? choose your platform|you might also like|related posts|related articles)\b", content, maxsplit=1, flags=re.I)[0].strip()


def extract_date_from_soup(soup: BeautifulSoup) -> str | None:
    for selector in ("meta[property='article:published_time']", "meta[name='date']",
                     "time[itemprop='datePublished'][datetime]", "time.entry-date[datetime]",
                     "time.published[datetime]", "time[pubdate][datetime]",
                     ".post-meta time[datetime]", ".entry-meta time[datetime]"):
        node = soup.select_one(selector)
        if not node:
            continue
        value = node.get("datetime") or node.get("content")
        parsed = parse_date(value)
        if parsed:
            return parsed

    # A date in article prose may be an old incident date, not publication.
    # Unknown publication must remain unknown instead of excluding fresh news.
    return None


def parse_rss_entries(content: bytes) -> list[dict[str, str | None]]:
    try:
        import feedparser

        feed = feedparser.parse(content)
        if not getattr(feed, "version", "") or (getattr(feed, "bozo", False) and not feed.entries):
            raise ValueError("Invalid RSS response")
        entries = []
        for entry in feed.entries:
            published = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published = datetime(*entry.published_parsed[:6]).date().isoformat()
            entries.append(
                {
                    "title": entry.get("title"),
                    "link": entry.get("link"),
                    "summary": entry.get("summary"),
                    "published_date": published,
                }
            )
        return entries
    except ModuleNotFoundError:
        root = ElementTree.fromstring(content)
        if root.tag != "rss":
            raise ValueError("Invalid RSS response")
        entries = []
        for item in root.findall(".//item"):
            published = None
            pub_date = text_of(item, "pubDate")
            if pub_date:
                try:
                    published = email.utils.parsedate_to_datetime(pub_date).date().isoformat()
                except Exception:
                    published = parse_date(pub_date)
            entries.append(
                {
                    "title": text_of(item, "title"),
                    "link": text_of(item, "link"),
                    "summary": text_of(item, "description"),
                    "published_date": published,
                    "source_url": item.find("source").attrib.get("url", "") if item.find("source") is not None else "",
                }
            )
        return entries


def text_of(node: ElementTree.Element, tag: str) -> str:
    child = node.find(tag)
    return child.text if child is not None and child.text else ""


def resolve_google_news_url(url: str) -> str:
    parsed = urlparse(clean_text(url))
    if not parsed.netloc:
        return ""
    if "news.google." not in parsed.netloc:
        return url
    query = parse_qs(parsed.query)
    for key in ("url", "q"):
        if query.get(key):
            return unquote(query[key][0])
    return url


def dedupe_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for article in articles:
        key = clean_text(article.get("url")) or clean_text(article.get("title"))
        if key and key not in seen:
            seen.add(key)
            unique.append(article)
    return unique
