from abc import ABC, abstractmethod
from typing import List
import random
import time
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..storage import Event
from ..utils import get_logger, Config


class BaseScraper(ABC):
    """Abstract base class for all scrapers."""

    def __init__(self, config: Config):
        """
        Initialize the scraper.

        Args:
            config: Configuration object
        """
        self.config = config
        self.logger = get_logger(self.__class__.__name__)
        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        """
        Create a requests session with retry logic.

        Returns:
            Configured requests session
        """
        session = requests.Session()

        # Set up retry strategy
        retry_strategy = Retry(
            total=self.config.max_retries,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )

        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        # Set default headers
        session.headers.update({
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate"
        })

        return session

    def _delay(self) -> None:
        """Add random delay between requests."""
        delay = random.uniform(
            self.config.request_delay_min,
            self.config.request_delay_max
        )
        time.sleep(delay)

    def _get(self, url: str, **kwargs) -> requests.Response:
        """
        Make a GET request with delay.

        Args:
            url: URL to request
            **kwargs: Additional arguments for requests.get

        Returns:
            Response object
        """
        self._delay()

        if "timeout" not in kwargs:
            kwargs["timeout"] = self.config.request_timeout

        return self.session.get(url, **kwargs)

    @abstractmethod
    def scrape(self) -> List[Event]:
        """
        Scrape data and return list of events.

        Returns:
            List of Event objects
        """
        pass

    @property
    @abstractmethod
    def source_name(self) -> str:
        """
        Get the name of the data source.

        Returns:
            Source name as string
        """
        pass

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.session.close()
