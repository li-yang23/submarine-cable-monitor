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
