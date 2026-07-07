from .article_sources import GoogleNewsArticleScraper, SubTelForumScraper, SubmarineNetworksScraper

try:
    from .base_scraper import BaseScraper
    from .telegeography import TeleGeographyScraper
    from .infrapedia import InfrapediaScraper
    from .cablefaults import CableFaultsScraper
    from .google_news import GoogleNewsScraper
    from .github_scraper import GitHubScraper
except ModuleNotFoundError:  # Optional legacy scrapers may need deps not installed in a test env.
    BaseScraper = None
    TeleGeographyScraper = None
    InfrapediaScraper = None
    CableFaultsScraper = None
    GoogleNewsScraper = None
    GitHubScraper = None

__all__ = [
    "BaseScraper",
    "TeleGeographyScraper",
    "InfrapediaScraper",
    "CableFaultsScraper",
    "GoogleNewsScraper",
    "GitHubScraper",
    "GoogleNewsArticleScraper",
    "SubTelForumScraper",
    "SubmarineNetworksScraper",
]
