#!/usr/bin/env python3
"""Deliver collection health alerts without silently treating failed delivery as success.

Only environment-configured email recipients and this workflow's repository receive
messages. State contains fingerprints and timestamps, never addresses or credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlsplit

import requests


MARKER = "<!-- submarine-cable-monitor-operational-alert:v1 -->"
ISSUE_TITLE = "[monitor] Collection health requires attention"
HEALTHY = "healthy"
REMINDER_SECONDS = 24 * 60 * 60
GOOD = {"success", "ok", "healthy", "available", "ready", "completed", "normal"}


@dataclass(frozen=True)
class Report:
    active: bool
    fingerprint: str
    subject: str
    body: str


def quantity(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, (list, tuple)):
        return len(value)
    if isinstance(value, dict):
        return sum(quantity(item) for item in value.values())
    return 0


def build_report(summary: dict[str, Any], workflow_status: str = "success", *,
                 summary_error: str | None = None, run_url: str | None = None) -> Report:
    """Interpret both existing audit counts and the durable queue health fields."""
    issues: dict[str, tuple[str, int | str]] = {}

    def add(key: str, label: str, value: int | str = "需要处理") -> None:
        issues[key] = (label, value)

    if summary_error:
        add("summary", "本次采集摘要缺失或不可读取")
    if workflow_status not in GOOD:
        add("workflow", "工作流未成功完成", workflow_status)
    status = str(summary.get("status", "unknown")).lower()
    if status not in GOOD and not summary_error:
        add("status", "采集健康状态异常", status if status in {"degraded", "failed", "failure", "unknown"} else "异常")
    for key, label in (("errors", "处理错误"), ("warnings", "采集警告"),
                       ("backlog", "未完成队列"), ("pending", "等待处理新闻"),
                       ("review", "需要复核新闻"), ("retry", "需要重试新闻"), ("pending_count", "等待处理新闻"),
                       ("review_count", "需要复核新闻")):
        value = quantity(summary.get(key))
        if value:
            add(key, label, value)

    queue = summary.get("queue", {})
    if isinstance(queue, dict):
        for key in ("pending", "review", "retry", "failed", "blocked", "backlog"):
            value = quantity(queue.get(key))
            if value:
                add(f"queue.{key}", f"队列 {key}", value)

    health = summary.get("model_health")
    if isinstance(health, str) and health.lower() not in GOOD:
        add("model_health", "模型不可用、余额不足或运行降级")
    elif isinstance(health, dict):
        model_status = str(health.get("status", "healthy")).lower()
        reason = health.get("reason")
        if reason == "balance_api_retired":
            add("model_health", "服务商余额接口已下线，无法提前核查余额")
        elif reason == "balance_exhausted":
            add("model_balance", "模型余额已耗尽，请充值后重试")
        elif reason == "authentication_failed":
            add("model_auth", "模型凭据无效或无权访问，请检查 API 密钥与权限")
        elif reason == "rate_limited":
            add("model_rate_limit", "模型请求受到限流，稍后自动重试")
        elif health.get("available") is False or health.get("ready") is False:
            add("model_health", "模型不可用，请检查凭据、余额或服务状态")
        elif model_status in {"health_unknown", "unknown"}:
            add("model_health", "模型健康状态尚未确认，请查看运行记录")
        elif model_status not in GOOD and model_status != "low_balance":
            add("model_health", "模型运行出现告警")
        if health.get("http_status") == 402 or reason == "balance_exhausted" or health.get("balance_status") == "exhausted":
            add("model_balance", "模型余额已耗尽，请充值后重试")
        elif health.get("balance_status") == "insufficient":
            add("model_balance", "模型余额不足，请充值后重试")
        elif health.get("low_balance") or health.get("balance_status") == "low" or model_status == "low_balance":
            add("model_balance", "模型余额低于提醒阈值，请及时充值")
        if health.get("http_status") == 429:
            add("model_rate_limit", "模型请求受到限流，稍后自动重试")
        if quantity(health.get("failures")):
            add("model_failures", "模型失败次数", quantity(health["failures"]))

    # Silence is not evidence of recovery. A broken/old summary must not close an
    # existing alert while unresolved work or the model's state is unknown.
    model_checked = (isinstance(health, str) and health.lower() in GOOD) or (
        isinstance(health, dict) and str(health.get("status", "")).lower() in GOOD
        and health.get("available") is not False and health.get("ready") is not False
    )
    queue_checked = True
    for key in ("pending", "retry", "review"):
        value = summary.get(key, summary.get(key + "_count", queue.get(key) if isinstance(queue, dict) else None))
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            queue_checked = False
    if not model_checked and not any(key.startswith("model") for key in issues):
        add("model_unchecked", "模型健康状态尚未确认")
    if not queue_checked:
        add("queue_unchecked", "待处理、重试及复核队列状态尚未确认")

    counts = summary.get("counts", {})
    if isinstance(counts, dict):
        # Fixed labels prevent arbitrary scraped/provider text from entering alerts.
        groups = {
            "source_failure": ("发现源或搜索请求失败", lambda key: ("discover" in key or "rss" in key or "source" in key or "search" in key) and "failed" in key),
            "model_failure": ("模型失败或发生规则回退", lambda key: "fallback" in key or ("extract" in key and "failed" in key)),
            "fetch_failure": ("正文获取失败，新闻仍需重试或复核", lambda key: "fetch" in key and "failed" in key),
            "blocked_403": ("站点拒绝访问，需复核", lambda key: "403" in key or "access_denied" in key or "forbidden" in key),
            "budget": ("本轮处理额度已用尽，仍有新闻等待处理", lambda key: "budget" in key),
        }
        for group, (label, matches) in groups.items():
            count = sum(quantity(value) for key, value in counts.items() if matches(str(key).lower()))
            if count:
                add(group, label, count)

    active = bool(issues)
    fingerprint = hashlib.sha256(json.dumps(issues, sort_keys=True, ensure_ascii=False).encode()).hexdigest() if active else HEALTHY
    subject = "[海缆监控] 采集需要处理" if active else "[海缆监控] 采集已恢复"
    lines = [subject, ""]
    if active:
        lines.extend(f"- {label}：{value}" for _, (label, value) in sorted(issues.items()))
        lines.extend(["", "请检查 Actions 运行摘要和 audit.jsonl，按上述原因处理；待处理新闻仍需完成重试。"])
    else:
        lines.append("本次采集已恢复正常，摘要中没有未处理队列、复核项或运行告警。")
    if run_url:
        lines.extend(["", f"运行记录：{run_url}"])
    return Report(active, fingerprint, subject, "\n".join(lines))


def load_summary(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        if path.stat().st_size > 2_000_000:
            return {}, "summary_too_large"
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            return {}, "summary_invalid"
        return result, None
    except (OSError, ValueError):
        return {}, "summary_unreadable"


def load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError("Notification state is unreadable; refusing to silently reset deduplication") from exc
    if not isinstance(value, dict):
        raise ValueError("Invalid notification state")
    result: dict[str, Any] = {}
    for channel in ("email", "github"):
        item = value.get(channel)
        if isinstance(item, dict) and isinstance(item.get("fingerprint"), str) and isinstance(item.get("last_sent"), (float, int)):
            result[channel] = {"fingerprint": item["fingerprint"], "last_sent": item["last_sent"]}
    return result


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def due(report: Report, previous: dict[str, Any] | None, now: float) -> bool:
    if not previous:
        return report.active
    if previous.get("fingerprint") != report.fingerprint:
        return True
    return report.active and now - previous.get("last_sent", 0) >= REMINDER_SECONDS


def github_context(env: dict[str, str]) -> tuple[str, str, str | None]:
    repository = env.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GITHUB_REPOSITORY must be owner/repository")
    server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    parts = urlsplit(server)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.path or parts.query or parts.fragment:
        raise ValueError("GITHUB_SERVER_URL must be an HTTPS GitHub host")
    api = "https://api.github.com" if server == "https://github.com" else server + "/api/v3"
    run_id = env.get("GITHUB_RUN_ID", "")
    run_url = f"{server}/{repository}/actions/runs/{run_id}" if run_id.isdigit() else None
    return api, repository, run_url


class GitHubNotifier:
    def __init__(self, env: dict[str, str], session: requests.Session | None = None):
        api, repository, _ = github_context(env)
        self.url = f"{api}/repos/{repository}/issues"
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {env['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        self.mention = env.get("ALERT_GITHUB_USER", "")
        if self.mention and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", self.mention):
            raise ValueError("ALERT_GITHUB_USER must be an explicit GitHub username")

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        response = self.session.request(method, url, timeout=30, allow_redirects=False, **kwargs)
        if 300 <= response.status_code < 400:
            raise RuntimeError("GitHub API redirected; refusing to notify a different destination")
        response.raise_for_status()
        return response.json() if response.content else None

    def find_issue(self) -> dict[str, Any] | None:
        for page in range(1, 21):
            issues = self.request("GET", self.url, params={"state": "all", "sort": "updated", "direction": "desc", "per_page": 100, "page": page})
            if not isinstance(issues, list):
                raise ValueError("Invalid GitHub issue response")
            for issue in issues:
                if not issue.get("pull_request") and MARKER in (issue.get("body") or ""):
                    return issue
            if len(issues) < 100:
                return None
        raise RuntimeError("Issue lookup exceeded safety limit; refusing to create a duplicate")

    def send(self, report: Report, *, recover_without_state: bool = False, mention: bool = True) -> bool:
        issue = self.find_issue()
        notification = (f"@{self.mention}\n\n" if self.mention and mention else "") + report.body
        body = f"{MARKER}\n\n{notification}"
        if report.active and issue is None:
            self.request("POST", self.url, json={"title": ISSUE_TITLE, "body": body})
            return True
        if issue is None or (recover_without_state and issue.get("state") != "open"):
            return False
        number = issue.get("number")
        if not isinstance(number, int) or number <= 0:
            raise ValueError("Invalid GitHub issue number")
        issue_url = f"{self.url}/{number}"
        self.request("PATCH", issue_url, json={"body": body, "state": "open" if report.active else "closed"})
        # Comments notify issue subscribers; edits alone do not reliably alert them.
        self.request("POST", issue_url + "/comments", json={"body": notification})
        return True


def address(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("Email header contains a newline")
    name, parsed = parseaddr(value)
    if name or parsed != value.strip() or not re.fullmatch(r"[^\s@,<>]+@[^\s@,<>]+\.[^\s@,<>]+", parsed):
        raise ValueError("Email address must be a plain address")
    return parsed


def send_email(report: Report, env: dict[str, str]) -> None:
    host = env.get("SMTP_HOST", "")
    if not host or re.search(r"[\s/:]", host):
        raise ValueError("SMTP_HOST must be a hostname")
    port = int(env.get("SMTP_PORT", "587"))
    if port not in (465, 587):
        raise ValueError("SMTP_PORT must be 465 (TLS) or 587 (STARTTLS)")
    sender = address(env.get("SMTP_FROM", ""))
    recipients = [address(item.strip()) for item in env.get("ALERT_EMAIL_TO", "").split(",") if item.strip()]
    if not recipients:
        raise ValueError("ALERT_EMAIL_TO must contain an email address")
    username, password = env.get("SMTP_USERNAME", ""), env.get("SMTP_PASSWORD", "")
    if bool(username) != bool(password):
        raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be configured together")
    message = EmailMessage()
    message["Subject"] = report.subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(report.body)
    context = ssl.create_default_context()
    if port == 465:
        connection = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        connection = smtplib.SMTP(host, port, timeout=30)
    with connection as client:
        if port == 587:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        if username:
            client.login(username, password)
        refused = client.send_message(message, from_addr=sender, to_addrs=recipients)
        if refused:
            raise RuntimeError("One or more configured recipients were refused")


def error_kind(exc: Exception) -> str:
    """Provider exception bodies can include addresses, passwords, or tokens."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) or getattr(exc, "smtp_code", None)
    return type(exc).__name__ + (f" (HTTP/SMTP {status})" if status else "")


