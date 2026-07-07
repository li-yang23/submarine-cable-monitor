import email.utils
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin
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
        response = self.session.get(url, timeout=self.config.request_timeout)
        response.raise_for_status()
        return response


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

    def scrape_articles(self, since: str | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
        articles: list[dict[str, Any]] = []
        for keyword in self.keywords:
            url = f"https://news.google.com/rss/search?q={quote(keyword)}&hl=en-US&gl=US&ceid=US:en"
            self.logger.info("Fetching Google News RSS for %s", keyword)
            response = self.get(url)
            for entry in parse_rss_entries(response.content):
                published = entry.get("published_date")
                if since and published and published < since:
                    continue
                title = clean_text(entry.get("title"))
                articles.append(
                    {
                        "source": self.source_name,
                        "url": clean_text(entry.get("link")),
                        "title": title,
                        "content": clean_text(entry.get("summary")) or title,
                        "published_date": published,
                        "keyword": keyword,
                    }
                )
                if dry_run and len(articles) >= 3:
                    return articles
        return dedupe_articles(articles)


class SubTelForumScraper(ArticleScraper):
    source_name = "SubTel Forum"
    start_url = "https://subtelforum.com/category/cable-faults-maintenance/"

    def scrape_articles(self, since: str | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
        urls = self._discover_urls(since=since, dry_run=dry_run)
        return [self._fetch_article(url) for url in urls]

    def _discover_urls(self, since: str | None, dry_run: bool) -> list[str]:
        urls: list[str] = []
        next_url = self.start_url
        max_pages = 1 if dry_run else int(getattr(self.config, "subtelforum_max_pages", 3))

        for _ in range(max_pages):
            self.logger.info("Discovering SubTel Forum articles from %s", next_url)
            soup = BeautifulSoup(self.get(next_url).text, "html.parser")
            for link in soup.select("h2 a, h3 a, h4 a, .fusion-title a"):
                href = clean_text(link.get("href"))
                title = clean_text(link.get_text(" "))
                if not href or "subtelforum.com" not in href:
                    continue
                if title and href not in urls:
                    urls.append(href)
                if dry_run and len(urls) >= 3:
                    return urls

            next_link = soup.select_one("a.pagination-next, .pagination-next a, a[rel='next']")
            if not next_link or not next_link.get("href"):
                break
            next_url = urljoin(next_url, next_link["href"])
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
                }
            )
        return entries


def text_of(node: ElementTree.Element, tag: str) -> str:
    child = node.find(tag)
    return child.text if child is not None and child.text else ""


def dedupe_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for article in articles:
        key = clean_text(article.get("url")) or clean_text(article.get("title"))
        if key and key not in seen:
            seen.add(key)
            unique.append(article)
    return unique
