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
from dataclasses import dataclass, replace
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlsplit

import requests


MARKER = "<!-- submarine-cable-monitor-operational-alert:v1 -->"
ISSUE_TITLE = "[monitor] Collection health requires attention"
HEALTHY = "healthy"
GOOD = {"success", "ok", "healthy", "available", "ready", "completed", "normal"}


@dataclass(frozen=True)
class Report:
    active: bool
    fingerprint: str
    subject: str
    body: str
    conditions: tuple[str, ...] = ()
    balance_attempt: str = ""
    details: tuple = ()
    detailed: bool = False


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


REASON_LABELS = {
    "article_fetch_error": "原文抓取失败，保留重试及替代来源补证",
    "alternative_fetch_failed": "补证来源请求异常",
    "cross_source_cable_identity_not_supported": "来源不能证实同一海缆，不能合并为同一事件",
    "candidates_rejected": "候选证据未通过，等待逐项补证", "short_content": "未取得完整正文",
    "cross_source_insufficient_sources": "缺少足够的独立可靠来源",
    "cross_source_quote_not_in_body": "模型引用不在来源正文中",
    "cross_source_primary_correction_required": "日期冲突，缺少原始公告或更正",
    "candidate_evidence_incomplete": "部分候选已补证，其他候选仍缺证据",
    "cross_source_manual_scope_remaining": "已有部分有效补证，但原文覆盖范围仍未确认",
    "cross_source_original_scope_unknown": "原文范围无法确认，需要完整正文或授权转载",
    "historical_candidate_evidence_missing": "旧日志缺少候选片段，需要重新提取原文",
}


def detail_report(summary, workflow_status, summary_error, run_url):
    # Keep actual model failures/recharge instructions identical to legacy reports.
    base = build_report({k: v for k, v in summary.items() if k != "runtime_status"},
                        workflow_status, summary_error=summary_error, run_url=run_url)
    details = {}
    def add(kind, identity, text):
        digest = hashlib.sha256(str(identity).encode()).hexdigest()[:20]
        details[kind + ":" + digest] = text
    runtime = summary.get("runtime_status")
    if summary_error:
        add("runtime", "summary", "运行故障：本轮摘要无法读取")
    for item in summary.get("failure_details", []):
        url = item.get("url") or item.get("discovered_url") or item.get("source") or item.get("stage")
        reason = item.get("reason", "unknown")
        code = item.get("http_status")
        add("runtime", (item.get("stage"), url, reason, code),
            f"运行故障：{item.get('source') or item.get('stage')}；{reason}" + (f"；HTTP {code}" if code else "") + f"；{url}")
    model_keys = [key for key in base.conditions if key.startswith("model_")]
    for key in model_keys:
        label = next((line for line in base.body.splitlines() if line.startswith("- ") and ("模型" in line or "SiliconFlow" in line)), key)
        add("model", key, label.removeprefix("- "))
    if runtime != "success" and not details:
        add("runtime", runtime, "运行故障：" + str(runtime) + "；检查本轮日志")
    if workflow_status not in GOOD and runtime == "success":
        add("runtime", "workflow", "运行故障：采集之外的导出、保存或工作流步骤失败")
    for item in summary.get("queue_items", []):
        if item.get("status") not in {"retry", "review"}:
            continue
        kind = "review" if item["status"] == "review" else "retry"
        reason = item.get("reason", "unknown")
        title = " ".join(str(item.get("title") or item.get("queue_id")).split())[:240]
        add(kind, (item.get("queue_id"), reason),
            f"{'待复核' if kind == 'review' else '待重试'}：{title}；{REASON_LABELS.get(reason, reason)}；{item.get('url') or item.get('queue_id')}")
    conditions = tuple(sorted(details))
    active = bool(conditions)
    subject = (base.subject if base.balance_attempt or "model_balance" in base.conditions else
               "[海缆监控] 运行故障" if runtime != "success" or any(k.startswith("runtime:") for k in conditions) else
               "[海缆监控] 新增待复核或重试事项" if active else "[海缆监控] 运行正常")
    queue = summary.get("queue", {})
    lines = [subject, "", f"运行状态：{runtime}；证据状态：{summary.get('review_status', 'unknown')}。",
             f"本轮处理 {quantity(summary.get('articles'))} 篇，新增 {quantity(summary.get('inserted'))} 条事件；累计待重试 {quantity(queue.get('retry'))} 条、待复核 {quantity(queue.get('review'))} 条、待处理 {quantity(queue.get('pending'))} 条。"]
    if "model_balance" in base.conditions:
        lines.extend(line for line in base.body.splitlines() if "充值" in line or "暂停" in line or "次日" in line)
    changes = summary.get("queue_changes", {})
    lines.append(f"本轮队列变化：新增 {len(changes.get('new', []))} 项，原因或状态变化 {len(changes.get('changed', []))} 项，完成 {len(changes.get('resolved', []))} 项。")
    if summary.get("counts", {}).get("discover.deferred.discovery_budget"):
        lines.append("目录扫描已保存续扫位置，下轮继续；这是进度信息，不计作运行故障。")
    lines.append("待复核不表示事件已确认，也不计作程序故障；未确认记录继续保留证据缺口。")
    unavailable = summary.get("unavailable_evidence", [])
    if unavailable:
        lines.append(f"本轮有 {len(unavailable)} 个补证参考页面无法公开读取；已记录证据缺口，不计作程序故障：")
        lines.extend(f"- {item.get('title') or item.get('parent_queue_id')}；HTTP {item.get('http_status')}；{item.get('url')}；关联记录 {item.get('parent_queue_id')}" for item in unavailable)
    if run_url:
        lines.append("运行记录：" + run_url)
    return Report(active, hashlib.sha256(json.dumps(conditions).encode()).hexdigest() if active else HEALTHY,
                  subject, "\n".join(lines), conditions, base.balance_attempt, tuple(details.items()), True)


