import contextlib
import io
import json
from pathlib import Path
import smtplib
import ssl
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

from scripts import notify_monitor as notify


def healthy(**changes):
    result = {"status": "success", "errors": 0, "warnings": 0,
              "queue": {"pending": 0, "retry": 0, "review": 0}, "model_health": {"status": "healthy"}}
    result.update(changes)
    return result


def github_env():
    return {"GITHUB_TOKEN": "private-token", "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_RUN_ID": "123", "ALERT_GITHUB_USER": "explicit-recipient"}


def email_env():
    return {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587",
            "SMTP_FROM": "sender@example.com", "ALERT_EMAIL_TO": "recipient@example.com",
            "SMTP_USERNAME": "private-user", "SMTP_PASSWORD": "private-password"}


def response(value, *, status=200):
    result = MagicMock()
    result.content = b"json"
    result.json.return_value = value
    result.status_code = status
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError("private provider error", response=result)
    return result


class ReportTests(unittest.TestCase):
    def detailed(self, rows, **kwargs):
        summary=healthy(runtime_status='success',review_status='needs_review',queue_items=rows,queue_changes={},failure_details=[])
        summary.update(kwargs)
        return notify.build_report(summary)

    def test_new_reviews_and_retries_never_trigger_notifications(self):
        for status in ('review','retry','unresolved'):
            row={'queue_id':'new','title':'Missing evidence','url':'https://example.org/a','status':status,'reason':'publisher_access_blocked'}
            report=self.detailed([row],queue={status:1})
            self.assertFalse(report.active)
            self.assertFalse(notify.due(report,None,1))
            self.assertNotIn('Missing evidence',report.body)

    def test_legacy_queue_alert_state_is_not_a_reason_to_send_recovery(self):
        report=self.detailed([])
        for conditions in (['queue.review','status'],['review:old','retry:old']):
            self.assertFalse(notify.due(report,{'fingerprint':'old','conditions':conditions,'last_sent':1},2))

    def test_discovery_progress_and_unavailable_sources_are_log_only(self):
        report=self.detailed([],counts={'discover.deferred.discovery_budget':1},queue={'unresolved':8},
            queue_changes={'closed_unresolved':[{'queue_id':'a'}]},
            unavailable_evidence=[{'title':'Alternate source','url':'https://example.org/a','http_status':401,'parent_queue_id':'parent'}])
        self.assertFalse(report.active)
        for text in ('Alternate source','停止核查','续扫'):
            self.assertNotIn(text,report.body)

    def test_runtime_mail_excludes_unrelated_data_gaps(self):
        row={'queue_id':'a','title':'Blocked news','status':'review','reason':'publisher_access_blocked'}
        failure={'stage':'rss','url':'https://news.google.com/rss/search','reason':'rss_fetch_error','http_status':500}
        report=self.detailed([row],runtime_status='degraded',failure_details=[failure])
        message=notify.with_change_details(report,None)
        self.assertTrue(report.active)
        self.assertIn('HTTP 500',message.body)
        self.assertNotIn('Blocked news',message.body)
        self.assertFalse(any(k.startswith(('review:','retry:')) for k in report.conditions))

    def test_gateway_notification_explains_remote_failure(self):
        failure = {'stage':'cross_source_fetch','url':'https://example.org/a','reason':'alternative_fetch_failed','http_status':502}
        report = self.detailed([], runtime_status='degraded', failure_details=[failure])
        body = notify.with_change_details(report, None).body
        self.assertTrue(report.active)
        self.assertIn('补证来源获取失败', body)
        self.assertIn('HTTP 502', body)
        self.assertIn('来源网站或上游网关暂时不可用', body)

    def test_url_failure_dedup_and_new_url_alerts(self):
        failure={'stage':'discover','source':'Submarine Networks','url':'https://example.org/a','reason':'cable_page_fetch_error','http_status':404}
        first=self.detailed([],runtime_status='degraded',failure_details=[failure])
        second=self.detailed([],runtime_status='degraded',failure_details=[failure,{**failure,'url':'https://example.org/b'}])
        previous={'fingerprint':first.fingerprint,'last_sent':1,'conditions':list(first.conditions)}
        self.assertTrue(notify.due(second,previous,2))
        self.assertIn('HTTP 404',notify.with_change_details(second,previous).body)

    def test_detailed_balance_failure_keeps_pause_and_recharge_instructions(self):
        report=self.detailed([],runtime_status='failed',model_health={'provider':'siliconflow','status':'paused','available':False,'reason':'balance_exhausted','http_status':402,'attempt_id':'today'})
        self.assertIn('充值',report.subject); self.assertIn('次日',report.body)
        self.assertEqual(report.balance_attempt,'today')

    def test_failures_and_pending_work_are_actionable(self):
        summary = healthy(status="degraded", errors=1, warnings=2,
                          queue={"pending": 3, "retry": 4, "review": 1},
                          model_health={"status": "unavailable", "http_status": 402, "balance": "private-amount"},
                          counts={"discovery.failed": 1, "fetch.failed.http_403": 1, "extraction.fallback": 2, "selection.skipped.budget": 3})
        report = notify.build_report(summary)
        self.assertTrue(report.active)
        for text in ("模型调用因余额耗尽失败", "发现源", "需要处理"):
            self.assertIn(text, report.body)
        self.assertNotIn("private-amount", report.body)

    def test_actual_timeout_and_directory_limit_never_claim_balance_exhaustion(self):
        report = notify.build_report(healthy(status="degraded", errors=1,
            failure_summary={"model": {"count": 1}}, queue={"pending": 0, "retry": 1, "review": 0},
            model_health={"provider": "siliconflow", "status": "unavailable", "available": False, "reason": "request_timeout"},
            counts={"discover.deferred.discovery_budget": 1, "extract.failed.request_timeout": 1}), "failure")
        self.assertIn("SiliconFlow 请求超时", report.body)
        self.assertNotIn("目录发现达到本轮时长或页数上限", report.body)
        self.assertNotIn("额度已用尽", report.body)
        self.assertNotIn("充值后重试", report.body)
        self.assertNotIn("Parallel", report.body)
        self.assertNotIn("processing_limit", report.conditions)
        self.assertIn("model_request_timeout", report.conditions)

    def test_fingerprint_ignores_run_ids_processed_counts_and_private_fields(self):
        first = notify.build_report(healthy(warnings=1, articles=1, run_id="first", smtp_password="secret"), run_url="https://github.com/owner/repo/actions/runs/1")
        second = notify.build_report(healthy(warnings=1, articles=99, run_id="second", smtp_password="different"), run_url="https://github.com/owner/repo/actions/runs/2")
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertNotIn("secret", first.body)

    def test_legacy_data_diagnostics_do_not_alert(self):
        self.assertFalse(notify.build_report({'status':'success'}).active)
        for field in ('pending','retry','review','unresolved'):
            self.assertFalse(notify.build_report(healthy(status='degraded',warnings=10,queue={field:20})).active)
        self.assertFalse(notify.build_report(healthy(counts={'fetch.failed.http_403':3})).active)
        self.assertFalse(notify.build_report(healthy(model_health={'status':'unknown'})).active)

    def test_workflow_failure_and_missing_summary_cannot_recover(self):
        self.assertTrue(notify.build_report(healthy(), "failure").active)
        self.assertTrue(notify.build_report(healthy(status="failed")).active)
        self.assertTrue(notify.build_report({}, summary_error="summary_unreadable").active)

    def test_retired_balance_api_is_unknown_not_a_claim_of_model_outage(self):
        report = notify.build_report(healthy(model_health={"status": "health_unknown", "available": None,
                                                           "low_balance": False, "reason": "balance_api_retired"}))
        self.assertFalse(report.active)
        self.assertNotIn("服务商余额接口", report.body)
        self.assertNotIn("模型不可用", report.body)
        self.assertNotIn("模型余额不足", report.body)
        self.assertNotIn("充值", report.body)

    def test_low_positive_balance_does_not_claim_model_is_unavailable(self):
        report = notify.build_report(healthy(model_health={"status": "low_balance", "available": True,
                                                           "low_balance": True, "reason": "balance_below_threshold"}))
        self.assertFalse(report.active)
        self.assertNotIn("余额低于提醒阈值", report.body)
        self.assertNotIn("模型不可用", report.body)

    def test_model_failure_reason_identifies_balance_credentials_or_rate_limit(self):
        for reason, status, expected in (("balance_exhausted", 402, "模型调用因余额耗尽失败"),
                                         ("authentication_failed", 401, "模型凭据无效或无权访问"),
                                         ("rate_limited", 429, "模型请求受到限流")):
            with self.subTest(reason=reason):
                report = notify.build_report(healthy(model_health={"status": "unavailable", "available": False,
                                                                   "reason": reason, "http_status": status}))
                self.assertTrue(report.active)
                self.assertIn(expected, report.body)
                self.assertEqual(report.body.count(expected), 1)
                self.assertNotIn("模型不可用", report.body)

    def test_model_call_failure_does_not_claim_rule_fallback(self):
        report = notify.build_report(healthy(counts={"extract.failed.invalid_response": 1}))
        self.assertIn("模型调用失败：1", report.body)
        self.assertNotIn("规则回退", report.body)

    def test_rule_fallback_only_appears_for_actual_extraction_fallback(self):
        report = notify.build_report(healthy(counts={"extract.failed.invalid_response": 1, "extract.fallback.missing_api_key": 2}))
        self.assertIn("模型调用失败：1", report.body)
        self.assertIn("检测到规则回退：2", report.body)
        unrelated = notify.build_report(healthy(counts={"discover.fallback.category_fetch_error": 1, "extract.failed.rule_fallback_disabled": 1}))
        self.assertNotIn("检测到规则回退", unrelated.body)
        self.assertIn("模型调用失败：1", unrelated.body)

    def test_counts_and_elapsed_time_do_not_repeat_an_existing_incident(self):
        warning = notify.build_report(healthy(warnings=1, queue={"pending": 0, "retry": 2, "review": 1}))
        changed = notify.build_report(healthy(warnings=99, queue={"pending": 0, "retry": 25, "review": 10}))
        previous = {"fingerprint": warning.fingerprint, "last_sent": 100, "conditions": list(warning.conditions)}
        self.assertEqual(warning.fingerprint, changed.fingerprint)
        self.assertFalse(notify.due(warning, previous, 200))
        self.assertFalse(notify.due(changed, previous, 100 + 30 * 86400))
        fewer_categories = notify.build_report(healthy(warnings=1, queue={"pending": 0, "retry": 0, "review": 1}))
        self.assertFalse(notify.due(fewer_categories, previous, 300))
        good = notify.build_report(healthy())
        self.assertFalse(notify.due(good, previous, 200))
        self.assertFalse(notify.due(good, None, 200))
        self.assertFalse(notify.due(good, {"fingerprint": "healthy", "last_sent": 1}, 999999))

    def test_new_actual_balance_failure_still_alerts_immediately(self):
        before = notify.build_report(healthy(warnings=1))
        previous = {"fingerprint": before.fingerprint, "last_sent": 100, "conditions": list(before.conditions)}
        exhausted = notify.build_report(healthy(warnings=1, model_health={"status": "unavailable", "available": False, "reason": "balance_exhausted", "http_status": 402}))
        self.assertTrue(notify.due(exhausted, previous, 101))
        self.assertIn("模型调用因余额耗尽失败", exhausted.body)

    def test_unchecked_model_or_disabled_preflight_does_not_alert(self):
        for status, reason in (("not_checked", "balance_check_disabled"), ("health_unknown", "balance_check_unsupported"), ("low_balance", "balance_below_threshold")):
            with self.subTest(status=status):
                report = notify.build_report(healthy(model_health={"status": status, "available": None, "reason": reason, "low_balance": True}))
                self.assertFalse(report.active)
                self.assertNotIn("充值", report.body)


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.report = notify.build_report(healthy(errors=1))

    @patch.object(notify.smtplib, "SMTP")
    def test_starttls_precedes_password_and_checks_certificates(self, smtp):
        client = smtp.return_value.__enter__.return_value
        client.send_message.return_value = {}
        notify.send_email(self.report, email_env())
        methods = [call[0] for call in client.method_calls]
        self.assertEqual(methods, ["ehlo", "starttls", "ehlo", "login", "send_message"])
        context = client.starttls.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(client.send_message.call_args.kwargs["to_addrs"], ["recipient@example.com"])

    @patch.object(notify.smtplib, "SMTP_SSL")
    def test_port_465_uses_validated_tls(self, smtp):
        smtp.return_value.__enter__.return_value.send_message.return_value = {}
        notify.send_email(self.report, {**email_env(), "SMTP_PORT": "465"})
        self.assertEqual(smtp.call_args.args, ("smtp.example.com", 465))
        self.assertTrue(smtp.call_args.kwargs["context"].check_hostname)

    @patch.object(notify.smtplib, "SMTP")
    def test_no_plaintext_fallback_when_starttls_fails(self, smtp):
        client = smtp.return_value.__enter__.return_value
        client.starttls.side_effect = smtplib.SMTPNotSupportedError("no TLS")
        with self.assertRaises(smtplib.SMTPNotSupportedError):
            notify.send_email(self.report, email_env())
        client.login.assert_not_called()
        client.send_message.assert_not_called()

    @patch.object(notify.smtplib, "SMTP")
    def test_plaintext_port_and_header_injection_rejected_before_connect(self, smtp):
        with self.assertRaises(ValueError):
            notify.send_email(self.report, {**email_env(), "SMTP_PORT": "25"})
        with self.assertRaises(ValueError):
            notify.send_email(self.report, {**email_env(), "ALERT_EMAIL_TO": "recipient@example.com\nBcc: other@example.com"})
        smtp.assert_not_called()

    @patch.object(notify.smtplib, "SMTP")
    def test_partial_refusal_is_a_failed_delivery(self, smtp):
        smtp.return_value.__enter__.return_value.send_message.return_value = {"recipient@example.com": (550, b"refused")}
        with self.assertRaises(RuntimeError):
            notify.send_email(self.report, email_env())


