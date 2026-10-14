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
    def test_failures_and_pending_work_are_actionable(self):
        summary = healthy(status="degraded", errors=1, warnings=2,
                          queue={"pending": 3, "retry": 4, "review": 1},
                          model_health={"status": "unavailable", "http_status": 402, "balance": "private-amount"},
                          counts={"discovery.failed": 1, "fetch.failed.http_403": 1, "extraction.fallback": 2, "selection.skipped.budget": 3})
        report = notify.build_report(summary)
        self.assertTrue(report.active)
        for text in ("模型余额已耗尽", "发现源", "站点拒绝", "额度已用尽", "需要处理"):
            self.assertIn(text, report.body)
        self.assertNotIn("private-amount", report.body)

    def test_fingerprint_ignores_run_ids_processed_counts_and_private_fields(self):
        first = notify.build_report(healthy(warnings=1, articles=1, run_id="first", smtp_password="secret"), run_url="https://github.com/owner/repo/actions/runs/1")
        second = notify.build_report(healthy(warnings=1, articles=99, run_id="second", smtp_password="different"), run_url="https://github.com/owner/repo/actions/runs/2")
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertNotIn("secret", first.body)

    def test_no_recovery_from_success_status_alone(self):
        incomplete = notify.build_report({"status": "success"})
        self.assertTrue(incomplete.active)
        self.assertIn("尚未确认", incomplete.body)
        self.assertFalse(notify.build_report(healthy()).active)
        for field in ("pending", "retry", "review"):
            queue = {"pending": 0, "retry": 0, "review": 0, field: 1}
            self.assertTrue(notify.build_report(healthy(queue=queue)).active)
        self.assertTrue(notify.build_report(healthy(model_health={"status": "unknown"})).active)

    def test_workflow_failure_and_missing_summary_cannot_recover(self):
        self.assertTrue(notify.build_report(healthy(), "failure").active)
        self.assertTrue(notify.build_report({}, summary_error="summary_unreadable").active)

    def test_retired_balance_api_is_unknown_not_a_claim_of_model_outage(self):
        report = notify.build_report(healthy(model_health={"status": "health_unknown", "available": None,
                                                           "low_balance": False, "reason": "balance_api_retired"}))
        self.assertTrue(report.active)
        self.assertIn("服务商余额接口已下线，无法提前核查余额", report.body)
        self.assertNotIn("模型不可用", report.body)
        self.assertNotIn("模型余额不足", report.body)
        self.assertNotIn("充值", report.body)

    def test_low_positive_balance_does_not_claim_model_is_unavailable(self):
        report = notify.build_report(healthy(model_health={"status": "low_balance", "available": True,
                                                           "low_balance": True, "reason": "balance_below_threshold"}))
        self.assertTrue(report.active)
        self.assertIn("余额低于提醒阈值", report.body)
        self.assertNotIn("模型不可用", report.body)

    def test_model_failure_reason_identifies_balance_credentials_or_rate_limit(self):
        for reason, status, expected in (("balance_exhausted", 402, "模型余额已耗尽"),
                                         ("authentication_failed", 401, "模型凭据无效或无权访问"),
                                         ("rate_limited", 429, "模型请求受到限流")):
            with self.subTest(reason=reason):
                report = notify.build_report(healthy(model_health={"status": "unavailable", "available": False,
                                                                   "reason": reason, "http_status": status}))
                self.assertTrue(report.active)
                self.assertIn(expected, report.body)
                self.assertEqual(report.body.count(expected), 1)
                self.assertNotIn("模型不可用", report.body)

    def test_repeat_change_and_recovery_delivery_schedule(self):
        warning = notify.build_report(healthy(warnings=1))
        changed = notify.build_report(healthy(warnings=2))
        previous = {"fingerprint": warning.fingerprint, "last_sent": 100}
        self.assertFalse(notify.due(warning, previous, 200))
        self.assertTrue(notify.due(warning, previous, 100 + notify.REMINDER_SECONDS))
        self.assertTrue(notify.due(changed, previous, 200))
        good = notify.build_report(healthy())
        self.assertTrue(notify.due(good, previous, 200))
        self.assertFalse(notify.due(good, None, 200))
        self.assertFalse(notify.due(good, {"fingerprint": "healthy", "last_sent": 1}, 999999))


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.report = notify.build_report(healthy(warnings=1))

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
        self.assertTrue(client.send(notify.build_report(healthy(warnings=1))))
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
        client.send(notify.build_report(healthy(warnings=2)))
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
        notify.GitHubNotifier(env, session).send(notify.build_report(healthy(warnings=1)))
        self.assertNotIn("@owner", session.request.call_args.kwargs["json"]["body"])

    def test_api_redirect_is_not_followed(self):
        session = MagicMock()
        session.request.return_value = response({}, status=302)
        with self.assertRaises(RuntimeError):
            notify.GitHubNotifier(github_env(), session).send(notify.build_report(healthy(warnings=1)))
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


class CLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.summary = self.root / "summary.json"
        self.state = self.root / "state.json"
        self.summary.write_text(json.dumps(healthy(warnings=1)))
        self.args = ["--summary", str(self.summary), "--state", str(self.state)]

    def call(self, *, env=None, now=100, args=None):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = notify.main(self.args if args is None else args, env={} if env is None else env, now=now)
        return result, output.getvalue(), errors.getvalue()

    @patch.object(notify, "GitHubNotifier")
    @patch.object(notify, "send_email")
    def test_dry_run_never_sends_or_writes_state(self, email, github):
        result, output, _ = self.call(args=self.args + ["--dry-run"], env={**github_env(), **email_env()})
        self.assertEqual(result, 0)
        self.assertIn("Dry run", output)
        self.assertFalse(self.state.exists())
        email.assert_not_called()
        github.assert_not_called()

    @patch.object(notify, "GitHubNotifier")
    def test_missing_email_is_explicit_but_github_still_delivers(self, github):
        github.return_value.send.return_value = True
        summary_path = self.root / "actions-summary.md"
        result, output, errors = self.call(env={**github_env(), "GITHUB_STEP_SUMMARY": str(summary_path)})
        self.assertEqual(result, 0)
        self.assertIn("未通知邮箱", errors)
        self.assertIn("未通知邮箱", summary_path.read_text())
        self.assertIn("github：告警已发送", output)
        self.assertEqual(set(json.loads(self.state.read_text())), {"github"})

    @patch.object(notify, "GitHubNotifier")
    @patch.object(notify, "send_email")
    def test_saved_recipient_without_smtp_is_not_an_attempted_email_channel(self, email, github):
        github.return_value.send.return_value = True
        result, output, errors = self.call(env={**github_env(), "ALERT_EMAIL_TO": "recipient@example.com"})
        self.assertEqual(result, 0)
        self.assertIn("未通知邮箱", errors)
        self.assertNotIn("recipient@example.com", errors)
        self.assertIn("github：告警已发送", output)
        email.assert_not_called()
        self.assertNotIn("email", json.loads(self.state.read_text()))

    @patch.object(notify, "GitHubNotifier")
    @patch.object(notify, "send_email")
    def test_delivery_failure_does_not_suppress_retry_or_leak_details(self, email, github):
        github.return_value.send.return_value = True
        email.side_effect = smtplib.SMTPAuthenticationError(535, b"private-password recipient@example.com")
        env = {**github_env(), **email_env()}
        result, _, errors = self.call(env=env)
        self.assertEqual(result, 1)
        state = json.loads(self.state.read_text())
        self.assertNotIn("email", state)
        self.assertIn("github", state)
        self.assertNotIn("private-password", errors)
        self.assertNotIn("recipient@example.com", errors)
        email.side_effect = None
        self.assertEqual(self.call(env=env, now=101)[0], 0)
        self.assertEqual(email.call_count, 2)
        self.assertEqual(github.return_value.send.call_count, 1)
        stored = self.state.read_text()
        for secret in ("private-password", "private-token", "recipient@example.com"):
            self.assertNotIn(secret, stored)
        self.assertEqual(set(json.loads(stored)["email"]), {"fingerprint", "last_sent"})

    @patch.object(notify, "GitHubNotifier")
    def test_recovery_delivered_once_and_reminders_wait_24_hours(self, github):
        github.return_value.send.return_value = True
        self.call(env=github_env(), now=100)
        self.call(env=github_env(), now=101)
        self.assertEqual(github.return_value.send.call_count, 1)
        self.call(env=github_env(), now=100 + notify.REMINDER_SECONDS)
        self.assertEqual(github.return_value.send.call_count, 2)
        self.summary.write_text(json.dumps(healthy()))
        self.call(env=github_env(), now=102 + notify.REMINDER_SECONDS)
        self.call(env=github_env(), now=103 + notify.REMINDER_SECONDS)
        self.assertEqual(github.return_value.send.call_count, 3)
        self.assertFalse(github.return_value.send.call_args.args[0].active)

    @patch.object(notify, "GitHubNotifier")
    def test_missing_summary_and_failed_workflow_raise_alert(self, github):
        github.return_value.send.return_value = True
        self.summary.unlink()
        result, _, _ = self.call(env=github_env(), args=self.args + ["--workflow-status", "failure"])
        self.assertEqual(result, 0)
        report = github.return_value.send.call_args.args[0]
        self.assertTrue(report.active)
        self.assertIn("摘要缺失", report.body)
        self.assertIn("工作流未成功", report.body)

    def test_active_alert_without_channels_fails(self):
        result, _, errors = self.call()
        self.assertEqual(result, 1)
        self.assertIn("没有可用通知渠道", errors)

    @patch.object(notify, "GitHubNotifier")
    def test_corrupt_dedup_state_does_not_silence_current_alert(self, github):
        github.return_value.send.return_value = True
        self.state.write_text("interrupted write")
        result, _, errors = self.call(env=github_env())
        self.assertEqual(result, 0)
        self.assertIn("去重状态不可读取", errors)
        github.return_value.send.assert_called_once()
        self.assertIn("github", json.loads(self.state.read_text()))


if __name__ == "__main__":
    unittest.main()
