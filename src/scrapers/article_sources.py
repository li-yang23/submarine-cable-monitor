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
from src.utils import Config, get_logger
from src.utils.audit import article_fields, emit, error_details


class ArticleScraper:
    source_name = "article"

    def __init__(self, config: Config):
        self.config = config
        self.article_state = ArticleState.from_config(config)
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
        articles: list[dict[str, Any]] = []
        max_articles = 3 if dry_run else int(self.config.google_news_max_articles)
        max_attempts = 8 if dry_run else int(self.config.google_news_max_attempts)
        if not since:
            since = (datetime.now(timezone.utc).date() - timedelta(days=self.config.google_news_lookback_days)).isoformat()
        keywords = self.config.google_news_keywords or self.keywords
        queues: dict[str, list[dict[str, Any]]] = {}
        seen: dict[str, dict[str, Any]] = {}
        successful_feeds = 0
        # Discovery is independent of the body-fetch budget: later keywords always run.
        for keyword in keywords:
            search_query = f"{keyword} after:{since}"
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
            emit("rss", "completed", source=self.source_name, query=keyword, entry_count=len(entries), since=since)
            queues[keyword] = []
            for entry in entries:
                item = {"source": self.source_name, "title": clean_text(entry.get("title")), "discovered_url": clean_text(entry.get("link")), "published_date": entry.get("published_date"), "keyword": keyword}
                fields = article_fields(item)
                emit("discover", "found", **fields)
                if not item["discovered_url"]:
                    emit("select", "skipped", **fields, reason="missing_url")
                    continue
                if item["published_date"] and item["published_date"] < since:
                    emit("select", "skipped", **fields, reason="before_since")
                    continue
                key = item["discovered_url"]
                if key in seen:
                    seen[key]["queries"].append(keyword)
                    emit("select", "skipped", **fields, reason="duplicate_discovery")
                    continue
                item["queries"] = [keyword]
                seen[key] = item
                queues[keyword].append(item)
            queues[keyword].sort(key=lambda item: item.get("published_date") or "", reverse=True)
        if not successful_feeds:
            raise RuntimeError("All Google News RSS queries failed")

        # Each query gets a turn; within a query fetch newest first. Date filtering and
        # duplicate RSS hits do not consume attempts. Every remainder has a reason.
        ordered: list[dict[str, Any]] = []
        while any(queues.values()):
            for queue in queues.values():
                if queue:
                    ordered.append(queue.pop(0))
        attempts = 0
        real_urls: set[str] = set()
        for item in ordered:
            fields = article_fields(item)
            if self.article_state.is_processed(item["discovered_url"]):
                emit("select", "skipped", **fields, queries=item["queries"], reason="already_processed")
                continue
            if len(articles) >= max_articles or attempts >= max_attempts:
                emit("select", "skipped", **fields, queries=item["queries"], reason="article_budget" if len(articles) >= max_articles else "attempt_budget")
                continue
            attempts += 1
            self._last_fetch_already_processed = False
            emit("select", "selected", **fields, queries=item["queries"])
            try:
                article = self._fetch_news_article(item["discovered_url"], item["title"], item["published_date"], item["keyword"])
            except Exception as exc:
                emit("fetch", "failed", **fields, reason="article_fetch_error", **error_details(exc))
                continue
            if self._last_fetch_already_processed:
                attempts -= 1
                continue
            if article:
                if article["url"] in real_urls:
                    emit("select", "skipped", **article_fields(article), reason="duplicate_final_url")
                    continue
                real_urls.add(article["url"])
                articles.append(article)
        emit("collection", "completed", source=self.source_name, attempts=attempts, article_count=len(articles), max_articles=max_articles, max_attempts=max_attempts)
        return articles

    def _fetch_news_article(
        self,
        discovered_url: str,
        rss_title: str,
        published: str | None,
        keyword: str,
    ) -> dict[str, Any] | None:
        fields = article_fields({"source": self.source_name, "title": rss_title, "discovered_url": discovered_url, "published_date": published, "keyword": keyword})
        real_url = self._resolve_google_news_url(discovered_url)
        if not real_url:
            emit("resolve", "failed", **fields, reason="google_news_decode_failed")
            return None
        fields["url"] = real_url
        emit("resolve", "completed", **fields)
        if self.article_state.is_processed(real_url):
            self._last_fetch_already_processed = True
            emit("select", "skipped", **fields, reason="already_processed")
            return None
        if "subtelforum.com" in urlparse(real_url).netloc:
            emit("fetch", "skipped", **fields, reason="dedicated_source_excluded")
            return None
        try:
            page = self.extract_page_text(real_url)
        except Exception as exc:
            emit("fetch", "failed", **fields, reason="article_fetch_error", **error_details(exc))
            self.logger.warning("Skipping Google News article %s: %s", fields["article_id"], type(exc).__name__)
            return None
        content = clean_text(page.get("content"))
        if len(content) < 300:
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
        if self.article_state.is_processed(article["url"]):
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
        urls = self._discover_urls(since=since, dry_run=dry_run)
        articles: list[dict[str, Any]] = []
        for url in urls:
            try:
                article = self._fetch_article(url)
                if since and article.get("published_date") and article["published_date"] < since:
                    emit("select", "skipped", **article_fields(article), reason="before_since")
                    continue
                emit("fetch", "completed", **article_fields(article), content_length=len(article.get("content", "")))
                articles.append(article)
                time.sleep(0.5)
            except Exception as exc:
                emit("fetch", "failed", **article_fields({"source": self.source_name, "url": url}), reason="article_fetch_error", **error_details(exc))
                self.logger.warning("Skipping SubTel Forum article: %s", type(exc).__name__)
        return articles

    def _discover_urls(self, since: str | None, dry_run: bool) -> list[str]:
        urls: list[str] = []
        next_url = self.start_url
        max_pages = 1 if dry_run else int(getattr(self.config, "subtelforum_max_pages", 3))
        max_articles = 3 if dry_run else int(getattr(self.config, "subtelforum_max_articles", 8))

        for _ in range(max_pages):
            self.logger.info("Discovering SubTel Forum articles from %s", next_url)
            try:
                soup = BeautifulSoup(self.get(next_url).text, "html.parser")
            except Exception as exc:
                emit("discover", "fallback", source=self.source_name, url=next_url, reason="category_fetch_error", **error_details(exc))
                self.logger.warning("SubTel Forum category page failed, falling back to feed: %s", type(exc).__name__)
                return self._discover_urls_from_feed(max_articles=max_articles)
            for link in soup.select("h2 a, h3 a, h4 a, .fusion-title a"):
                href = clean_text(link.get("href"))
                title = clean_text(link.get_text(" "))
                if not href or "subtelforum.com" not in href:
                    continue
                if title and href not in urls:
                    fields = article_fields({"source": self.source_name, "url": href, "title": title})
                    emit("discover", "found", **fields)
                    if len(urls) >= max_articles:
                        emit("select", "skipped", **fields, reason="article_budget")
                    else:
                        urls.append(href)
                        emit("select", "selected", **fields)

            next_link = soup.select_one("a.pagination-next, .pagination-next a, a[rel='next']")
            if not next_link or not next_link.get("href"):
                break
            next_url = urljoin(next_url, next_link["href"])
        return urls

    def _discover_urls_from_feed(self, max_articles: int) -> list[str]:
        response = self.get(self.feed_url)
        urls: list[str] = []
        for entry in parse_rss_entries(response.content):
            href = clean_text(entry.get("link"))
            if href and "subtelforum.com" in href and href not in urls:
                fields = article_fields({"source": self.source_name, "url": href, "title": entry.get("title"), "published_date": entry.get("published_date")})
                emit("discover", "found", **fields)
                if len(urls) >= max_articles:
                    emit("select", "skipped", **fields, reason="article_budget")
                else:
                    urls.append(href)
                    emit("select", "selected", **fields)
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
        links = self._load_cable_links()
        max_cables = 3 if dry_run else int(getattr(self.config, "submarine_networks_max_cables", 50))
        max_articles = 1 if dry_run else int(getattr(self.config, "submarine_networks_articles_per_cable", 3))
        articles: list[dict[str, Any]] = []

        for cable in links[:max_cables]:
            cable_url = clean_text(cable.get("href"))
            cable_name = clean_text(cable.get("cable_name"))
            if not cable_url:
                continue
            try:
                discovered_urls = self._discover_article_urls(cable_url)
            except Exception as exc:
                emit("discover", "failed", source=self.source_name, url=cable_url, reason="cable_page_fetch_error", **error_details(exc))
                self.logger.warning("Skipping cable page: %s", type(exc).__name__)
                continue
            for index, url in enumerate(discovered_urls):
                fields = article_fields({"source": self.source_name, "url": url})
                emit("discover", "found", **fields, cable_name=cable_name)
                if index >= max_articles:
                    emit("select", "skipped", **fields, reason="article_budget")
                    continue
                emit("select", "selected", **fields)
                try:
                    article = self._fetch_article(url)
                except Exception as exc:
                    emit("fetch", "failed", **fields, reason="article_fetch_error", **error_details(exc))
                    self.logger.warning("Skipping article: %s", type(exc).__name__)
                    continue
                article["cable_name"] = cable_name
                if since and article.get("published_date") and article["published_date"] < since:
                    emit("select", "skipped", **article_fields(article), reason="before_since")
                    continue
                emit("fetch", "completed", **article_fields(article), content_length=len(article.get("content", "")))
                articles.append(article)
                if dry_run and len(articles) >= 3:
                    return dedupe_articles(articles)
        return dedupe_articles(articles)

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
    for node in soup.select("script, style, nav, footer, header, aside, .related-posts, .fusion-related-posts, .fusion-sharing-box, .sharing-box, .social-share, .newsletter, .comments-area, #respond, [class*='related-post'], [id*='related-post']"):
        node.decompose()
    node = (soup.select_one("[itemprop='articleBody']") or soup.select_one(".article-body")
            or soup.select_one(".post-content") or soup.select_one(".entry-content")
            or soup.select_one("article") or soup.select_one("main") or soup.body or soup)
    content = clean_text(node.get_text(" "))
    # Some publisher themes append recommendations inside the same body element.
    return re.split(r"\b(?:news via inbox|sign up to get the latest updates straight to your inbox|share this story,? choose your platform|you might also like|related posts|related articles)\b", content, maxsplit=1, flags=re.I)[0].strip()


def extract_date_from_soup(soup: BeautifulSoup) -> str | None:
    for selector in ("time[datetime]", "meta[property='article:published_time']", "meta[name='date']"):
        node = soup.select_one(selector)
        if not node:
            continue
        value = node.get("datetime") or node.get("content")
        parsed = parse_date(value)
        if parsed:
            return parsed

    text = clean_text(soup.get_text(" "))[:3000]
    date_match = re.search(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b", text)
    return parse_date(date_match.group(1)) if date_match else None


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
