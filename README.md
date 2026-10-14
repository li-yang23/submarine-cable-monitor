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
  - 无 API key 时使用规则抽取，支持 `--dry-run`
  - 事件字段标准化、日期清洗、文本相似度去重
  - 重复事件合并 source/evidence URL
- 部署：
  - GitHub Actions 每天 UTC 02:00 自动运行
  - 结果提交到 `data/events.jsonl` 和 `docs/data/`
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

## Daily Pipeline

```bash
# Run all enabled sources
python -m src.main --run

# Run selected sources
python -m src.main --run --sources google_news subtelforum submarinenetworks

# Process a small sample without writing
python -m src.main --run --dry-run
```

## Diagnostics

每次采集在 `logs/runs/<run_id>/` 生成 `audit.jsonl`、`summary.json` 和 `summary.md`。日志包含所有关键词的发现结果，以及每篇新闻的预算/缓存跳过、抓取失败、模型回退、拒绝原因和入库/合并 UID，不保存全文、模型响应或密钥。Actions Summary 显示 success/degraded/failed；失败时也尝试上传 `monitor-output-<run_id>-<attempt>` 附件，保留30天。

```bash
python -m src.main --run --sources google_news --since 2026-10-01
python -m src.main --audit-search Philippines
python -m src.main --audit-search gmanetwork.com --audit-limit 0
gh run download RUN_ID --pattern 'monitor-output-*' --dir /tmp/monitor-audit
python -m src.main --audit-dir /tmp/monitor-audit/logs/runs --audit-search Philippines
```

可用 `--audit-run RUN_ID` 限定运行。查询会关联同一 article_id 的其他处理阶段；日志未命中不能证明 Google 从未收录。

Google 默认查询近14天，先发现全部关键词，再轮流选择文章，每次仍最多尝试30篇、成功抓取12篇。仅成功模型处理的文章缓存7天（包括无候选结果），失败和规则回退会重试；`data/article-state.json` 保留30天、最多2000个URL。正文排除推荐栏，未知发生日期不使用发表日补齐。`rule_verified` 仅表示规则检查通过。

2026-10-14 的漏报证据与定点修复见[排查报告](research/incident-audit-2026-10-14.md)。

## API Configuration

本地开发可以放 `.env` 或 `config.env`，GitHub Actions 使用 Secrets/Variables。

Required/optional environment variables:

- `SILICONFLOW_API_KEY` or `OPENAI_API_KEY`
- `SILICONFLOW_BASE_URL`, default `https://api.siliconflow.cn/v1/`
- `LLM_MODEL`, default `deepseek-ai/DeepSeek-V3`
- `EMBEDDING_MODEL`, default `BAAI/bge-m3`

API key 不应提交到仓库。Actions workflow 会从 GitHub Secrets 注入。

## GitHub Pages

Workflow 会执行：

1. 安装依赖；
2. 初始化 `data/events.jsonl`；
3. 运行采集和抽取；
4. 导出 `docs/data/events.json` 与 `docs/data/events.csv`；
5. 提交数据更新；
6. 部署 `docs/` 到 GitHub Pages。

前端只读取静态文件，支持按 cable、source、accident type、verification status、日期和文本搜索筛选。

## Tests

```bash
python -m unittest discover -s tests
```

## Project Structure

```text
src/
  main.py                    CLI
  pipeline.py                daily monitor pipeline
  processing/                extraction, normalization, similarity
  scrapers/                  Google News, SubTel Forum, Submarine Networks
  storage/                   JSON event store and legacy SQLite module
data/
  cable-links.json           Submarine Networks seed links
  events.jsonl               canonical event store
docs/
  index.html                 GitHub Pages frontend
  data/events.json           frontend data export
```

## License

MIT License
