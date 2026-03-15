from typing import List
from datetime import datetime
import feedparser

from .base_scraper import BaseScraper
from ..storage import Event, EventType, EventStatus
from ..utils import Config


class GoogleNewsScraper(BaseScraper):
    """Scraper for Google News RSS feeds."""

    # Search keywords for submarine cable news
    KEYWORDS = [
        "submarine cable fault",
        "submarine cable outage",
        "submarine cable break",
        "cable break submarine",
        "海缆故障",
        "海底电缆故障"
    ]

    @property
    def source_name(self) -> str:
        return "Google News"

    def scrape(self) -> List[Event]:
        """
        Scrape Google News RSS for submarine cable related news.

        Returns:
            List of Event objects
        """
        events: List[Event] = []

        for keyword in self.KEYWORDS:
            try:
                url = self._build_rss_url(keyword)
                self.logger.info(f"Fetching news for keyword: {keyword}")

                response = self._get(url)
                response.raise_for_status()

                feed = feedparser.parse(response.content)

                for entry in feed.entries:
                    event = self._parse_entry(entry, keyword)
                    if event:
                        events.append(event)

            except Exception as e:
                self.logger.error(f"Error fetching news for '{keyword}': {e}")

        return events

    def _build_rss_url(self, keyword: str) -> str:
        """
        Build Google News RSS URL for a keyword.

        Args:
            keyword: Search keyword

        Returns:
            RSS feed URL
        """
        from urllib.parse import quote
        query = quote(keyword)
        return f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

    def _parse_entry(self, entry: dict, keyword: str) -> Event:
        """
        Parse a feed entry into an Event.

        Args:
            entry: Feed entry from feedparser
            keyword: Search keyword that matched this entry

        Returns:
            Event object
        """
        # Parse published date
        reported_at = None
        if hasattr(entry, "published_parsed"):
            reported_at = datetime(*entry.published_parsed[:6])

        # Determine event type from title/description
        event_type = EventType.NEWS
        title = entry.get("title", "").lower()
        if "fault" in title or "break" in title:
            event_type = EventType.FAULT
        elif "outage" in title:
            event_type = EventType.OUTAGE
        elif "repair" in title or "fixed" in title:
            event_type = EventType.REPAIR

        return Event(
            source=self.source_name,
            event_type=event_type,
            status=EventStatus.REPORTED,
            reported_at=reported_at,
            description=entry.get("title", ""),
            url=entry.get("link"),
            raw_data={
                "keyword": keyword,
                "summary": entry.get("summary", ""),
                "source": entry.get("source", {}).get("title", "")
            }
        )
