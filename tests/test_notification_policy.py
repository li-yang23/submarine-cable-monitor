import contextlib
import io
import json
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import patch

from scripts import notify_monitor as notify
from src import notification_policy as policy
from src.utils.audit import AuditRun


def summary(**changes):
    return {"run_id": "run-1", "runtime_status": "success", "status": "success", "errors": 0,
            "model_health": {"reason": "inference_succeeded", "status": "available", "available": True}, **changes}


def failure(**changes):
    return {"stage": "fetch", "reason": "article_fetch_error", "url": "https://publisher.test/news",
            "http_status": 502, **changes}


def balance(**changes):
    return summary(model_health={"provider": "siliconflow", "available": False, "reason": "balance_exhausted",
                                 "attempt_id": "one", **changes})


class PolicyTests(unittest.TestCase):
    def test_external_failure_waits_a_week_and_absence_does_not_claim_recovery(self):
        state = {}
        p = policy.reconcile(state, summary(failure_details=[failure()]), [], [], 100)
        self.assertEqual(policy.plan(p, "email", 100), [])
        p = policy.reconcile(state, summary(run_id="run-2"), [], [], 200)
        self.assertTrue(all(i["status"] == "open" for i in p["issues"].values()))
        message, = policy.plan(p, "email", 100 + policy.WEEK)
        self.assertEqual(message["type"], "digest")
        self.assertIn("无需操作", message["body"])
        policy.acknowledge(p, "email", message, 100 + policy.WEEK)
        self.assertEqual(policy.plan(p, "email", 101 + policy.WEEK), [])

    def test_explicit_fetch_success_removes_issue_from_weekly_summary(self):
        state = {}
        policy.reconcile(state, summary(failure_details=[failure()]), [], [], 100)
        p = policy.reconcile(state, summary(run_id="run-2"), [], [failure(outcome="completed")], 200)
        self.assertEqual(policy.plan(p, "email", 100 + policy.WEEK), [])

    def test_balance_action_once_until_real_inference_recovery(self):
        state = {}
        p = policy.reconcile(state, balance(), [], [], 100)
        message, = policy.plan(p, "email", 100)
        self.assertIn("充值", message["body"])
        self.assertIn("自动重试", message["body"])
        policy.acknowledge(p, "email", message, 100)
        for health in (balance(attempt_id="tomorrow"), summary(model_health={"reason": "balance_check_disabled"}), balance()):
            p = policy.reconcile(state, health, [], [], 200)
            self.assertEqual(policy.plan(p, "email", 200), [])
        policy.reconcile(state, summary(), [], [], 300)
        p = policy.reconcile(state, balance(), [], [], 400)
        self.assertEqual(len(policy.plan(p, "email", 400)), 1)

    def test_auth_action_identifies_provider_secret_and_settings(self):
        p = policy.reconcile({}, summary(model_health={"provider": "openai", "http_status": 401}), [], [], 100,
                             run_url="https://github.com/owner/repo/actions/runs/123")
        message, = policy.plan(p, "email", 100)
        self.assertIn("OPENAI_API_KEY", message["body"])
        self.assertIn("https://github.com/owner/repo/settings/secrets/actions", message["body"])

    def test_timeout_rate_limit_and_no_user_action_do_not_send_immediately(self):
        for reason in ("request_timeout", "rate_limited", "provider_unavailable"):
            p = policy.reconcile({}, summary(model_health={"available": False, "reason": reason}), [], [], 100)
            self.assertEqual(policy.plan(p, "email", 100), [])

    def test_queue_retains_unresolved_and_does_not_treat_done_as_missing(self):
        row = {"queue_id": "q1", "url": "https://publisher.test/news", "status": "unresolved", "reason": "subscription_required"}
        state = {}
        p = policy.reconcile(state, summary(), [row], [], 100)
        self.assertIn("订阅", policy.plan(p, "email", 100 + policy.WEEK)[0]["body"])
        p = policy.reconcile(state, summary(run_id="run-2"), [{**row, "status": "done"}], [], 200)
        self.assertEqual(policy.plan(p, "email", 100 + policy.WEEK), [])

    def test_gateway_failure_keeps_audit_but_does_not_fail_program_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditRun(tmp)
            audit.emit("fetch", "failed", url="https://publisher.test/news", http_status=502, reason="article_fetch_error")
            self.assertEqual(audit.failures, 1)
            self.assertEqual(audit.runtime_failures, 0)
            audit.emit("process", "failed", reason="processing_error", error_type="ValueError")
            self.assertEqual(audit.runtime_failures, 1)
            result = audit.finish({})
            self.assertEqual(len(result["failure_details"]), 2)

    def test_unknown_fetch_programming_exception_is_not_hidden(self):
        self.assertFalse(policy.external_failure(failure(http_status=None, error_type="ValueError")))
        self.assertTrue(policy.external_failure(failure(http_status=None, error_type="SubscriptionRequiredError")))

    def test_repeated_rss_date_changes_share_an_incident(self):
        first = failure(url="https://news.google.com/rss/search?q=cable+after%3A2026-10-01")
        second = failure(url="https://news.google.com/rss/search?q=cable+after%3A2026-10-02")
        self.assertEqual(policy.identity(first), policy.identity(second))

    def test_infrastructure_step_failure_is_recorded_even_with_degraded_collection(self):
        state = {}
        p = policy.reconcile(state, summary(runtime_status="degraded", failure_details=[failure()]), [], [], 100,
                             steps={"DEPLOY_OUTCOME": "failure"})
        self.assertTrue(any(i.get("evidence", {}).get("step") == "DEPLOY_OUTCOME" for i in p["issues"].values()))
        p = policy.reconcile(state, summary(), [], [], 200, steps={"DEPLOY_OUTCOME": "success"})
        self.assertTrue(all(i["status"] == "resolved" for i in p["issues"].values() if i.get("evidence", {}).get("step")))

    def test_directory_recovery_requires_the_same_url(self):
        state = {}
        item = failure(stage="discover", url="https://publisher.test/category/5")
        policy.reconcile(state, summary(failure_details=[item]), [], [], 100)
        p = policy.reconcile(state, summary(), [], [{"stage": "discover", "outcome": "completed", "url": "https://publisher.test/category/6"}], 200)
        self.assertTrue(policy.plan(p, "email", 100 + policy.WEEK))
        p = policy.reconcile(state, summary(), [], [{**item, "outcome": "completed"}], 300)
        self.assertEqual(policy.plan(p, "email", 100 + policy.WEEK), [])


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.summary = root / "summary.json"
        self.state = root / "state.json"
        self.queue = root / "queue.json"
        self.summary.write_text(json.dumps(summary(failure_details=[failure()])))
        self.args = ["--summary", str(self.summary), "--state", str(self.state), "--queue", str(self.queue)]
        self.env = {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "sender@example.com", "ALERT_EMAIL_TO": "recipient@example.com",
                    "GITHUB_TOKEN": "private-token", "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123"}

    def call(self, now=100, extra=()):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return notify.main(self.args + list(extra), env=self.env, now=now)

    @patch.object(notify, "GitHubNotifier")
    @patch.object(notify, "send_email")
    def test_old_state_migrates_without_repeat_and_weekly_sends_once(self, mail, github):
        self.state.write_text(json.dumps({"email": {"fingerprint": "old", "conditions": ["runtime:old"], "last_sent": 1}}))
        self.assertEqual(self.call(), 0)
        mail.assert_not_called(); github.assert_not_called()
        self.assertEqual(self.call(now=100 + policy.WEEK), 0)
        mail.assert_called_once(); github.assert_not_called()
        self.assertIn("每周", mail.call_args.args[0].subject)
        self.call(now=101 + policy.WEEK)
        self.assertEqual(mail.call_count, 1)

    @patch.object(notify, "GitHubNotifier")
    @patch.object(notify, "send_email")
    def test_failed_delivery_remains_due_and_is_not_duplicated_in_github(self, mail, github):
        self.summary.write_text(json.dumps(balance()))
        mail.side_effect = smtplib.SMTPAuthenticationError(535, b"private-password")
        self.assertEqual(self.call(), 1)
        self.assertEqual(json.loads(self.state.read_text())["policy"]["deliveries"], {})
        mail.side_effect = None
        self.assertEqual(self.call(now=101), 0)
        self.call(now=102)
        self.assertEqual(mail.call_count, 2)
        github.assert_not_called()
        self.assertNotIn("private-password", self.state.read_text())

    @patch.object(notify, "send_email")
    def test_dry_run_never_sends_or_writes(self, mail):
        self.summary.write_text(json.dumps(balance()))
        self.assertEqual(self.call(extra=["--dry-run"]), 0)
        self.assertFalse(self.state.exists()); mail.assert_not_called()

    @patch.object(notify, "send_email")
    def test_corrupt_state_is_preserved_without_duplicate_delivery(self, mail):
        self.state.write_text("interrupted write")
        self.assertEqual(self.call(), 1)
        self.assertEqual(self.state.read_text(), "interrupted write")
        mail.assert_not_called()

    @patch.object(notify, "send_email")
    def test_digest_failure_does_not_advance_timestamp(self, mail):
        self.call()
        mail.side_effect = RuntimeError("smtp offline")
        self.assertEqual(self.call(now=100 + policy.WEEK), 1)
        mail.side_effect = None
        self.assertEqual(self.call(now=101 + policy.WEEK), 0)
        self.call(now=102 + policy.WEEK)
        self.assertEqual(mail.call_count, 2)


if __name__ == "__main__":
    unittest.main()
