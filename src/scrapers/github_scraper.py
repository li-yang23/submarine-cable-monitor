from typing import List
from datetime import datetime, timedelta

from .base_scraper import BaseScraper
from ..storage import Event, EventType, EventStatus
from ..utils import Config


class GitHubScraper(BaseScraper):
    """
    Scraper for GitHub repositories related to submarine cables.

    API Setup:
    - Create a Personal Access Token (PAT): https://github.com/settings/tokens
    - Scopes needed: `public_repo` (read-only access to public repos)
    - No token needed for public API, but rate-limited to 60 requests/hour
    - With token: 5000 requests/hour

    What this does:
    - Searches for repositories about submarine cables
    - Finds recent activity (pushes, issues, releases)
    - Creates events for significant updates
    """

    # Search queries for submarine cable related repositories
    SEARCH_QUERIES = [
        "submarine cable",
        "undersea cable",
        "fiber optic cable network",
        "cable monitoring system"
    ]

    @property
    def source_name(self) -> str:
        return "GitHub"

    def scrape(self) -> List[Event]:
        """
        Scrape GitHub for submarine cable related repositories and activity.

        To use with full rate limits:
        1. Go to https://github.com/settings/tokens
        2. Generate a new token (classic)
        3. Check the `public_repo` scope
        4. Add it to config.yaml: scrapers.github.api_key

        Returns:
            List of Event objects
        """
        events: List[Event] = []
        scraper_config = self.config.scrapers.get("github")
        api_key = scraper_config.api_key if scraper_config else ""

        # Prepare headers
        headers = {"Accept": "application/vnd.github.v3+json"}
        if api_key:
            headers["Authorization"] = f"token {api_key}"
            self.logger.info("Using GitHub API with authentication")
        else:
            self.logger.warning("No GitHub API key configured - rate limited to 60 requests/hour")
            self.logger.info("  To get a token, visit: https://github.com/settings/tokens")

        try:
            # Search for repositories
            for query in self.SEARCH_QUERIES:
                self.logger.info(f"Searching GitHub for: '{query}'")
                repos = self._search_repositories(query, headers)

                for repo in repos:
                    # Check if repo has recent activity (last 30 days)
                    pushed_at = repo.get("pushed_at")
                    if pushed_at:
                        try:
                            pushed_date = datetime.strptime(pushed_at, "%Y-%m-%dT%H:%M:%SZ")
                            if pushed_date > datetime.utcnow() - timedelta(days=30):
                                # Create event for recent repo
                                event = self._create_repo_event(repo)
                                if event:
                                    events.append(event)
                        except ValueError:
                            pass

                # Add delay between requests
                if len(self.SEARCH_QUERIES) > 1:
                    self._delay()

            self.logger.info(f"Found {len(events)} GitHub events")

        except Exception as e:
            self.logger.error(f"Error scraping GitHub: {e}")

        return events

    def _search_repositories(self, query: str, headers: dict) -> List[dict]:
        """
        Search GitHub repositories.

        Args:
            query: Search query
            headers: Request headers

        Returns:
            List of repository dictionaries
        """
        from urllib.parse import quote

        try:
            url = f"https://api.github.com/search/repositories?q={quote(query)}&sort=updated&per_page=10"
            response = self._get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            return data.get("items", [])
        except Exception as e:
            self.logger.error(f"Search failed for '{query}': {e}")
            return []

    def _create_repo_event(self, repo: dict) -> Event:
        """
        Create an Event from a GitHub repository.

        Args:
            repo: GitHub repository data

        Returns:
            Event object
        """
        # Parse pushed date
        reported_at = None
        pushed_at = repo.get("pushed_at")
        if pushed_at:
            try:
                reported_at = datetime.strptime(pushed_at, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                reported_at = datetime.utcnow()

        # Build description
        description = repo.get("description", "") or "No description"
        stars = repo.get("stargazers_count", 0)
        language = repo.get("language", "unknown")

        full_description = f"[⭐{stars}] {description} (Language: {language})"

        return Event(
            source=self.source_name,
            event_type=EventType.UPDATE,
            cable_name=None,
            location=None,
            status=EventStatus.UNKNOWN,
            reported_at=reported_at,
            description=full_description,
            url=repo.get("html_url"),
            raw_data={
                "repo_name": repo.get("full_name"),
                "stars": stars,
                "language": language,
                "forks": repo.get("forks_count"),
                "open_issues": repo.get("open_issues_count"),
                "owner": repo.get("owner", {}).get("login")
            }
        )
