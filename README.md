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

可用 `--audit-run RUN_ID` 限定运行。查询会关联同一 article_id 的其他处理阶段；日志未命中不能证明 Google 从未收录。 Submarine Networks 目录日志还记录 HTTP 状态、页面标题、响应字节数、链接数和匹配新闻数；明显空页或防爬页、缺失种子文件、完整目录遍历零发现会显式记录，便于区分来源故障与正常无新增。

Google 默认查询近14天。生产配置不再设置固定文章数量配额；发现的新闻先写入 `data/article-queue.json`，再按队列处理。单次运行设1200秒采集预算，未完成新闻留到下次运行，不因预算耗尽、RSS 过期或旧缓存淘汰而删除。工作流总时限45分钟，为导出、保存和通知留出时间。正文抓取失败或模型异常保留为 retry 并退避重试；模型候选因证据不足等原因被拒绝时保留为 review，队列状态只记日志，不独立触发邮件。

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

通知按责任和可执行操作分流：临时超时、限流和外站 5xx 由现有退避与后续定时任务重试；只在确需用户充值或更新密钥时立即发送邮件，正文说明平台、设置位置、具体操作，以及操作后系统如何恢复。同一问题只通知一次；新的每日失败、问题暂未被检查、次数变化不会重新通知。模型实际调用成功后才重新允许下一次独立故障通知。

网站拒绝访问、订阅全文不可用、失效链接、缺证和暂未找到修复方案的问题，保留在 `data/alert-state.json` 的 `policy.issues` 台账中。每 7 天由已有四小时定时任务发一封尚未解决的问题汇总（首次在新策略启用满 7 天后的首次运行发送）；没有未解决事项时不发送。没有再次遇到某问题不算解决，必须有成功请求、队列完成或模型实际成功的证据。摘要最多列出 8 项，附完整台账链接；所有未解决记录保留，不为减少通知而标成完成。

SMTP 可用时只发送邮件，不同时发 GitHub Issue 提及。SMTP 未配置时使用现有 Issue 备用渠道；投递失败保留待发送状态供下轮重试。通知及原有状态保存步骤在采集和部署后统一执行，没有新增定时任务、自动提交次数或访问权限。目录扫描预算、处理预算和恢复正常不另发通知。台账不保存收件人、凭据或完整正文，只保留经过脱敏的链接、原因、时间、状态和投递记录。

余额提前预警仍关闭。真正收到 HTTP 402 才暂停模型处理，未处理新闻留存，按北京时间次日零点解除暂停并由后续任务重试；同一余额问题不会每天重复催充值。证据不足不触发其他付费服务或自动购买订阅。

要直接发送邮件，在仓库 Settings → Secrets and variables → Actions 配置以下 Secrets：

| Secret | 用途 |
| --- | --- |
| `SMTP_HOST` | 邮箱服务商的 SMTP 主机名 |
| `SMTP_PORT` | `587`（STARTTLS，默认）或 `465`（TLS） |
| `SMTP_USERNAME` | SMTP 登录账号 |
| `SMTP_PASSWORD` | SMTP 密码或邮箱授权码 |
| `SMTP_FROM` | 发件邮箱地址 |
| `ALERT_EMAIL_TO` | 接收邮箱地址，多个地址用逗号分隔 |

SMTP 未配置时执行 GitHub issue 备用通知，并在 Actions 摘要明确提示未投递邮箱，不能视为已发邮件。邮件发送失败不会记录为成功；GitHub 渠道独立执行，下一次继续重试失败渠道。手动 `dry_run` 只预览告警，不发邮件、不更新 issue、不保存队列或通知状态。

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

### 抓取恢复与告警口径

正文失败只统计 `fetch/resolve` 阶段，目录和 RSS 发现失败单列；摘要的 `failure_summary` 按阶段、来源及 URL 去重，传播到上层的同一异常不重复计入。原始 audit 记录仍全部保留用于排查，邮件不再把目录数描述成新闻篇数。

公开网页优先普通 HTTP，拒绝旧客户端时使用浏览器兼容 TLS；线上启用 Chromium 渲染后备以处理 JavaScript 页面，不执行验证码、登录或付费墙操作。每个站点按配置间隔访问，HTTP 429 的 Retry-After 冷却写入队列供后续运行沿用。目录源连续拒绝时保存游标并报告覆盖缺口，避免继续撞击全部目录；正文队列不删除。

