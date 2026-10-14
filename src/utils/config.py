import os
from typing import Any
import yaml
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency fallback
    load_dotenv = None


@dataclass
class ScraperConfig:
    """Configuration for a single scraper."""
    url: str
    enabled: bool = True
    update_interval_hours: int = 24
    api_key: str = ""


@dataclass
class Config:
    """Main configuration class."""
    # Request settings
    request_delay_min: float = 1.0
    request_delay_max: float = 3.0
    request_timeout: int = 30
    max_retries: int = 3

    # Database settings
    database_path: str = "data/events.db"
    data_retention_days: int = 730  # 2 years
    event_store_path: str = "data/events.jsonl"

    # Scraper configurations
    scrapers: dict[str, ScraperConfig] = field(default_factory=dict)
    subtelforum_max_pages: int = 0
    subtelforum_max_articles: int = 0
    google_news_max_articles: int = 0
    google_news_max_attempts: int = 0
    google_news_lookback_days: int = 14
    google_news_keywords: list[str] = field(default_factory=list)
    audit_dir: str = "logs/runs"
    article_queue_path: str = "data/article-queue.json"
    collection_time_budget_seconds: int = 1200
    low_balance_threshold: float = 5.0
    article_state_path: str = "data/article-state.json"
    article_state_refresh_days: int = 7
    article_state_retention_days: int = 30
    article_state_max_urls: int = 2000
    submarine_networks_links_path: str = "data/cable-links.json"
    submarine_networks_max_cables: int = 0
    submarine_networks_articles_per_cable: int = 0

    # LLM settings. API keys are read from env/GitHub Secrets only.
    llm_model: str = "deepseek-ai/DeepSeek-V3"
    llm_base_url: str = "https://api.siliconflow.cn/v1/"
    embedding_model: str = "BAAI/bge-m3"

    # User agent
    user_agent: str = "Mozilla/5.0 (compatible; SubmarineCableMonitor/1.0; +https://github.com/your-username/submarine-cable-monitor)"

    @classmethod
    def from_yaml(cls, config_path: str) -> "Config":
        """
        Load configuration from a YAML file.

        Args:
            config_path: Path to the YAML configuration file

        Returns:
            Config instance
        """
        if load_dotenv:
            load_dotenv(".env", override=False)
            load_dotenv("config.env", override=False)

        config = cls()

        if not os.path.exists(config_path):
            return config

        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if "request" in data:
            req = data["request"]
            config.request_delay_min = req.get("delay_min", 1.0)
            config.request_delay_max = req.get("delay_max", 3.0)
            config.request_timeout = req.get("timeout", 30)
            config.max_retries = req.get("max_retries", 3)

        if "database" in data:
            db = data["database"]
            config.database_path = db.get("path", "data/events.db")
            config.data_retention_days = db.get("retention_days", 730)
            config.event_store_path = db.get("event_store_path", "data/events.jsonl")

        if "scrapers" in data:
            for name, scraper_data in data["scrapers"].items():
                config.scrapers[name] = ScraperConfig(
                    url=scraper_data.get("url", ""),
                    enabled=scraper_data.get("enabled", True),
                    update_interval_hours=scraper_data.get("update_interval_hours", 24),
                    api_key=scraper_data.get("api_key", "")
                )

        if "user_agent" in data:
            config.user_agent = data["user_agent"]

        article_sources = data.get("article_sources", {})
        config.subtelforum_max_pages = article_sources.get("subtelforum_max_pages", config.subtelforum_max_pages)
        config.subtelforum_max_articles = article_sources.get("subtelforum_max_articles", config.subtelforum_max_articles)
        config.google_news_max_articles = article_sources.get("google_news_max_articles", config.google_news_max_articles)
        config.google_news_max_attempts = article_sources.get("google_news_max_attempts", config.google_news_max_attempts)
        config.google_news_lookback_days = article_sources.get("google_news_lookback_days", config.google_news_lookback_days)
        config.google_news_keywords = article_sources.get("google_news_keywords", config.google_news_keywords)
        config.audit_dir = data.get("logging", {}).get("audit_dir", config.audit_dir)
        config.article_queue_path = data.get("article_queue", {}).get("path", config.article_queue_path)
        config.collection_time_budget_seconds = article_sources.get("time_budget_seconds", config.collection_time_budget_seconds)
        article_state = data.get("article_state", {})
        config.article_state_path = article_state.get("path", config.article_state_path)
        config.article_state_refresh_days = article_state.get("refresh_days", config.article_state_refresh_days)
        config.article_state_retention_days = article_state.get("retention_days", config.article_state_retention_days)
        config.article_state_max_urls = article_state.get("max_urls", config.article_state_max_urls)
        config.submarine_networks_links_path = article_sources.get(
            "submarine_networks_links_path",
            config.submarine_networks_links_path,
        )
        config.submarine_networks_max_cables = article_sources.get(
            "submarine_networks_max_cables",
            config.submarine_networks_max_cables,
        )
        config.submarine_networks_articles_per_cable = article_sources.get(
            "submarine_networks_articles_per_cable",
            config.submarine_networks_articles_per_cable,
        )

        llm = data.get("llm", {})
        config.low_balance_threshold = llm.get("low_balance_threshold", config.low_balance_threshold)
        config.llm_model = os.getenv("LLM_MODEL") or llm.get("model", config.llm_model)
        config.llm_base_url = os.getenv("SILICONFLOW_BASE_URL") or llm.get("base_url", config.llm_base_url)
        config.embedding_model = os.getenv("EMBEDDING_MODEL") or llm.get("embedding_model", config.embedding_model)

        return config

    def to_dict(self) -> dict[str, Any]:
        """Convert config to dictionary."""
        return {
            "request": {
                "delay_min": self.request_delay_min,
                "delay_max": self.request_delay_max,
                "timeout": self.request_timeout,
                "max_retries": self.max_retries
            },
            "database": {
                "path": self.database_path,
                "retention_days": self.data_retention_days,
                "event_store_path": self.event_store_path,
            },
            "scrapers": {
                name: {
                    "url": sc.url,
                    "enabled": sc.enabled,
                    "update_interval_hours": sc.update_interval_hours,
                    "api_key": sc.api_key
                }
                for name, sc in self.scrapers.items()
            },
            "article_queue": {"path": self.article_queue_path},
            "article_sources": {
                "time_budget_seconds": self.collection_time_budget_seconds,
                "subtelforum_max_pages": self.subtelforum_max_pages,
                "subtelforum_max_articles": self.subtelforum_max_articles,
                "google_news_max_articles": self.google_news_max_articles,
                "google_news_max_attempts": self.google_news_max_attempts,
                "google_news_lookback_days": self.google_news_lookback_days,
                "google_news_keywords": self.google_news_keywords,
                "submarine_networks_links_path": self.submarine_networks_links_path,
                "submarine_networks_max_cables": self.submarine_networks_max_cables,
                "submarine_networks_articles_per_cable": self.submarine_networks_articles_per_cable,
            },
            "llm": {
                "model": self.llm_model,
                "base_url": self.llm_base_url,
                "embedding_model": self.embedding_model,
                "low_balance_threshold": self.low_balance_threshold,
            },
            "user_agent": self.user_agent,
            "logging": {"audit_dir": self.audit_dir},
            "article_state": {
                "path": self.article_state_path,
                "refresh_days": self.article_state_refresh_days,
                "retention_days": self.article_state_retention_days,
                "max_urls": self.article_state_max_urls,
            },
        }
