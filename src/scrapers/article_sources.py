import email.utils
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from src.processing.normalization import clean_text, parse_date
from src.utils import Config, get_logger


class ArticleScraper:
    source_name = "article"

    def __init__(self, config: Config):
        self.config = config
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
        for node in soup.select("script, style, nav, footer, header, aside"):
            node.decompose()
        title = clean_text((soup.select_one("h1") or soup.select_one("title") or soup).get_text(" "))
        content_node = (
            soup.select_one("article")
            or soup.select_one("main")
            or soup.select_one(".post-content")
            or soup.select_one(".article-body")
            or soup.body
            or soup
        )
        return {
            "title": title,
            "content": clean_text(content_node.get_text(" ")),
            "published_date": extract_date_from_soup(soup),
            "final_url": response.url,
        }


class GoogleNewsArticleScraper(ArticleScraper):
    source_name = "Google News"
    keywords = [
        "submarine cable fault",
        "submarine cable outage",
        "submarine cable break",
        "undersea cable damage",
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
        max_articles = 3 if dry_run else int(getattr(self.config, "google_news_max_articles", 12))
        max_attempts = 8 if dry_run else int(getattr(self.config, "google_news_max_attempts", 30))
        attempts = 0
        for keyword in self.keywords:
            url = f"https://news.google.com/rss/search?q={quote(keyword)}&hl=en-US&gl=US&ceid=US:en"
            self.logger.info("Fetching Google News RSS for %s", keyword)
            response = self.get(url)
            for entry in parse_rss_entries(response.content):
                attempts += 1
                if attempts > max_attempts:
                    return dedupe_articles(articles)
                published = entry.get("published_date")
                if since and published and published < since:
                    continue
                title = clean_text(entry.get("title"))
                discovered_url = clean_text(entry.get("link"))
                article = self._fetch_news_article(discovered_url, title, published, keyword)
                if article:
                    articles.append(article)
                if len(articles) >= max_articles:
                    return dedupe_articles(articles)
        return dedupe_articles(articles)

    def _fetch_news_article(
        self,
        discovered_url: str,
        rss_title: str,
        published: str | None,
        keyword: str,
    ) -> dict[str, Any] | None:
        real_url = self._resolve_google_news_url(discovered_url)
        if not real_url:
            return None
        if "subtelforum.com" in urlparse(real_url).netloc:
            return None
        try:
            page = self.extract_page_text(real_url)
        except Exception as exc:
            self.logger.warning("Skipping Google News article %s: %s", real_url, exc)
            return None
        content = clean_text(page.get("content"))
        if len(content) < 300:
            self.logger.warning("Skipping Google News article with short content: %s", real_url)
            return None
        return {
            "source": self.source_name,
            "url": clean_text(page.get("final_url") or real_url),
            "discovered_url": discovered_url,
            "title": clean_text(page.get("title")) or rss_title,
            "content": content,
            "published_date": page.get("published_date") or published,
            "keyword": keyword,
        }

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
            self.logger.warning("Unable to decode Google News URL %s: %s", url, exc)
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
                articles.append(self._fetch_article(url))
                time.sleep(0.5)
            except Exception as exc:
                self.logger.warning("Skipping SubTel Forum article %s: %s", url, exc)
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
                self.logger.warning("SubTel Forum category page failed, falling back to feed: %s", exc)
                return self._discover_urls_from_feed(max_articles=max_articles)
            for link in soup.select("h2 a, h3 a, h4 a, .fusion-title a"):
                href = clean_text(link.get("href"))
                title = clean_text(link.get_text(" "))
                if not href or "subtelforum.com" not in href:
                    continue
                if title and href not in urls:
                    urls.append(href)
                if len(urls) >= max_articles:
                    return urls

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
                urls.append(href)
            if len(urls) >= max_articles:
                break
        return urls

    def _fetch_article(self, url: str) -> dict[str, Any]:
        soup = BeautifulSoup(self.get(url).text, "html.parser")
        title = clean_text((soup.select_one("h1") or soup.select_one("title") or soup).get_text(" "))
        published = extract_date_from_soup(soup)
        content_node = soup.select_one(".post-content") or soup.select_one("article") or soup.body or soup
        return {
            "source": self.source_name,
            "url": url,
            "title": title,
            "content": clean_text(content_node.get_text(" ")),
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
                article_urls = self._discover_article_urls(cable_url)[:max_articles]
            except Exception as exc:
                self.logger.warning("Skipping cable page %s: %s", cable_url, exc)
                continue
            for url in article_urls:
                try:
                    article = self._fetch_article(url)
                except Exception as exc:
                    self.logger.warning("Skipping article %s: %s", url, exc)
                    continue
                article["cable_name"] = cable_name
                if since and article.get("published_date") and article["published_date"] < since:
                    continue
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
        content_node = soup.select_one(".article-body") or soup.select_one("article") or soup.body or soup
        return {
            "source": self.source_name,
            "url": url,
            "title": title,
            "content": clean_text(content_node.get_text(" ")),
            "published_date": extract_date_from_soup(soup),
        }


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