def with_change_details(report, previous):
    if not report.detailed:
        return report
    seen = set((previous or {}).get("conditions", []))
    new = [(key, label) for key, label in report.details if key not in seen]
    old = [(key, label) for key, label in report.details if key in seen]
    resolved = seen - set(report.conditions)
    legacy = bool(seen) and not any(":" in key for key in seen)
    if legacy:
        # Old state tracked categories, not article identities. It cannot prove
        # which of today's individual records were newly observed.
        new, old, resolved = [], list(report.details), set()
        subject = report.subject.replace("新增待复核或重试事项", "待复核与重试记录基线")
        report = replace(report, subject=subject, body=report.body.replace(report.subject, subject, 1))
        lines = [report.body, "", "首次启用逐项告警：以下记录用于建立当前问题基线，不计作本轮新增；本轮实际队列变化见上方统计。"]
    else:
        lines = [report.body, "", f"相对上次检查，新增或变化 {len(new)} 项："]
    lines.extend("- " + label for _, label in new)
    if not new and not legacy:
        lines.append("- 无新增问题")
    lines.append(f"仍未解决的既有事项：{len(old)} 项。")
    lines.extend("- " + label for _, label in old)
    if resolved:
        lines.append(f"上次问题本轮未再出现或已解决：{len(resolved)} 项；历史证据仍保存在队列和审计记录中。")
    return replace(report, body="\n".join(lines))


