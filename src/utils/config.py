import os
from typing import Any
import yaml
from dataclasses import dataclass, field


@dataclass
class ScraperConfig:
    """Configuration for a single scraper."""
    url: str
    enabled: bool = True
    update_interval_hours: int = 24


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

    # Scraper configurations
    scrapers: dict[str, ScraperConfig] = field(default_factory=dict)

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

        if "scrapers" in data:
            for name, scraper_data in data["scrapers"].items():
                config.scrapers[name] = ScraperConfig(
                    url=scraper_data.get("url", ""),
                    enabled=scraper_data.get("enabled", True),
                    update_interval_hours=scraper_data.get("update_interval_hours", 24)
                )

        if "user_agent" in data:
            config.user_agent = data["user_agent"]

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
                "retention_days": self.data_retention_days
            },
            "scrapers": {
                name: {
                    "url": sc.url,
                    "enabled": sc.enabled,
                    "update_interval_hours": sc.update_interval_hours
                }
                for name, sc in self.scrapers.items()
            },
            "user_agent": self.user_agent
        }