正文解析会恢复 React 流式 HTML 的内容块，并从多个正文容器中选择有内容的正文，避免把图片说明当成整篇文章。模型接收清洗后的完整正文，不再静默截断前 5000 个字符。余额不足暂停和次日重试规则保持生效。


## 自动交叉核实

`cross_source.enabled` 启用免费 Google News RSS 检索和公开正文抓取，不使用 Parallel。抓取失败的新闻在常规采集之后进入有界补证流程；每轮最多处理 2 条、每条抓取最多 4 个候选正文，共使用不超过 300 秒的阶段时间（单个正在执行的网络请求受自己的超时约束）。搜索围绕原标题、实体和发表日前后 14 天展开。可信站点及同属编辑机构的映射见 `src/processing/cross_source.py` 的 `PUBLISHERS`，未知域名不自动采信。

自动完成要求两个编辑机构的独立报道，正文能支持同一具体事件的身份和发生日期；同一新闻稿或转载不能凑数。模型负责语义核对，代码再次核验来源、逐字引文、日期、区域和电缆名称。不得把备用线路认作损坏线路，恢复服务不推断为物理修复。只有事件成功落盘后才将原记录置为 `done / cross_source_corroborated`；原文未读的事实保留在证据记录中，事件引用实际读取的替代正文 URL。

没有足够来源、只证实相近主题、原文范围不清、没有确定事故、日期或身份冲突均保留 `review`；搜索失败或本轮时间不足保留 `retry`。`cross_source.reason` 记录具体原因，`sources` 保存 URL、发表日、正文长度与哈希，`queries` 保存检索条件，不保存整篇正文。复核项默认 24 小时后再查新证据；连续三次成功检索仍是相同证据和缺失原因时，转为 `unresolved` 并停止核查。搜索失败、限流、服务异常或时间不足不计入这三次。已人工确认无法补齐的记录也可直接保留为 `unresolved`，不计作完成、不写入已处理缓存；已知需要原始公告更正的争议不会被普通转载自动覆盖。新发现的替代文章也会持久入队，下一轮按全文处理其中其他事故，不仅保留与原新闻相符的部分。

模型补证使用现有 SiliconFlow/OpenAI 配置，不改用其他付费服务；余额不足时同样立即暂停到北京时间次日并提醒。`llm.request_timeout` 默认为 120 秒，SDK 不在本轮自动重试。邮件立即报告需用户操作的认证和余额不足；超时、限流自动重试，尚未解决的问题进入每周汇总；目录发现/文章处理的运行时上限只记日志，不表示账户余额耗尽。对来源证据不足的保留原因可用 `--queue-list` 查看，对 `parent_queue_id` 搜索可追溯替代来源的发现和抓取过程。

候选复核：队列中的 `candidate_reviews` 和审计日志保存候选编号、海缆名、地点、拒绝原因、模型声称日期与校验后日期，以及最多 400 字符的证据片段和 300 字符的日期片段；不保存整份模型响应。每篇队列最多保留 100 项，超过时设置 `candidate_reviews_truncated` 并保留总数。旧运行缺少这些片段时，无法凭编号准确还原未命名候选，人工复核须明确此限制。

单篇原文在公开读取及浏览器回退后仍返回 HTTP 202/401/403/406/404/410/451 或已识别的 HTTP 200 验证页，记为来源覆盖缺口，每 24 小时最多重试一次，累计三次后保留为 `unresolved`。原始 URL、HTTP 状态和缺失原因保留。新闻目录的访问限制同样只记覆盖缺口；Submarine Networks 被拒目录保留在种子列表中，每个 URL 至少间隔 24 小时再试，恢复后继续扫描。验证页和冷却跳过不计作空目录故障。外站 HTTP 429/5xx、超时和订阅限制保留为覆盖缺口，不将整轮程序执行标为失败；模型、程序处理及保存故障仍使运行失败。摘要分别统计实际完成与停止核查；这些数据状态和网站访问限制仅记日志，不触发邮件或采集告警 issue。
