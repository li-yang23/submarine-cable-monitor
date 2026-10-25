"""Durable incidents: actionable mail now, unresolved external gaps weekly.

Absence from a run is not evidence of recovery. Only an explicit successful
request, completed queue record, or successful inference closes an incident.
"""
from datetime import datetime, timezone, timedelta
from collections import Counter
import hashlib
import json
import re
from urllib.parse import urlsplit

from src.utils.audit import safe_url

WEEK = 7 * 86400
EXTERNAL_STAGES = {"fetch", "resolve", "discover", "discovery_seed", "cable_page", "rss", "source",
                   "cross_source_fetch", "cross_source_search"}
NETWORK_ERRORS = {"HTTPError", "ReadTimeout", "ConnectTimeout", "Timeout", "TimeoutError",
                  "ConnectionError", "SSLError", "ProxyError", "DNSError", "ConnectError",
                  "PublisherAccessError", "SubscriptionRequiredError"}


def external_failure(item):
    """Do not hide programming exceptions merely because they occurred in fetch."""
    return item.get("stage") in EXTERNAL_STAGES and (
        item.get("http_status") is not None or item.get("error_type") in NETWORK_ERRORS
        or item.get("reason") in {"google_news_decode_failed", "directory_unresolved",
                                   "no_articles_in_seed_traversal", "subscription_required"})


def identity(item):
    url = safe_url(item.get("url") or item.get("discovered_url") or "")
    # Rolling RSS dates must not create a new incident every day.
    url = re.sub(r"(?:after|before)(?:%3A|:)[0-9-]+", "", url, flags=re.I)
    return url or str(item.get("queue_id") or item.get("source") or item.get("stage") or "unknown")


def describe(item):
    code, reason = item.get("http_status"), str(item.get("reason") or "unknown")
    if reason == "subscription_required":
        return "原文需要订阅，公开全文不可用；保留缺口，不自动购买或绕过限制"
    if code in {202, 401, 403, 406, 451} or "access" in reason:
        return "来源网站限制访问；公开请求和替代来源不能保证取得原文"
    if code in {404, 410} or reason == "publisher_missing":
        return "来源页面已失效；保留原始链接并尝试可靠替代来源"
    if code in {429, 500, 502, 503, 504}:
        return f"来源返回 HTTP {code}；已按请求重试策略处理，后续任务继续退避重试"
    if "Timeout" in str(item.get("error_type")):
        return "来源请求超时；后续任务继续重试，无法控制对方响应时间"
    if reason.startswith("cross_source_") or item.get("status") in {"review", "unresolved"}:
        return "原文或替代来源证据不足，无法确认；保留具体原因：" + reason
    return "未取得可用正文或链接；保留重试和替代来源核查记录：" + reason