class GitHubTests(unittest.TestCase):
    def test_single_issue_created_without_requiring_labels(self):
        session = MagicMock()
        session.request.side_effect = [response([]), response({"number": 7})]
        client = notify.GitHubNotifier(github_env(), session)
        self.assertTrue(client.send(notify.build_report(healthy(errors=1))))
        create = session.request.call_args_list[1]
        self.assertEqual(create.args, ("POST", "https://api.github.com/repos/owner/repo/issues"))
        payload = create.kwargs["json"]
        self.assertIn(notify.MARKER, payload["body"])
        self.assertIn("@explicit-recipient", payload["body"])
        self.assertNotIn("labels", payload)

    def test_changes_update_same_issue_and_comment_to_notify(self):
        session = MagicMock()
        session.request.side_effect = [response([{"number": 7, "title": notify.ISSUE_TITLE, "body": notify.MARKER, "state": "open"}]), response({}), response({})]
        client = notify.GitHubNotifier(github_env(), session)
        client.send(notify.build_report(healthy(errors=2)))
        self.assertEqual(session.request.call_args_list[1].args[0], "PATCH")
        self.assertTrue(session.request.call_args_list[2].args[1].endswith("/issues/7/comments"))

    def test_recovery_closes_existing_issue_even_if_local_state_missing(self):
        session = MagicMock()
        session.request.side_effect = [response([{"number": 7, "title": notify.ISSUE_TITLE, "body": notify.MARKER, "state": "open"}]), response({}), response({})]
        client = notify.GitHubNotifier(github_env(), session)
        self.assertTrue(client.send(notify.build_report(healthy()), recover_without_state=True))
        self.assertEqual(session.request.call_args_list[1].kwargs["json"]["state"], "closed")

    def test_no_healthy_issue_created_or_repeated_recovery_comment(self):
        for existing in ([], [{"number": 7, "title": notify.ISSUE_TITLE, "body": notify.MARKER, "state": "closed"}]):
            session = MagicMock()
            session.request.return_value = response(existing)
            self.assertFalse(notify.GitHubNotifier(github_env(), session).send(notify.build_report(healthy()), recover_without_state=True))
            self.assertEqual(session.request.call_count, 1)

    def test_untrusted_repo_or_server_and_implicit_mentions_rejected(self):
        for changes in ({"GITHUB_REPOSITORY": "owner/repo/../../other"}, {"GITHUB_SERVER_URL": "http://github.com"}, {"GITHUB_SERVER_URL": "https://token@github.com"}, {"ALERT_GITHUB_USER": "@everyone\nmalicious"}):
            with self.assertRaises(ValueError):
                notify.GitHubNotifier({**github_env(), **changes}, MagicMock())
        session = MagicMock()
        session.request.side_effect = [response([]), response({})]
        env = github_env()
        env.pop("ALERT_GITHUB_USER")
        notify.GitHubNotifier(env, session).send(notify.build_report(healthy(errors=1)))
        self.assertNotIn("@owner", session.request.call_args.kwargs["json"]["body"])

    def test_api_redirect_is_not_followed(self):
        session = MagicMock()
        session.request.return_value = response({}, status=302)
        with self.assertRaises(RuntimeError):
            notify.GitHubNotifier(github_env(), session).send(notify.build_report(healthy(errors=1)))
        self.assertIs(session.request.call_args.kwargs["allow_redirects"], False)

    def test_issue_discovered_after_first_page(self):
        session = MagicMock()
        session.request.side_effect = [response([{"title": "unrelated", "body": ""}] * 100),
                                       response([{"number": 7, "title": notify.ISSUE_TITLE, "body": notify.MARKER}])]
        self.assertEqual(notify.GitHubNotifier(github_env(), session).find_issue()["number"], 7)
        self.assertEqual(session.request.call_args.kwargs["params"]["page"], 2)

    def test_renamed_issue_is_still_deduplicated_by_marker(self):
        session = MagicMock()
        session.request.return_value = response([{"number": 7, "title": "User changed this title", "body": notify.MARKER}])
        self.assertEqual(notify.GitHubNotifier(github_env(), session).find_issue()["number"], 7)


# Delivery policy integration tests live in test_notification_policy.py.

if __name__ == "__main__":
    unittest.main()
