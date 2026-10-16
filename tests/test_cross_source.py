import copy
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.processing.cross_source import CrossSourceRecovery, publisher_group, validate_review
from src.processing.extractor import EventExtractor, ExtractionUnavailable
from src.pipeline import MonitorPipeline
from src.storage.article_queue import ArticleQueue
from src.storage.article_state import ArticleState
from src.storage.event_store import EventStore
from src.utils.config import Config

Q1 = 'AAG cable outage affected Vietnam on October 2, 2026.'
Q2 = 'Vietnam experienced an outage of the AAG submarine cable on October 2, 2026.'


def articles():
    return [dict(url='https://www.gmanetwork.com/news/a', title='AAG outage Vietnam', published_date='2026-10-03', source='Cross-source recovery',
                 content=Q1 + ' The reporter interviewed engineers in Hanoi about the loss of connectivity and measured packet loss on several international routes. Redundant circuits helped customers continue their work, while technicians investigated the offshore fault. This report independently confirms the operator advisory.'),
            dict(url='https://newsbytes.ph/b', title='Vietnam AAG cable outage', published_date='2026-10-04', source='Cross-source recovery',
                 content=Q2 + ' Independent traffic observations show a sharp drop in throughput. A university laboratory documented the impact at multiple research facilities. Businesses described interruptions to overseas video calls. The publisher obtained a separate confirmation from the network operations center.')]


def review():
    return dict(decision='corroborated', reason='insufficient_evidence', independent_reporting=True, original_scope_matched=True, conflict=False,
                events=[dict(cable_name='AAG', accident_location='Vietnam', affected_area='Vietnam', accident_type='Limit', occurrence_date='2026-10-02',
                             original_text_zh='AAG 海缆故障影响越南。', supports=[dict(source_index=0, quote=Q1, date_quote=Q1), dict(source_index=1, quote=Q2, date_quote=Q2)])])


class CrossSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = Config(article_queue_path=str(self.root/'queue.json'), article_state_path=str(self.root/'state.json'),
                             event_store_path=str(self.root/'events.jsonl'), audit_dir=str(self.root/'audit'), cross_source_enabled=True)
        self.queue = ArticleQueue.from_config(self.config)
        self.parent = self.queue.enqueue(dict(source='Google News', url='https://mb.com.ph/blocked', discovered_url='https://mb.com.ph/blocked', title='AAG cable outage affects Vietnam', published_date='2026-10-03'))
        self.queue.mark_retry(self.parent, 'article_fetch_error', delay_seconds=0)
        self.queue.persist(); self.parent = self.queue.get(self.parent)
        self.state = ArticleState.from_config(self.config)
        self.store = EventStore(self.config.event_store_path)
        self.extractor = EventExtractor(); self.extractor.api_key = 'fake'; self.extractor.provider = 'siliconflow'
        self.recovery = CrossSourceRecovery(self.config, self.queue, self.extractor, self.store, self.state, deadline=time.monotonic()+600)

    def validate(self, decision=None, sources=None):
        return validate_review(self.parent, sources or articles(), decision if decision is not None else review(), self.extractor)

    def test_two_independent_quoted_sources_keep_real_urls_and_no_original_body_claim(self):
        events, reason = self.validate()
        self.assertEqual(reason, 'corroborated'); self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event['url'], articles()[0]['url'])
        self.assertNotIn(self.parent['url'], event['evidence_urls'])
        self.assertFalse(event['raw_data']['cross_source']['original_full_text_read'])
        self.assertIsNone(event['repair_date']); self.assertFalse(event['reason'])

    def test_three_unchanged_evidence_gaps_stop_without_done(self):
        with patch.object(self.recovery,'retrieve',return_value=([], 'insufficient_sources')):
            for _ in range(3):
                self.recovery.process(self.parent)
        self.assertEqual(self.parent['status'],'unresolved')
        self.assertFalse(self.state.is_processed(self.parent['url']))
        self.assertEqual(self.recovery.eligible(),[])

    def test_search_fault_never_archived_as_missing_data(self):
        with patch.object(self.recovery,'retrieve',return_value=([], 'search_failed')):
            for _ in range(4):
                self.recovery.process(self.parent)
        self.assertEqual(self.parent['status'],'retry')
        self.assertNotIn('unchanged_evidence_attempts',self.parent['cross_source'])

    def test_candidate_exhaustion_preserves_parent_and_missing_reason(self):
        self.parent['candidate_reviews']=[dict(candidate_index=0,evidence_excerpt=Q1,outcome='rejected')]
        with patch.object(self.recovery,'retrieve',return_value=([], 'insufficient_sources')):
            for _ in range(3):
                self.recovery.process(self.parent)
        self.assertEqual(self.parent['status'],'unresolved')
        self.assertEqual(self.parent['candidate_reviews'][0]['recovery']['reason'],'insufficient_sources')
        self.assertFalse(self.state.is_processed(self.parent['url']))

    def test_manual_exclusion_enforced_even_if_model_returns_corroborated(self):
        self.parent['manual_review'] = {'excluded_alternative_sources': [{'url': articles()[1]['url']}]}
        self.assertEqual(self.validate()[1], 'manually_excluded_source')

    def test_candidate_review_is_eligible_and_partial_success_never_completes_parent(self):
        self.queue.mark_review(self.parent, 'candidates_rejected')
        self.parent['candidate_reviews'] = [dict(candidate_index=i, cable_name='AAG', accident_location='Vietnam',
            evidence_excerpt=Q1, claimed_occurrence_date='2026-10-02', outcome='rejected') for i in range(2)]
        self.assertIn(self.parent, self.recovery.eligible())
        with patch.object(self.recovery, 'retrieve', return_value=(articles(), 'ready')), patch.object(self.extractor, 'review_cross_sources', return_value=review()):
            result = self.recovery.process(self.parent)
        self.assertEqual(result['inserted'], 1)
        self.assertEqual(self.parent['status'], 'review')
        self.assertEqual(self.parent['candidate_reviews'][0]['recovery']['status'], 'verified')
        self.assertNotIn('recovery', self.parent['candidate_reviews'][1])
        self.assertFalse(self.state.is_processed(self.parent['url']))
        self.assertEqual(len(EventStore(self.config.event_store_path).events), 1)

    def test_all_candidates_verified_completes_parent_after_store(self):
        self.queue.mark_review(self.parent, 'candidates_rejected')
        self.parent['candidate_reviews'] = [dict(candidate_index=0, cable_name='AAG', accident_location='Vietnam',
            evidence_excerpt=Q1, claimed_occurrence_date='2026-10-02', outcome='rejected')]
        with patch.object(self.recovery, 'retrieve', return_value=(articles(), 'ready')), patch.object(self.extractor, 'review_cross_sources', return_value=review()):
            self.recovery.process(self.parent)
        self.assertEqual(self.parent['status'], 'done')
        self.assertTrue(self.state.is_processed(self.parent['url']))

    def test_candidate_balance_failure_keeps_unfinished_work(self):
        self.queue.mark_review(self.parent, 'candidates_rejected')
        self.parent['candidate_reviews']=[dict(candidate_index=0,evidence_excerpt=Q1,outcome='rejected')]
        with patch.object(self.recovery,'retrieve',return_value=(articles(),'ready')), patch.object(self.extractor,'review_cross_sources',side_effect=ExtractionUnavailable('balance_exhausted',provider='siliconflow',http_status=402)):
            with self.assertRaises(ExtractionUnavailable):self.recovery.process(self.parent)
        self.assertEqual(self.parent['status'],'review')
        self.assertFalse(self.state.is_processed(self.parent['url']))
        self.assertEqual(self.store.events,[])

    def test_manual_exclusion_keeps_article_query_identity(self):
        from src.processing.cross_source import excluded_source
        self.parent['manual_review']={'excluded_alternative_sources':[{'url':'https://www.cga.gov.tw/news?id=1'}]}
        self.assertTrue(excluded_source(self.parent,'https://www.cga.gov.tw/news?id=1&utm_source=test'))
        self.assertFalse(excluded_source(self.parent,'https://www.cga.gov.tw/news?id=2'))

    def test_unread_original_with_manual_scope_hold_saves_evidence_without_completion(self):
        self.parent['manual_review']={'completion_authorized':False,'missing_evidence':['authorized full text']}
        with patch.object(self.recovery,'retrieve',return_value=(articles(),'ready')), patch.object(self.extractor,'review_cross_sources',return_value=review()):
            result=self.recovery.process(self.parent)
        self.assertEqual(result['inserted'],1)
        self.assertEqual(self.parent['status'],'review')
        self.assertEqual(self.parent['reason'],'cross_source_manual_scope_remaining')
        self.assertFalse(self.state.is_processed(self.parent['url']))
        self.assertEqual(len(EventStore(self.config.event_store_path).events),1)

    def test_partial_manual_scope_cannot_be_auto_completed(self):
        self.queue.mark_review(self.parent, 'candidates_rejected')
        self.parent['candidate_reviews']=[dict(candidate_index=0,evidence_excerpt=Q1,outcome='rejected')]
        self.parent['manual_review']={'completion_authorized':False,'missing_evidence':['other historical claims']}
        with patch.object(self.recovery,'retrieve',return_value=(articles(),'ready')), patch.object(self.extractor,'review_cross_sources',return_value=review()):
            self.recovery.process(self.parent)
        self.assertEqual(self.parent['status'],'review')
        self.assertEqual(self.parent['candidate_reviews'][0]['recovery']['status'],'verified')

    def test_candidate_insufficient_evidence_keeps_reason_and_rotates(self):
        self.queue.mark_review(self.parent, 'candidates_rejected')
        self.parent['candidate_reviews'] = [dict(candidate_index=i, evidence_excerpt=Q1, outcome='rejected') for i in range(2)]
        with patch.object(self.recovery, 'retrieve', return_value=([], 'insufficient_sources')):
            self.recovery.process(self.parent)
            self.recovery.process(self.parent)
        self.assertEqual(self.parent['status'], 'review')
        self.assertTrue(all(c['recovery']['reason']=='insufficient_sources' for c in self.parent['candidate_reviews']))
        self.assertEqual(self.recovery.eligible(), [])

    def test_unrelated_australia_lead_rejects_philippine_bodies(self):
        from src.processing.cross_source import relevant_source
        self.parent['title'] = 'Australia banks prepare cable blackouts'
        self.assertFalse(relevant_source(self.parent, 'Globe restores internet', 'Globe restores internet in Philippines. Australia is nearby.'))

    def test_excluded_sources_never_fetched(self):
        entries=[{'link':a['url'],'title':a['title'],'published_date':a['published_date']} for a in articles()]
        self.parent['manual_review']={'excluded_alternative_sources':entries}
        # metadata uses url, not RSS link
        self.parent['manual_review']['excluded_alternative_sources']=[{'url':a['url']} for a in articles()]
        with patch.object(self.recovery.scraper,'get',return_value=SimpleNamespace(content=b'fixture')), patch('src.processing.cross_source.parse_rss_entries',return_value=entries), patch.object(self.recovery.scraper,'_resolve_google_news_url',side_effect=lambda u:u), patch.object(self.recovery.scraper,'extract_page_text') as fetch:
            found,reason=self.recovery.retrieve(self.parent,{'sources':[]})
        fetch.assert_not_called(); self.assertEqual(reason,'insufficient_sources')

    def test_historical_candidate_allows_later_primary_reporting(self):
        self.parent['rejected_candidate']={'claimed_occurrence_date':None,'evidence_excerpt':Q1}
        self.parent['published_date']='2026-10-03'
        entries=[{'link':a['url'],'title':a['title'],'published_date':'2024-10-03'} for a in articles()]
        pages=[{**a,'published_date':'2024-10-03'} for a in articles()]
        record={'sources':[]}
        with patch.object(self.recovery.scraper,'get',return_value=SimpleNamespace(content=b'fixture')), patch('src.processing.cross_source.parse_rss_entries',return_value=entries), patch.object(self.recovery.scraper,'_resolve_google_news_url',side_effect=lambda u:u), patch.object(self.recovery.scraper,'extract_page_text',side_effect=pages):
            found, reason=self.recovery.retrieve(self.parent,record)
        self.assertEqual(reason,'ready')
        self.assertEqual(len(found),2)
        self.assertTrue(all('after:' not in query for query in record['queries']))
        # Retrieval does not confirm an occurrence or complete the parent.
        self.assertNotEqual(self.parent['status'],'done')
        self.assertFalse(self.store.events)

    def test_resolver_access_failure_remains_runtime_failure(self):
        import requests
        from src.utils.audit import AuditRun
        response=requests.Response();response.status_code=401
        record={'sources':[]}
        audit=AuditRun(self.root/'audit');audit.activate()
        try:
            with patch.object(self.recovery.scraper,'get',return_value=SimpleNamespace(content=b'fixture')), patch('src.processing.cross_source.parse_rss_entries',return_value=[{'link':'https://news.google.com/rss/articles/opaque'}]), patch.object(self.recovery.scraper,'_resolve_google_news_url',side_effect=requests.HTTPError(response=response)):
                self.recovery.retrieve(self.parent,record)
        finally:
            summary=audit.finish({})
        self.assertEqual(audit.failures,1)
        self.assertFalse(summary['unavailable_evidence'])

    def test_optional_access_failure_keeps_gap_without_runtime_failure(self):
        import requests
        from src.utils.audit import AuditRun
        for status in (401, 403, 404, 410, 451, 429, 500):
            with self.subTest(status=status):
                response = requests.Response(); response.status_code = status
                error = requests.HTTPError("private response", response=response)
                link = "https://news.google.com/rss/articles/opaque"
                resolved = articles()[0]["url"]
                entries = [{"link":link,"title":"AAG outage Vietnam","published_date":"2026-10-03"}]
                record = {"sources":[]}
                audit = AuditRun(self.root/'audit'); audit.activate()
                try:
                    with patch.object(self.recovery.scraper,'get',return_value=SimpleNamespace(content=b'fixture')), patch('src.processing.cross_source.parse_rss_entries',return_value=entries), patch.object(self.recovery.scraper,'_resolve_google_news_url',return_value=resolved), patch.object(self.recovery.scraper,'extract_page_text',side_effect=error):
                        found, reason = self.recovery.retrieve(self.parent,record)
                finally:
                    summary = audit.finish({})
                self.assertEqual(reason,'insufficient_sources')
                self.assertFalse(found)
                self.assertNotEqual(self.parent['status'],'done')
                self.assertFalse(self.state.is_processed(self.parent['url']))
                unavailable = status in (401,403,404,410,451)
                self.assertEqual(audit.failures,0 if unavailable else 1)
                items = summary['unavailable_evidence' if unavailable else 'failure_details']
                self.assertEqual(len(items),1)
                self.assertEqual(items[0]['url'],resolved)
                self.assertEqual(items[0]['parent_queue_id'],self.parent['queue_id'])
                self.assertEqual(items[0]['http_status'],status)
                self.assertNotIn('private response',json.dumps(record))

    def test_untrusted_and_tricky_domains_are_rejected(self):
        for url in ('http://newsbytes.ph/a', 'https://newsbytes.ph.attacker.com/a', 'https://u:p@newsbytes.ph/a',
                    'https://127.0.0.1/a', 'https://newsbytes.ph:99/a', 'https://unknown.example/a'):
            self.assertIsNone(publisher_group(url), url)
        sources = articles(); sources[1]['url'] = 'https://unknown.example/a'
        self.assertEqual(self.validate(sources=sources)[1], 'untrusted_source')

    def test_same_publisher_and_syndicated_groups_cannot_count_twice(self):
        sources = articles(); sources[0]['url']='https://newsinfo.inquirer.net/a'; sources[1]['url']='https://www.thestar.com.my/b'
        self.assertEqual(self.validate(sources=sources)[1], 'insufficient_independent_publishers')
        result=review(); result['independent_reporting']=False
        self.assertEqual(self.validate(result)[1], 'syndicated_sources')

    def test_reprinted_body_rejected_even_if_model_claims_independence(self):
        sources=articles(); sources[0]['content']=sources[1]['content']=Q1+' '+Q2+' '+('Repeated statement. '*30)
        self.assertEqual(self.validate(sources=sources)[1], 'syndicated_sources')

    def test_fabricated_quote_cannot_complete_parent(self):
        result=review(); result['events'][0]['supports'][1]['quote']='AAG was severed somewhere else.'
        self.assertEqual(self.validate(result)[1], 'quote_not_in_body')

    def test_wrong_date_and_publication_date_cannot_be_substituted(self):
        result=review(); result['events'][0]['occurrence_date']='2026-10-03'
        self.assertEqual(self.validate(result)[1], 'occurrence_date_not_supported')
        result=review(); result['events'][0]['supports'][1]['date_quote']='Published October 2, 2026'
        sources=articles(); sources[1]['content']+=' Published October 2, 2026'
        self.assertEqual(self.validate(result,sources)[1], 'occurrence_date_not_supported')

    def test_conflict_is_retained_even_with_model_candidates(self):
        result=review(); result.update(conflict=True,reason='date_conflict')
        self.assertEqual(self.validate(result),([], 'date_conflict'))
        result.update(reason='identity_conflict')
        self.assertEqual(self.validate(result)[1], 'identity_conflict')

    def test_manual_date_conflict_needs_primary_correction(self):
        self.parent['manual_review']={'requires_primary_correction':True}
        self.assertEqual(self.validate()[1], 'primary_correction_required')

    def test_topic_only_or_empty_events_cannot_clear_queue(self):
        for reason in ('no_confirmed_incident','original_scope_unknown','different_incident'):
            result=review(); result.update(decision='review',reason=reason)
            self.assertEqual(self.validate(result),([],reason))
        result=review(); result['events']=[]
        self.assertEqual(self.validate(result)[1],'no_confirmed_incident')

    def test_backup_cable_name_and_cut_without_damage_evidence_rejected(self):
        result=review(); result['events'][0]['cable_name']='Bifrost'
        self.assertEqual(self.validate(result)[1],'cable_identity_not_supported')
        result=review(); result['events'][0]['accident_type']='Cut'
        self.assertEqual(self.validate(result)[1],'physical_cut_not_supported')

    def test_invalid_source_references_do_not_pass(self):
        for index in (-1, 0, 99, True, '1'):
            result=review(); result['events'][0]['supports'][1]['source_index']=index
            self.assertEqual(self.validate(result)[1],'invalid_source_index')

    def test_unrelated_original_title_rejected(self):
        self.parent['title']='Globe Singapore restoration'
        self.assertEqual(self.validate()[1],'original_identity_not_supported')

    def test_store_written_before_queue_done_and_cache(self):
        with patch.object(self.recovery,'retrieve',return_value=(articles(),'ready')), patch.object(self.extractor,'review_cross_sources',return_value=review()):
            result=self.recovery.process(self.parent)
        self.assertEqual(result['inserted'],1)
        self.assertEqual(ArticleQueue.from_config(self.config).get(self.parent)['status'],'done')
        self.assertTrue(ArticleState.from_config(self.config).is_processed(self.parent['url']))
        self.assertEqual(len(EventStore(self.config.event_store_path).events),1)

    def test_store_failure_never_acknowledges_or_caches_original(self):
        with patch.object(self.recovery,'retrieve',return_value=(articles(),'ready')), patch.object(self.extractor,'review_cross_sources',return_value=review()), patch.object(self.store,'save',side_effect=OSError('disk')):
            with self.assertRaises(OSError):self.recovery.process(self.parent)
        self.assertEqual(len(self.store.events),0)
        self.assertNotEqual(ArticleQueue.from_config(self.config).get(self.parent)['status'],'done')
        self.assertFalse(self.state.is_processed(self.parent['url']))

    def test_merge_preserves_cross_source_provenance(self):
        self.store.upsert(self.validate()[0][0]); self.store.save()
        with patch.object(self.recovery,'retrieve',return_value=(articles(),'ready')), patch.object(self.extractor,'review_cross_sources',return_value=review()):
            result=self.recovery.process(self.parent)
        self.assertEqual(result['merged'],1)
        self.assertTrue(self.store.events[0]['raw_data']['cross_source_reviews'])

    def test_insufficient_evidence_has_reason_cooldown_and_no_success_cache(self):
        with patch.object(self.recovery,'retrieve',return_value=(articles()[:1],'insufficient_sources')), patch.object(self.extractor,'review_cross_sources') as judge:
            self.recovery.process(self.parent)
        judge.assert_not_called(); self.assertEqual(self.parent['status'],'review')
        self.assertEqual(self.parent['reason'],'cross_source_insufficient_sources')
        self.assertFalse(self.state.is_processed(self.parent['url'])); self.assertEqual(self.recovery.eligible(),[])

    def test_search_failure_remains_retry_and_review_empty_is_not_done(self):
        with patch.object(self.recovery,'retrieve',return_value=([],'search_failed')):
            self.recovery.process(self.parent)
        self.assertEqual(self.parent['status'],'retry')
        self.assertEqual(self.parent['reason'],'cross_source_search_failed')

    def test_model_only_timeout_not_eligible_for_source_recovery(self):
        self.queue.mark_retry(self.parent,'request_timeout')
        self.assertEqual(self.recovery.eligible(),[])

    def test_dry_run_does_not_change_persisted_queue_store_or_cache(self):
        self.queue.dry_run=True; self.recovery.dry_run=True
        before=Path(self.config.article_queue_path).read_bytes()
        with patch.object(self.recovery,'retrieve',return_value=(articles(),'ready')), patch.object(self.extractor,'review_cross_sources',return_value=review()):
            self.recovery.process(self.parent)
        self.assertEqual(Path(self.config.article_queue_path).read_bytes(),before)
        self.assertFalse(self.store.path.exists()); self.assertFalse(self.state.path.exists())

    def test_retrieval_persists_alternative_urls_for_full_normal_processing(self):
        entries=[{'link':a['url'],'title':a['title'],'published_date':a['published_date']} for a in articles()]
        pages=[{'final_url':a['url'],'title':a['title'],'published_date':a['published_date'],'content':a['content']} for a in articles()]
        record={'sources':[]}
        with patch.object(self.recovery.scraper,'get',return_value=SimpleNamespace(content=b'fixture')), patch('src.processing.cross_source.parse_rss_entries',return_value=entries), patch.object(self.recovery.scraper,'_resolve_google_news_url',side_effect=lambda u:u), patch.object(self.recovery.scraper,'extract_page_text',side_effect=pages):
            found, reason=self.recovery.retrieve(self.parent,record)
        self.assertEqual(reason,'ready'); self.assertEqual(len(found),2)
        for a in articles():
            row=ArticleQueue.from_config(self.config).get(a['url']); self.assertEqual(row['status'],'pending')
            self.assertIn(self.parent['queue_id'],row['cross_source_parents'])
        self.assertTrue(all('content' not in source for source in record['sources']))

    def test_search_audit_overrides_original_keyword_without_false_failure(self):
        self.parent['keyword'] = 'original discovery query'
        record = {'sources': []}
        with patch.object(self.recovery.scraper, 'get', return_value=SimpleNamespace(content=b'fixture')), patch('src.processing.cross_source.parse_rss_entries', return_value=[]), patch('src.processing.cross_source.emit') as audit:
            found, reason = self.recovery.retrieve(self.parent, record)
        searches = [call for call in audit.call_args_list if call.args[0] == 'cross_source_search']
        self.assertEqual(len(searches), 2)
        self.assertTrue(all(call.args[1] == 'completed' for call in searches))
        self.assertEqual([call.kwargs['query'] for call in searches], record['queries'])
        self.assertEqual(reason, 'insufficient_sources')

    def test_model_402_during_recovery_pauses_all_further_calls(self):
        other=self.queue.enqueue(dict(source='Google News',url='https://mb.com.ph/other',title='AAG outage Vietnam',published_date='2026-10-03'))
        self.queue.mark_review(other,'cross_source_insufficient_sources'); self.queue.persist()
        null_scraper=SimpleNamespace(scrape_articles=lambda **k:[])
        pipeline=MonitorPipeline(self.config,self.store,self.extractor,health_checker=lambda *a:{'available':True,'provider':'siliconflow'})
        with patch('src.pipeline.SCRAPER_CLASSES',{'fixture':lambda c:null_scraper}), patch.object(CrossSourceRecovery,'retrieve',return_value=(articles(),'ready')), patch.object(self.extractor,'review_cross_sources',side_effect=ExtractionUnavailable('balance_exhausted',http_status=402,provider='siliconflow')) as judge:
            summary=pipeline.run(['fixture'])
        judge.assert_called_once()
        q=ArticleQueue.from_config(self.config)
        self.assertTrue(q.balance_paused()); self.assertEqual(summary.model_health['reason'],'balance_exhausted')
        row=q.get(self.parent); self.assertEqual(row['status'],'retry')
        self.assertEqual(row['cross_source']['next_search_at'],q.model_pause['retry_at'])
        self.assertEqual(q.get(other)['status'],'review')

    def test_unchanged_evidence_does_not_repeat_paid_review(self):
        def retrieve(row, record):
            record["sources"] = [{"url": a["url"], "published_date": a["published_date"], "title": a["title"], "content_sha256": str(i)} for i, a in enumerate(articles())]
            return articles(), "ready"
        response = review(); response.update(decision="review", reason="original_scope_unknown")
        with patch.object(self.recovery, "retrieve", side_effect=retrieve), patch.object(self.extractor, "review_cross_sources", return_value=response) as judge:
            self.recovery.process(self.parent)
            self.recovery.process(self.parent)
        judge.assert_called_once()
        self.assertEqual(self.parent["status"], "review")
        self.assertEqual(self.parent["reason"], "cross_source_original_scope_unknown")

    def test_no_model_call_when_review_cannot_fit_remaining_time(self):
        self.recovery.deadline = time.monotonic() + 10
        with patch.object(self.recovery, "retrieve", return_value=(articles(), "ready")), patch.object(self.extractor, "review_cross_sources") as judge:
            self.recovery.process(self.parent)
        judge.assert_not_called()
        self.assertEqual(self.parent["reason"], "cross_source_review_time_limit")
        self.assertNotEqual(self.parent["status"], "done")

    def test_balanced_pause_skips_recovery_before_any_model_call(self):
        self.queue.pause_for_balance(); self.queue.persist()
        null_scraper=SimpleNamespace(scrape_articles=lambda **k:[])
        pipeline=MonitorPipeline(self.config,self.store,self.extractor,health_checker=lambda *a:{"available":True,"provider":"siliconflow"})
        with patch("src.pipeline.SCRAPER_CLASSES", {"fixture":lambda c:null_scraper}), patch.object(CrossSourceRecovery, "process") as process:
            summary=pipeline.run(["fixture"])
        process.assert_not_called()
        self.assertEqual(summary.model_health["reason"], "balance_exhausted")

    def test_timeout_config_is_used_without_sdk_retries(self):
        with patch('openai.OpenAI') as client:
            self.extractor.request_timeout=120; self.extractor._client_instance()
        self.assertEqual(client.call_args.kwargs['timeout'],120)
        self.assertEqual(client.call_args.kwargs['max_retries'],0)
