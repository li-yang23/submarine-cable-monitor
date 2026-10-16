import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.scrapers.article_sources import SubmarineNetworksScraper
from src.utils import Config
from src.utils.audit import AuditRun


class NetworksDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = Config(article_queue_path="", article_state_path=str(self.root / "state.json"),
                             submarine_networks_links_path=str(self.root / "seeds.json"))
        self.scraper = SubmarineNetworksScraper(self.config)
        self.scraper.fetch_enabled = False
        self.audit = AuditRun(self.root / "runs")
        self.audit.activate()
        self.addCleanup(self.audit.finish, {})

    def records(self):
        return [json.loads(line) for line in (self.audit.path / "audit.jsonl").read_text().splitlines()]

    @staticmethod
    def response(html, url="https://www.submarinenetworks.com/en/systems/trans-pacific/aag"):
        return SimpleNamespace(text=html, status_code=200, url=url)

    def test_directory_records_response_evidence_and_relative_incident_links(self):
        html = "<title>AAG - Submarine Networks</title><a href='/en/'>Home</a><a href='/en/systems/trans-pacific/aag/aag-cable-cut'>AAG cable cut</a>"
        url = "https://www.submarinenetworks.com/en/systems/trans-pacific/aag"
        with patch.object(self.scraper, "get", return_value=self.response(html)):
            found = self.scraper._discover_article_urls(url)
        self.assertEqual(found, [url + "/aag-cable-cut"])
        record = next(row for row in self.records() if row["stage"] == "cable_page")
        self.assertEqual((record["http_status"], record["anchor_count"], record["entry_count"]), (200, 2, 1))
        self.assertEqual(record["content_length"], len(html.encode()))
        self.assertEqual(record["title"], "AAG - Submarine Networks")
        self.assertEqual(record["discovered_url"], url)

    def test_successful_page_with_no_incident_links_still_has_zero_count_trace(self):
        with patch.object(self.scraper, "get", return_value=self.response("<title>System</title><a href='/en/'>Home</a>")):
            self.assertEqual(self.scraper._discover_article_urls("https://www.submarinenetworks.com/system"), [])
        record = next(row for row in self.records() if row["stage"] == "cable_page")
        self.assertEqual((record["outcome"], record["entry_count"], record["anchor_count"]), ("completed", 0, 1))

    def test_http_200_empty_or_challenge_html_is_failure(self):
        for html in ["<html></html>", "<title>Just a moment...</title><a href='/'>Home</a>"]:
            with self.subTest(html=html), patch.object(self.scraper, "get", return_value=self.response(html)):
                with self.assertRaises(ValueError):
                    self.scraper._discover_article_urls("https://www.submarinenetworks.com/system")
        self.assertEqual(sum(row.get("reason") == "empty_or_challenge_page" for row in self.records()), 2)

    def test_full_seed_traversal_with_no_articles_warns_and_reports_every_page(self):
        seeds = [{"href": f"https://www.submarinenetworks.com/system/{i}", "cable_name": f"Cable {i}"} for i in range(3)]
        Path(self.config.submarine_networks_links_path).write_text(json.dumps(seeds))
        with patch.object(self.scraper, "get", return_value=self.response("<title>System</title><a href='/en/'>Home</a>")):
            self.assertEqual(self.scraper.scrape_articles(), [])
        records = self.records()
        self.assertEqual(sum(row["stage"] == "cable_page" and row["outcome"] == "completed" for row in records), 3)
        self.assertTrue(any(row.get("reason") == "no_articles_in_seed_traversal" for row in records))
        seed_record = next(row for row in records if row["stage"] == "discovery_seed")
        self.assertEqual(seed_record["entry_count"], 3)
        self.assertEqual(self.scraper.article_queue.get_cursor(self.scraper.source_name), 0)

    def test_directory_repair_only_uses_verified_advertised_same_site_link(self):
        old='https://www.submarinenetworks.com/en/old/aag'
        new='https://www.submarinenetworks.com/en/systems/trans-pacific/aag'
        html=f'<a href="{new}">AAG</a><a href="https://evil.example/en/systems/aag">AAG</a>'
        with patch.object(self.scraper,'get',return_value=self.response(html)), patch.object(self.scraper,'_discover_article_urls',return_value=['https://www.submarinenetworks.com/articles/fault']) as discover:
            found=self.scraper._repair_directory({'href':old,'cable_name':'AAG'})
        discover.assert_called_once_with(new)
        self.assertEqual(self.scraper.article_queue.get_cursor('directory_route:'+old),new)
        self.assertEqual(len(found),1)

    def test_ambiguous_directory_replacement_is_not_guessed(self):
        html='<a href="/en/systems/a/aag">AAG</a><a href="/en/systems/b/aag">AAG</a>'
        with patch.object(self.scraper,'get',return_value=self.response(html)):
            self.assertIsNone(self.scraper._repair_directory({'href':'https://www.submarinenetworks.com/en/old/aag','cable_name':'AAG'}))

    def test_missing_or_empty_seed_file_is_not_silent_success(self):
        self.assertEqual(self.scraper._load_cable_links(), [])
        Path(self.config.submarine_networks_links_path).write_text("[]")
        self.assertEqual(self.scraper._load_cable_links(), [])
        self.assertEqual({row.get("reason") for row in self.records()}, {"missing_seed_file", "empty_seed_file"})


if __name__ == "__main__":
    unittest.main()