def reconcile(state, summary, queue, records, now, *, summary_error=None, workflow_status="success", run_url=None, steps=None):
    policy = state.setdefault("policy", {"version": 2, "started_at": now, "issues": {}, "deliveries": {}})
    issues = policy["issues"]
    run = str(summary.get("run_id") or run_url or now)

    def observe(kind, key, label, *, url="", action="", evidence=None):
        uid = hashlib.sha256((kind + ":" + key).encode()).hexdigest()[:24]
        previous = issues.get(uid, {})
        episode = previous.get("episode", 1) + int(previous.get("status") == "resolved")
        fresh = previous.get("last_run") != run
        issues[uid] = {**previous, "kind": kind, "status": "open", "episode": episode,
                       "first_seen": previous.get("first_seen", now), "last_seen": now, "last_run": run,
                       "observations": previous.get("observations", 0) + int(fresh),
                       "label": str(label)[:700], "url": safe_url(url), "action": action,
                       "run_url": run_url or previous.get("run_url", ""), "evidence": evidence or {}}
        return uid

    # Close only with positive evidence, and before recording current failures.
    succeeded = {identity(r) for r in records if r.get("outcome") == "completed"
                 and r.get("stage") in {"fetch", "rss", "discover", "cable_page", "cross_source_fetch"}}
    done_urls = {identity(r) for r in queue if r.get("status") == "done"}
    for item in issues.values():
        if item["kind"] in {"external", "automatic"} and item.get("url") in succeeded | done_urls:
            item.update(status="resolved", resolved_at=now)

    health = summary.get("model_health", {})
    if not isinstance(health, dict):
        health = {}
    if health.get("reason") == "inference_succeeded":
        for item in issues.values():
            if item.get("evidence", {}).get("component") == "model":
                item.update(status="resolved", resolved_at=now)
    provider = {"siliconflow": "SiliconFlow", "openai": "OpenAI"}.get(health.get("provider"), "当前模型服务")
    code, reason = health.get("http_status"), health.get("reason")
    if code == 402 or reason == "balance_exhausted":
        observe("user", "model_balance", provider + " 返回余额不足，模型处理已暂停",
                action="请登录当前 API 密钥所属的 " + provider + " 账户充值。无需重新运行或重配服务；余额暂停到期后的定时任务会自动重试，未处理新闻仍保留。",
                evidence={"component": "model", "reason": "balance_exhausted"})
    elif code in {401, 403} or reason in {"authentication_failed", "missing_api_key"}:
        secret = "OPENAI_API_KEY" if health.get("provider") == "openai" else "SILICONFLOW_API_KEY"
        repo = (run_url or "").split("/actions/")[0]
        observe("user", "model_auth", provider + " API 凭据缺失、失效或无权限",
                action=f"请在 {provider} 控制台创建有模型调用权限的有效密钥，然后在仓库 Settings → Secrets and variables → Actions 更新 {secret}。" +
                       (f"设置地址：{repo}/settings/secrets/actions 。" if repo else "") + "更新后定时任务会自动重试，请勿将密钥发到邮件或聊天中。",
                evidence={"component": "model", "reason": "authentication_failed"})
    elif health.get("available") is False and reason not in {"balance_check_disabled", "balance_check_unsupported"}:
        observe("automatic", "model_transient", provider + " 暂时调用失败，待自动重试；" + str(reason),
                evidence={"component": "model", "reason": str(reason)})

    for item in summary.get("failure_details", []) + summary.get("unavailable_evidence", []):
        if item.get("stage") in {"extract", "extraction", "model_health"}:
            continue
        kind = "external" if external_failure(item) or item in summary.get("unavailable_evidence", []) else "internal"
        observe(kind, identity(item), describe(item) if kind == "external" else "程序处理异常，需修复代码：" + str(item.get("reason")),
                url=identity(item), evidence={k: item[k] for k in ("stage", "reason", "http_status", "error_type") if k in item})
    for item in queue:
        if item.get("status") not in {"review", "unresolved", "retry"}:
            continue
        # Short-lived retries are not yet unsolvable. They enter the weekly list
        # only after repeated attempts, or when a durable evidence gap is known.
        if item.get("status") == "retry" and item.get("attempts", 0) < 3:
            continue
        if str(item.get("reason", "")).startswith("model_") or item.get("error_type") == "ExtractionUnavailable" or item.get("reason") in {"balance_exhausted", "authentication_failed", "missing_api_key", "rate_limited"}:
            continue
        observe("external", identity(item), describe(item), url=identity(item),
                evidence={k: item[k] for k in ("status", "reason", "attempts", "queue_id") if k in item})
    if summary_error:
        observe("internal", "summary", "运行摘要缺失或无法读取；无法确认本轮处理结果")
    else:
        uid = hashlib.sha256(b"internal:summary").hexdigest()[:24]
        if uid in issues:
            issues[uid].update(status="resolved", resolved_at=now)
    # Generic failures cannot turn into a useful user action by asking the user
    # to read logs. Record them honestly for repair, without invented instructions.
    if summary.get("errors") and not summary.get("failure_details"):
        observe("internal", "processing", "程序处理失败，摘要未提供足够的根因信息")
    if summary.get("notification_scope") == "infrastructure" and workflow_status == "success":
        for item in issues.values():
            if item.get("evidence", {}).get("component") == "infrastructure":
                item.update(status="resolved", resolved_at=now)
    if workflow_status != "success" and (not summary or summary.get("runtime_status", summary.get("status")) == "success"):
        observe("internal", "workflow", "采集之外的工作流步骤失败，尚未确定可由用户执行的修复操作",
                evidence={"component": "infrastructure"})
    for step, outcome in (steps or {}).items():
        if outcome == "failure":
            observe("internal", "workflow:" + step, "工作流步骤失败：" + step,
                    evidence={"component": "infrastructure", "step": step})
        elif outcome == "success":
            for item in issues.values():
                if item.get("evidence", {}).get("step") == step:
                    item.update(status="resolved", resolved_at=now)
    # Resolved evidence is retained for 90 days; open gaps are never evicted.
    policy["issues"] = {k: v for k, v in issues.items() if v["status"] == "open" or now - v.get("resolved_at", now) < 90 * 86400}
    return policy


