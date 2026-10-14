# Submarine Cable Monitor

海缆舆情数据收集与监控系统。系统从 Google News、SubTel Forum、Submarine Networks 等来源采集海缆相关文章，抽取海缆事故事件，做去重与核验，然后导出静态 JSON/CSV 给 GitHub Pages 展示。

## Features

- 多源采集：
  - Google News RSS
  - SubTel Forum cable faults & maintenance
  - Submarine Networks cable article pages
  - TeleGeography / Infrapedia 配置保留，默认关闭，待真实 API 权限确认后启用
- 事件处理：
  - OpenAI-compatible LLM 事件抽取
  - 模型不可用时保留新闻等待重试，不自动退回规则抽取
  - 事件字段标准化、日期清洗、文本相似度去重
  - 重复事件合并 source/evidence URL
- 部署：
  - GitHub Actions 每4小时第17分钟运行（北京时间 00:17、04:17、08:17、12:17、16:17、20:17）
  - 已完成事件、未完成新闻队列和告警状态分别持久化
  - GitHub Pages 静态前端展示，不暴露任何 API key

## Quick Start

```bash
pip install -r requirements.txt
python -m src.main --init-db
python -m src.main --run --dry-run
python -m src.main --export-json docs/data/events.json --export-csv docs/data/events.csv
```

默认不需要 MongoDB。canonical store 是 `data/events.jsonl`，Pages 读取 `docs/data/events.json`。

## Historical Import

可从 `submarine-event-extractor` 导入历史事件 CSV 作为去重基础：

```bash
python -m src.main --import-history /Users/liyang/Documents/Projects/submarine-event-extractor/data-217-20260116.csv
python -m src.main --export-json docs/data/events.json --export-csv docs/data/events.csv
```

导入会生成稳定 `event_uid`，并保留历史字段到 `raw_data`。

## Collection Pipeline

```bash
# Run all enabled sources
python -m src.main --run

# Run selected sources
python -m src.main --run --sources google_news subtelforum submarinenetworks

# Process a small sample without writing
python -m src.main --run --dry-run
```

## Diagnostics

每次采集在 `logs/runs/<run_id>/` 生成 `audit.jsonl`、`summary.json` 和 `summary.md`。日志记录发现结果、队列状态、正文获取失败、模型失败、复核原因和入库/合并 UID，不保存全文、模型响应或密钥。Actions Summary 包含模型健康状态与 pending/retry/review 数量；采集不健康时工作流明确失败，并继续尝试保存队列、投递告警和上传 `monitor-output-<run_id>-<attempt>` 附件，保留30天。

```bash
python -m src.main --run --sources google_news --since 2026-10-01
python -m src.main --audit-search Philippines
python -m src.main --audit-search gmanetwork.com --audit-limit 0
gh run download RUN_ID --pattern 'monitor-output-*' --dir /tmp/monitor-audit
python -m src.main --audit-dir /tmp/monitor-audit/logs/runs --audit-search Philippines
```

可用 `--audit-run RUN_ID` 限定运行。查询会关联同一 article_id 的其他处理阶段；日志未命中不能证明 Google 从未收录。 Submarine Networks 目录日志还记录 HTTP 状态、页面标题、响应字节数、链接数和匹配新闻数；明显空页或防爬页、缺失种子文件、完整目录遍历零发现会显式告警，便于区分来源故障与正常无新增。

Google 默认查询近14天。生产配置不再设置固定文章数量配额；发现的新闻先写入 `data/article-queue.json`，再按队列处理。单次运行设1200秒采集预算，未完成新闻留到下次运行，不因预算耗尽、RSS 过期或旧缓存淘汰而删除。工作流总时限45分钟，为导出、保存和通知留出时间。正文抓取失败或模型异常保留为 retry 并退避重试；模型候选因证据不足等原因被拒绝时保留为 review，两种状态都会告警。

缺少模型密钥、余额耗尽、认证失败、限流、服务异常或无效模型响应都不会触发生产规则回退。账户不可用时暂停后续模型请求；恢复后继续处理队列。已经完成的新闻按 URL 去重，不重复付费处理。正文排除推荐栏，未知发生日期不使用发表日补齐；`rule_verified` 表示候选经过规则检查，不表示原始新闻事实已经独立核实。

这能避免已发现新闻被静默丢弃，但不能保证搜索引擎收录全部报道或网站始终允许抓取。未收录新闻需要增加来源或补充 URL；403、模型失败和待复核新闻都会明确暴露，不能把工作流运行结束等同于全部新闻已处理。

2026-10-14 的漏报证据与定点修复见[排查报告](research/incident-audit-2026-10-14.md)。

## Queue Operations

队列维护命令不会自动调用模型。列出未完成新闻后，可以按 ID 或 URL 重新排队；修复原因后再运行采集。