def build_report(summary: dict[str, Any], workflow_status: str = "success", *,
                 summary_error: str | None = None, run_url: str | None = None) -> Report:
    """Interpret both existing audit counts and the durable queue health fields."""
    if "runtime_status" in summary:
        return detail_report(summary, workflow_status, summary_error, run_url)
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
    for key, label in (("errors", "处理错误"), ("warnings", "采集异常计数（含未完成队列提示）"),
                       ("backlog", "未完成队列"), ("pending", "等待处理新闻"),
                       ("review", "需要复核新闻"), ("retry", "需要重试新闻"), ("pending_count", "等待处理新闻"),
                       ("review_count", "需要复核新闻")):
        value = quantity(summary.get(key))
        if value and not (key == "warnings" and "failure_summary" in summary):
            add(key, label, value)

    queue = summary.get("queue", {})
    if isinstance(queue, dict):
        for key in ("pending", "review", "retry", "failed", "blocked", "backlog"):
            value = quantity(queue.get(key))
            if value:
                add(f"queue.{key}", f"队列 {key}", value)

    health = summary.get("model_health")
    # Balance forecasts and unavailable balance APIs are intentionally silent.
    # Only missing credentials or observed inference failures are actionable.
    quiet_statuses = {"low_balance", "health_unknown", "unknown", "not_checked"}
    quiet_reasons = {"balance_api_retired", "balance_check_unsupported", "balance_check_disabled", "balance_below_threshold"}
    if isinstance(health, str) and health.lower() not in GOOD | quiet_statuses:
        add("model_health", "模型调用不可用")
    elif isinstance(health, dict):
        provider = {"siliconflow": "SiliconFlow", "openai": "OpenAI"}.get(health.get("provider"), "当前模型服务")
        model_status = str(health.get("status", "unknown")).lower()
        reason = health.get("reason")
        http_status = health.get("http_status")
        if http_status == 402 or reason == "balance_exhausted":
            add("model_balance", f"{provider} 模型调用因余额耗尽失败，请充值后重试")
        elif http_status in {401, 403} or reason in {"authentication_failed", "missing_api_key"}:
            add("model_auth", f"{provider} 模型凭据无效或无权访问，请检查 API 密钥与权限")
        elif http_status == 429 or reason == "rate_limited":
            add("model_rate_limit", f"{provider} 模型请求受到限流，稍后自动重试")
        elif reason in {"request_timeout", "connection_failed", "provider_unavailable", "invalid_response", "request_rejected", "extraction_failed"}:
            descriptions = {"request_timeout": "请求超时", "connection_failed": "连接失败",
                            "provider_unavailable": "服务暂时不可用", "invalid_response": "返回内容不完整或格式无效",
                            "request_rejected": "请求被拒绝", "extraction_failed": "调用失败"}
            add("model_" + reason, f"{provider} {descriptions[reason]}；未处理新闻已保留供后续重试")
        elif reason not in quiet_reasons and model_status not in quiet_statuses:
            if health.get("available") is False or health.get("ready") is False or model_status not in GOOD:
                add("model_health", "模型调用不可用，请检查凭据或服务状态")
        if quantity(health.get("failures")):
            add("model_failures", "模型失败次数", quantity(health["failures"]))

    # Balance checks are disabled, so an untested model is not itself an alert.
    # A missing queue summary still must not claim that unfinished work cleared.
    queue_checked = True
    for key in ("pending", "retry", "review"):
        value = summary.get(key, summary.get(key + "_count", queue.get(key) if isinstance(queue, dict) else None))
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            queue_checked = False
    if not queue_checked:
        add("queue_unchecked", "待处理、重试及复核队列状态尚未确认")

    counts = summary.get("counts", {})
    if isinstance(counts, dict):
        # Fixed labels prevent arbitrary scraped/provider text from entering alerts.
        groups = {
            "source_failure": ("发现源或搜索请求失败", lambda key: key.split(".")[:2] in (["discover", "failed"], ["discovery", "failed"], ["rss", "failed"], ["source", "failed"], ["search", "failed"])) ,
            "model_failure": ("模型调用失败", lambda key: key.split(".")[:2] in (["extract", "failed"], ["extraction", "failed"])),
            "model_fallback": ("检测到规则回退", lambda key: key.split(".")[:2] in (["extract", "fallback"], ["extraction", "fallback"])),
            "fetch_failure": ("正文获取失败，新闻仍需重试或复核", lambda key: key.split(".")[:2] == ["fetch", "failed"]),
            "blocked_403": ("站点拒绝访问，需复核", lambda key: "403" in key or "access_denied" in key or "forbidden" in key),
            "discovery_limit": ("目录发现达到本轮时长或页数上限，已保存续扫位置（非账户余额）", lambda key: key == "discover.deferred.discovery_budget"),
            "processing_limit": ("本轮处理达到时长或篇数上限，剩余新闻留待后续处理（非账户余额）", lambda key: key in {"collection.deferred.time_budget", "process.deferred.time_budget", "select.deferred.time_budget", "select.deferred.article_budget", "select.deferred.attempt_budget", "selection.skipped.budget"}),
            "cross_source_review": ("替代来源证据不足或冲突，保留复核记录", lambda key: key.startswith("cross_source.review.")),
        }
        for group, (label, matches) in groups.items():
            if "failure_summary" in summary and group in {"source_failure", "fetch_failure"}:
                continue
            count = sum(quantity(value) for key, value in counts.items() if matches(str(key).lower()))
            if count:
                add(group, label, count)

    for group, key, label in (("article_fetch", "fetch_failure", "新闻正文获取失败（按 URL 去重）"),
                              ("source_discovery", "source_failure", "目录或搜索请求失败（按 URL 去重，非新闻篇数）")):
        details = summary.get("failure_summary", {}).get(group, {})
        if quantity(details.get("count")):
            sources = details.get("sources", {})
            add(key, label, quantity(details["count"]))
            # Known source labels only; no publisher-controlled strings in email.
            names = [name for name in ("Google News", "SubTel Forum", "Submarine Networks") if sources.get(name)]
            if names:
                issues[key] = (label + "；来源：" + "、".join(names), quantity(details["count"]))
    active = bool(issues)
    conditions = tuple(sorted(issues))
    fingerprint = hashlib.sha256(json.dumps(conditions).encode()).hexdigest() if active else HEALTHY
    subject = "[海缆监控] 采集需要处理" if active else "[海缆监控] 采集已恢复"
    balance_attempt = ""
    if "model_balance" in issues:
        subject = "[海缆监控] 模型余额不足，请充值；处理已暂停"
        if isinstance(health, dict):
            balance_attempt = str(health.get("attempt_id", ""))
    lines = [subject, ""]
    if "model_balance" in issues:
        lines.append("模型服务已返回余额不足，请为当前模型 API 账户充值。本轮模型处理已停止，未处理新闻已保留，不会改用规则或其他模型继续处理。")
        lines.append("北京时间次日的定时任务将重试积压新闻和新发现新闻；如果余额仍不足，将再次暂停并提醒充值。")
        if isinstance(health, dict) and health.get("retry_at"):
            lines.append(f"暂停截止时间（UTC）：{health['retry_at']}，到期后的首次定时任务重试。")
    if active:
        if isinstance(health, dict) and health.get("reason") in {"request_timeout", "connection_failed", "provider_unavailable"}:
            lines.append("本次没有收到模型账户余额不足的返回；无需根据这封邮件判断是否充值。")
        if isinstance(health, dict) and health.get("reason") == "inference_succeeded":
            lines.append(f"模型调用正常；本轮处理 {quantity(summary.get('articles'))} 篇，新增 {quantity(summary.get('inserted'))} 条事件。")
        lines.extend(f"- {label}：{value}" for _, (label, value) in sorted(issues.items()))
        lines.extend(["", "请检查 Actions 运行摘要和 audit.jsonl，按上述原因处理；待处理新闻仍需完成重试。"])
    else:
        lines.append("本次采集已恢复正常，摘要中没有未处理队列、复核项或运行告警。")
    if run_url:
        lines.extend(["", f"运行记录：{run_url}"])
    return Report(active, fingerprint, subject, "\n".join(lines), conditions, balance_attempt)


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
            if isinstance(item.get("balance_attempt"), str):
                result[channel]["balance_attempt"] = item["balance_attempt"]
            conditions = item.get("conditions")
            if isinstance(conditions, list) and all(isinstance(key, str) for key in conditions):
                result[channel]["conditions"] = sorted(set(conditions))
    return result


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def due(report: Report, previous: dict[str, Any] | None, now: float) -> bool:
    if not previous:
        return report.active
    if not report.active:
        return previous.get("fingerprint") != HEALTHY
    if previous.get("fingerprint") == HEALTHY:
        return True
    if report.balance_attempt and report.balance_attempt != previous.get("balance_attempt"):
        # Each actual next-day 402 sends once; skipped runs reuse the same attempt.
        return True
    conditions = previous.get("conditions")
    if isinstance(conditions, list):
        # Counts, disappearing categories and time passing do not send reminders.
        # A newly observed failure category (e.g. actual HTTP 402) still alerts.
        return bool(set(report.conditions) - set(conditions))
    # Backward compatibility for state written before condition-level tracking.
    return previous.get("fingerprint") != report.fingerprint