def plan(policy, channel, now):
    delivery = policy.get("deliveries", {}).get(channel, {})
    sent = delivery.get("actions", {})
    actions = [(k, i) for k, i in policy["issues"].items() if i["kind"] == "user" and i["status"] == "open"
               and sent.get(k) != i["episode"]]
    messages = []
    if actions:
        body = ["需要你完成以下操作；其他可自动处理的问题由系统继续处理。"]
        for _, item in actions:
            body.extend(["", item["label"], item["action"], "运行记录：" + item.get("run_url", "")])
        messages.append({"type": "action", "subject": "[海缆监控] 需要你操作：" + "、".join(i["label"] for _, i in actions),
                         "body": "\n".join(body), "actions": {k: i["episode"] for k, i in actions}})
    if now - delivery.get("last_digest", policy["started_at"]) >= WEEK:
        unresolved = [(k, i) for k, i in policy["issues"].items() if i["status"] == "open" and i["kind"] != "user"]
        if unresolved:
            day = datetime.fromtimestamp(now, timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
            lines = [f"截至 {day}，仍未确认解决 {len(unresolved)} 项。你无需操作；原始记录保留，未将未读新闻标成完成。",
                     "已恢复的问题不列入。下列事项需要来源恢复、可核实的新证据，或尚未找到的程序修复方案。"]
            groups = Counter((urlsplit(i["url"]).hostname or "模型或程序") for _, i in unresolved)
            lines.append("涉及来源：" + "；".join(f"{host} {count} 项" for host, count in groups.most_common()))
            lines.append("以下列出最近确认的最多 8 项；全部问题及原因保存在问题台账。")
            for _, i in sorted(unresolved, key=lambda x: x[1]["last_seen"], reverse=True)[:8]:
                last = datetime.fromtimestamp(i["last_seen"], timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
                lines.extend(["", i["label"], f"记录次数：{i['observations']}；最近记录：{last}（北京时间）。",
                              i["url"] or i.get("run_url", "")])
            run_url = next((i.get("run_url") for _, i in unresolved if i.get("run_url")), "")
            if run_url:
                lines.append("\n完整问题台账：" + run_url.split("/actions/")[0] + "/blob/main/data/alert-state.json")
            messages.append({"type": "digest", "subject": "[海缆监控] 每周未解决问题汇总", "body": "\n".join(lines)})
    return messages


def acknowledge(policy, channel, message, now):
    delivered = policy.setdefault("deliveries", {}).setdefault(channel, {})
    if message["type"] == "action":
        delivered.setdefault("actions", {}).update(message["actions"])
    else:
        delivered["last_digest"] = now