```bash
python -m src.main --queue-list
python -m src.main --queue-retry ARTICLE_ID_OR_URL
python -m src.main --queue-retry all-retry
python -m src.main --queue-retry all-review
python -m src.main --enqueue-url https://example.com/cable-incident
python -m src.main --queue-import-audit /tmp/monitor-audit/logs/runs/RUN_ID/audit.jsonl --since 2026-10-01
python -m src.main --run
```

`--enqueue-url` 可重复指定，适用于搜索尚未收录但已知地址的公开新闻。`--queue-import-audit` 从旧日志恢复已发现的新闻，`--since` 按有记录的发表日期筛选导入条目；已经存在的未完成队列不会因为日期窗口变旧而删除。重试 review 前应检查拒绝原因，重复调用模型本身不能补足原文没有的证据。

## API Configuration

本地开发可以放 `.env` 或 `config.env`，GitHub Actions 使用 Secrets/Variables。

Required/optional environment variables:

- `SILICONFLOW_API_KEY` or `OPENAI_API_KEY`
- `SILICONFLOW_BASE_URL`, default `https://api.siliconflow.cn/v1/`
- `LLM_MODEL`, default `deepseek-ai/DeepSeek-V3`
- `EMBEDDING_MODEL`, default `BAAI/bge-m3`

API key 不应提交到仓库。Actions workflow 会从 GitHub Secrets 注入。

## Health Notifications

SiliconFlow 已在[官方公告](https://api-docs.siliconflow.cn/docs/release-notes/overview)中明确：`/user/info` 于2026年8月14日下线，账户级替代接口将另行公布。截至2026年10月14日未找到受支持的替代余额 API，因此系统不会继续请求旧接口，也不能实现真实余额低于5时的提前提醒。运行摘要明确记录 `health_unknown / balance_api_retired`，不伪造余额或报告健康；`llm.low_balance_threshold` 暂留作未来接口兼容配置，目前不生效。严格提取仍可执行，实际余额不足、认证失败或其他模型故障会触发告警并保留队列。

为降低余额耗尽风险，可由账号持有人在 SiliconFlow 控制台自行设置[支付宝自动充值](https://docs.siliconflow.cn/docs/userguide/faqs/misc_finance)。官方说明该功能每10分钟检查余额，触发前发送短信；它涉及签约和实际扣款，本项目不会代为开启，也不能保证余额永远充足。不启用自动充值时，需要在控制台定期核对余额，不能把调用成功当作剩余额度充足。

当前使用仓库 issue 通知 `@li-yang23`，不等待 SMTP 配置。需要 GitHub 账号通知设置允许邮件，issue 通知才会转发到账号邮箱；系统不会假定邮件已送达。通知包含运行链接、模型问题及未处理/待复核数量，不包含新闻全文或密钥。同一问题状态不变时每天提醒一次，状态变化即时通知，确认恢复后关闭告警。`data/alert-state.json` 只保存通知指纹和时间。

要直接发送邮件，在仓库 Settings → Secrets and variables → Actions 配置以下 Secrets：

| Secret | 用途 |
| --- | --- |
| `SMTP_HOST` | 邮箱服务商的 SMTP 主机名 |
| `SMTP_PORT` | `587`（STARTTLS，默认）或 `465`（TLS） |
| `SMTP_USERNAME` | SMTP 登录账号 |
| `SMTP_PASSWORD` | SMTP 密码或邮箱授权码 |
| `SMTP_FROM` | 发件邮箱地址 |
| `ALERT_EMAIL_TO` | 接收邮箱地址，多个地址用逗号分隔 |

SMTP 未配置时只执行 GitHub issue 通知，并在 Actions 摘要明确提示。邮件发送失败不会记录为成功；GitHub 渠道独立执行，下一次继续重试失败渠道。手动 `dry_run` 只预览告警，不发邮件、不更新 issue、不保存队列或通知状态。

## GitHub Pages

Workflow 会执行：

1. 安装依赖；
2. 初始化 `data/events.jsonl`；
3. 发现新闻入队，检查模型，处理到期队列；
4. 导出有效事件到 `docs/data/events.json` 与 `docs/data/events.csv`，不自动清理历史记录或补译；
5. 即使采集降级也保存有效事件和未完成队列，再发送并保存告警状态；
6. 上传诊断附件，部署成功导出的 `docs/` 到 GitHub Pages；
7. 有未解决问题或保存/通知失败时，将采集工作流标记为失败。

前端只读取静态文件，支持按 cable、source、accident type、verification status、日期和文本搜索筛选。

## Tests

```bash
python -m unittest discover -s tests
```

## Project Structure

```text
src/
  main.py                    CLI
  pipeline.py                durable collection and extraction pipeline
  processing/                extraction, normalization, similarity
  scrapers/                  Google News, SubTel Forum, Submarine Networks
  storage/                   JSON event store and legacy SQLite module
data/
  cable-links.json           Submarine Networks seed links
  events.jsonl               canonical event store
  article-queue.json         durable pending/retry/review/done news metadata
  alert-state.json           notification deduplication state
docs/
  index.html                 GitHub Pages frontend
  data/events.json           frontend data export
```

## License

MIT License
