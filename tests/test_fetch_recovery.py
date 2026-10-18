import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
import requests

from src.scrapers.article_sources import ArticleScraper, GoogleNewsArticleScraper, PublisherAccessError, SubTelForumScraper, article_body_text
from src.utils import Config
from src.utils.audit import AuditRun
from scripts.notify_monitor import build_report


def response(status=200, body='<article>Public cable report</article>', headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = body.encode()
    r.encoding = 'utf-8'
    r.url = 'https://publisher.example/story'
    r.headers.update(headers or {})
    return r


class ShortBodyRegressionTests(unittest.TestCase):
    def test_caption_cannot_hide_complete_article_or_include_sidebar(self):
        from bs4 import BeautifulSoup
        from src.scrapers.article_sources import article_body_text
        prose='SIG appointed its finance chief as its next CEO. ' * 10
        html='<article><div itemprop="articleBody">Photo caption.</div><section>'+prose+'</section><aside>Unrelated undersea cable cut</aside></article>'
        body=article_body_text(BeautifulSoup(html,'html.parser'))
        self.assertIn(prose.strip(),body)
        self.assertNotIn('undersea',body)


class FetchRecoveryTests(unittest.TestCase):
    def test_featured_image_does_not_hide_real_entry_content(self):
        html = "<div class='entry-content featured-image'>Photo</div><div class='entry-content'><p>Actual submarine cable outage evidence.</p></div>"
        self.assertEqual(article_body_text(BeautifulSoup(html, 'html.parser')), 'Actual submarine cable outage evidence.')

    def test_split_article_keeps_all_body_sections(self):
        html = '<article><div class="post-content post-content-double">First cable cut.</div><aside>Unrelated outage.</aside><div class="post-content post-content-double">Long second interview section about cable resilience.</div><div class="post-content post-content-double">Last restoration update.</div></article>'
        text = article_body_text(BeautifulSoup(html, 'html.parser'))
        self.assertEqual(text, 'First cable cut. Long second interview section about cable resilience. Last restoration update.')

    def test_tagdiv_body_excludes_sidebar_news(self):
        html = '<article><div class="tdb_single_content">Subsea cable cut evidence.</div><div>RELEVANT STORIES: unrelated outage</div></article>'
        self.assertEqual(article_body_text(BeautifulSoup(html, 'html.parser')), 'Subsea cable cut evidence.')

    def test_dynamic_shell_never_counts_as_fetched_body(self):
        scraper = GoogleNewsArticleScraper(Config(article_queue_path=''))
        shell = response(200, '<main>' + 'Newsletter navigation ' * 80 + '</main>')
        with patch.object(scraper, 'get', return_value=shell), patch.dict('os.environ', {'MONITOR_BROWSER_FALLBACK': '0'}):
            with self.assertRaisesRegex(ValueError, 'dynamic_article_body_missing'):
                scraper.extract_page_text('https://www.abs-cbn.com/news/story')
        hydrated = response(200, '<div id="bodyTopPart">' + 'A submarine cable cut caused service disruption. ' * 10 + '</div>')
        with patch.object(scraper, 'get', return_value=shell), patch.object(scraper, '_render_get', return_value=hydrated), patch.dict('os.environ', {'MONITOR_BROWSER_FALLBACK': '1'}):
            self.assertNotIn('Newsletter', scraper.extract_page_text('https://www.abs-cbn.com/news/story')['content'])

    def test_subscription_page_remains_unprocessed(self):
        scraper = ArticleScraper(Config(article_queue_path=''))
        with patch.object(scraper, 'get', return_value=response(200, '<main>Subscribe to unlock this article</main>')):
            with self.assertRaisesRegex(ValueError, 'article_requires_subscription'):
                scraper.extract_page_text('https://www.ft.com/content/example')

    def test_streamed_body_is_reassembled_without_recommendations(self):
        html = '''<article><p>Intro.</p><template id="B:1"></template></article>
        <aside><template id="B:2"></template></aside>
        <div hidden id="S:1"><p>Actual cable was cut.</p><template id="P:3"></template></div>
        <div hidden id="S:2"><p>Unrelated WACS recommendation.</p></div>
        <div hidden id="S:3"><p>Restored Friday.</p></div>
        <script>$RC("B:1","S:1");$RC("B:2","S:2");$RS("S:3","P:3");</script>'''
        body = article_body_text(BeautifulSoup(html, 'html.parser'))
        self.assertIn('Actual cable was cut.', body)
        self.assertIn('Restored Friday.', body)
        self.assertNotIn('WACS', body)

    def test_403_tries_browser_compatible_transport_once(self):
        scraper = ArticleScraper(Config(article_queue_path=''))
        with patch.object(scraper.session, 'get', return_value=response(403)) as plain, patch.object(scraper, '_browser_get', return_value=response()) as browser, patch('time.sleep'):
            self.assertEqual(scraper.get('https://publisher.example/story').status_code, 200)
            plain.assert_called_once()
            browser.assert_called_once()
            scraper.get('https://publisher.example/another')
            plain.assert_called_once()
            self.assertEqual(browser.call_count, 2)

    def test_google_news_publisher_uses_shared_recovery(self):
        scraper = GoogleNewsArticleScraper(Config(article_queue_path=''))
        with patch.object(scraper.session, 'get', return_value=response(403)), patch.object(scraper, '_browser_get', return_value=response()) as browser, patch('time.sleep'):
            self.assertEqual(scraper.get('https://publisher.example/story').status_code, 200)
            browser.assert_called_once()
        with patch.object(scraper.session, 'get', return_value=response()) as rss, patch.object(scraper, '_browser_get') as browser:
            scraper.get('https://news.google.com/rss/search?q=cable')
            rss.assert_called_once()
            browser.assert_not_called()

    def test_two_http_transport_failures_can_use_normal_browser(self):
        from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
        scraper = ArticleScraper(Config(article_queue_path=''))
        with patch.object(scraper.session, 'get', side_effect=requests.ConnectionError()), patch.object(scraper, '_browser_get', side_effect=CurlConnectionError('connection failed')), patch.object(scraper, '_render_get', return_value=response()) as render, patch.dict('os.environ', {'MONITOR_BROWSER_FALLBACK': '1'}):
            self.assertEqual(scraper.get('https://publisher.example/story').status_code, 200)
            render.assert_called_once()

    def test_localized_challenge_is_not_article_content(self):
        self.assertTrue(ArticleScraper._blocked_html('<title>请稍候…</title>正在进行安全验证'))

    def test_429_respects_retry_after_across_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(article_queue_path=str(Path(tmp) / 'queue.json'))
            scraper = ArticleScraper(config)
            with patch.object(scraper.session, 'get', return_value=response(429, headers={'Retry-After': '3600'})) as get, patch.object(scraper, '_browser_get') as browser:
                with self.assertRaises(requests.HTTPError): scraper.get('https://publisher.example/story')
                get.assert_called_once()
                browser.assert_not_called()
            again = ArticleScraper(config)
            with patch.object(again.session, 'get') as get:
                with self.assertRaises(requests.HTTPError): again.get('https://publisher.example/another')
                get.assert_not_called()

    def test_verification_page_never_becomes_article_success(self):
        scraper = ArticleScraper(Config(article_queue_path=''))
        challenge = response(202, '<title>Just a moment...</title>Verify you are human')
        with patch.object(scraper.session, 'get', return_value=challenge), patch.object(scraper, '_browser_get', return_value=challenge), patch('time.sleep'), patch.dict('os.environ', {'MONITOR_BROWSER_FALLBACK': '0'}):
            with self.assertRaises(requests.HTTPError): scraper.get('https://publisher.example/story')

    def test_browser_render_is_explicit_and_preserves_failure(self):
        scraper = ArticleScraper(Config(article_queue_path=''))
        with patch.object(scraper.session, 'get', return_value=response(403)), patch.object(scraper, '_browser_get', return_value=response(403)), patch.object(scraper, '_render_get', return_value=response(403)) as render, patch('time.sleep'), patch.dict('os.environ', {'MONITOR_BROWSER_FALLBACK': '1'}):
            with self.assertRaises(requests.HTTPError): scraper.get('https://publisher.example/story')
            render.assert_called_once()

    def test_html_200_challenge_is_typed_and_not_retried_as_runtime_error(self):
        scraper = ArticleScraper(Config(article_queue_path=''))
        challenge = response(200, '<title>Just a moment...</title>Verify you are human')
        with patch.object(scraper.session, 'get', return_value=challenge) as plain, patch.object(scraper, '_browser_get', return_value=challenge), patch('time.sleep'), patch.dict('os.environ', {'MONITOR_BROWSER_FALLBACK': '0'}):
            with self.assertRaises(PublisherAccessError) as caught:
                scraper.get('https://publisher.example/story')
            plain.assert_called_once()
        from src.utils.audit import error_details
        self.assertEqual(scraper._publisher_failure(error_details(caught.exception)), 'publisher_access_blocked')

    def test_broken_fallback_does_not_erase_known_access_restriction(self):
        scraper = ArticleScraper(Config(article_queue_path=''))
        with patch.object(scraper.session, 'get', return_value=response(403)) as plain, patch.object(scraper, '_browser_get', side_effect=requests.ConnectionError()), patch.object(scraper, '_render_get', side_effect=TimeoutError()), patch('time.sleep'), patch.dict('os.environ', {'MONITOR_BROWSER_FALLBACK': '1'}):
            with self.assertRaises(PublisherAccessError) as caught:
                scraper.get('https://publisher.example/story')
            plain.assert_called_once()
            self.assertEqual(caught.exception.response.status_code, 403)

    def test_category_access_restriction_keeps_feed_fallback_without_runtime_alert(self):
        scraper = SubTelForumScraper(Config(article_queue_path=''))
        scraper.fetch_enabled = False
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditRun(tmp); audit.activate()
            try:
                with patch.object(scraper, 'get', side_effect=PublisherAccessError(response=response(202))), patch.object(scraper, '_discover_urls_from_feed', return_value=['https://subtelforum.com/cable-cut/']) as feed:
                    scraper.scrape_articles()
                    feed.assert_called_once()
            finally:
                summary = audit.finish({'runtime_status':'success','status':'success'})
            self.assertEqual(audit.failures, 0)
            self.assertEqual(summary['unavailable_evidence'][0]['http_status'], 202)
            self.assertFalse(build_report(summary).active)

    def test_duplicate_layers_count_urls_not_log_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditRun(tmp)
            for i in range(301):
                fields = {'source':'Submarine Networks', 'url':f'https://example.com/cable/{i}'}
                audit.emit('cable_page', 'failed', reason='empty_or_challenge_page', **fields)
                audit.emit('discover', 'failed', reason='cable_page_fetch_error', **fields)
            audit.emit('discover', 'failed', source='Submarine Networks', reason='no_articles_in_seed_traversal')
            for i in range(17):
                audit.emit('fetch', 'failed', source='Google News', url=f'https://example.com/news/{i}', reason='article_fetch_error')
            self.assertEqual(audit.failures, 318)
            summary = audit.finish({'status':'degraded','warnings':319,'queue':{'retry':22,'review':12,'pending':0}})
            self.assertEqual(summary['failure_summary']['article_fetch']['count'], 17)
            self.assertEqual(summary['failure_summary']['source_discovery']['count'], 301)
            report = build_report(summary)
            self.assertIn('Google News：17', report.body)
            self.assertIn('Submarine Networks：301', report.body)
            self.assertNotIn('采集异常计数', report.body)

    def test_legacy_count_reason_fetch_is_not_article_fetch_stage(self):
        summary = {'status':'degraded','queue':{'retry':22,'review':12,'pending':0},
                   'counts':{'discover.failed.cable_page_fetch_error':301,'fetch.failed.article_fetch_error':17}}
        body = build_report(summary).body
        self.assertIn('正文获取失败，新闻仍需重试或复核：17', body)
        self.assertNotIn('正文获取失败，新闻仍需重试或复核：318', body)

if __name__ == '__main__':
    unittest.main()