def note(message: str, env: dict[str, str], *, warning: bool = False) -> None:
    print(message, file=sys.stderr if warning else sys.stdout)
    if env.get("GITHUB_STEP_SUMMARY"):
        with Path(env["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as handle:
            handle.write(f"- {message}\n")


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None, now: float | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=Path("data/alert-state.json"))
    parser.add_argument("--workflow-status", default="success", choices=("success", "failure", "cancelled", "skipped", "unknown", "timed_out"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    env = dict(os.environ if env is None else env)
    now = time.time() if now is None else now
    summary, summary_error = load_summary(args.summary)
    run_url = None
    if env.get("GITHUB_REPOSITORY"):
        try:
            _, _, run_url = github_context(env)
        except ValueError:
            pass  # Delivery below explicitly fails if GitHub is configured incorrectly.
    report = build_report(summary, args.workflow_status, summary_error=summary_error, run_url=run_url)
    if args.dry_run:
        print(report.body)
        print("\nDry run: no email or GitHub issue sent; notification state unchanged.")
        return 0
    try:
        state = load_state(args.state)
    except (OSError, RuntimeError, ValueError) as exc:
        note(f"告警去重状态不可读取：{error_kind(exc)}；本次重新检查并发送，不假定已通知。", env, warning=True)
        state = {}
    failures = 0
    configured = 0
    for channel in ("github", "email"):
        if channel == "github":
            enabled = bool(env.get("GITHUB_TOKEN"))
            if not enabled:
                note("GitHub 告警未配置（缺少 GITHUB_TOKEN），未创建或更新告警 issue。", env, warning=True)
                if env.get("GITHUB_ACTIONS") == "true":
                    failures += 1
                continue
        else:
            enabled = bool(env.get("SMTP_HOST") and env.get("SMTP_FROM") and env.get("ALERT_EMAIL_TO"))
            if not enabled:
                note("邮件告警未配置完整（需要 SMTP_HOST、SMTP_FROM、ALERT_EMAIL_TO），未通知邮箱；GitHub issue 渠道独立执行。", env, warning=True)
                if any(env.get(key) for key in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM")):
                    failures += 1
                continue
        configured += 1
        previous = state.get(channel)
        recovery_lookup = channel == "github" and not report.active and not previous
        if not due(report, previous, now) and not recovery_lookup:
            note(f"{channel}：状态未变化，未重复发送。", env)
            continue
        try:
            delivered = True
            if channel == "github":
                delivered = GitHubNotifier(env).send(report, recover_without_state=recovery_lookup,
                                                     mention=not previous or previous.get("fingerprint") != report.fingerprint)
            else:
                send_email(report, env)
            if delivered:
                state[channel] = {"fingerprint": report.fingerprint, "last_sent": now}
                save_state(args.state, state)
                note(f"{channel}：{'告警' if report.active else '恢复通知'}已发送。", env)
            else:
                # No outstanding issue exists. Remember health without claiming delivery.
                state[channel] = {"fingerprint": HEALTHY, "last_sent": now}
                save_state(args.state, state)
                note(f"{channel}：没有待关闭的告警 issue，未发送恢复通知。", env)
        except Exception as exc:
            failures += 1
            note(f"{channel}：通知失败 {error_kind(exc)}，本次不记录发送成功，下次重试。", env, warning=True)
    if report.active and not configured:
        failures += 1
        note("存在采集告警，但没有可用通知渠道。", env, warning=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