def after_model_recovery(previous: dict[str, Any] | None, report: Report,
                         summary: dict[str, Any]) -> dict[str, Any] | None:
    """A successful inference re-arms model alerts without another notification.

    Outstanding review work must not suppress a later, genuinely new model outage.
    An untested model cannot establish recovery, and unrelated conditions stay seen.
    """
    if not previous or not report.active or not isinstance(previous.get("conditions"), list):
        return previous
    health = summary.get("model_health")
    if not isinstance(health, dict) or health.get("status") != "available" or health.get("reason") != "inference_succeeded":
        return previous
    conditions = [key for key in previous["conditions"]
                  if not key.startswith("model_") or key in report.conditions]
    if conditions == previous["conditions"]:
        return previous
    return {**previous, "conditions": conditions,
            "fingerprint": hashlib.sha256(json.dumps(tuple(sorted(conditions))).encode()).hexdigest()}


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
                if report.balance_attempt or any(env.get(key) for key in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM")):
                    failures += 1
                continue
        configured += 1
        previous = after_model_recovery(state.get(channel), report, summary)
        recovery_lookup = channel == "github" and not report.active and not previous
        delivery_report = with_change_details(report, previous)
        if not due(report, previous, now) and not recovery_lookup:
            if report.detailed and previous:
                previous = {**previous, "conditions": list(report.conditions), "fingerprint": report.fingerprint}
            if previous != state.get(channel):
                try:
                    state[channel] = previous
                    save_state(args.state, state)
                except OSError as exc:
                    failures += 1
                    note(f"{channel}：无法保存模型恢复状态 {error_kind(exc)}。", env, warning=True)
            note(f"{channel}：没有新增故障类型，未重复发送。", env)
            continue
        try:
            delivered = True
            if channel == "github":
                delivered = GitHubNotifier(env).send(delivery_report, recover_without_state=recovery_lookup,
                                                     mention=not previous or previous.get("fingerprint") != report.fingerprint or bool(report.balance_attempt and report.balance_attempt != previous.get("balance_attempt")))
            else:
                send_email(delivery_report, env)
            if delivered:
                state[channel] = {"fingerprint": report.fingerprint, "last_sent": now, "conditions": list(report.conditions)}
                if report.balance_attempt:
                    state[channel]["balance_attempt"] = report.balance_attempt
                save_state(args.state, state)
                note(f"{channel}：{'告警' if report.active else '恢复通知'}已发送。", env)
            else:
                # No outstanding issue exists. Remember health without claiming delivery.
                state[channel] = {"fingerprint": HEALTHY, "last_sent": now, "conditions": []}
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
