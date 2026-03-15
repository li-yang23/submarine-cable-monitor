# Submarine Cable Monitor

全球海缆事件自动监控系统。自动监控多个数据源，收集海底光缆的故障、维护和修复事件。

## Features

- **多源数据采集**:
  - TeleGeography 海缆地图
  - Infrapedia 海缆数据库
  - CableFaults 故障报告
  - Google News RSS 新闻监控
  - GitHub 相关技术仓库监控

- **数据存储**:
  - SQLite 数据库存储
  - JSON/CSV 导出
  - 2年数据保留策略

- **Web Frontend (GitHub Pages)**:
  - Interactive table view with all events
  - Filter by any column (source, type, status, date range, etc.)
  - Sortable columns
  - Pagination for large datasets
  - Statistics dashboard

- **自动化**:
  - GitHub Actions 定时运行
  - 自动部署 GitHub Pages
  - 失败时自动创建 Issue 通知
  - 支持手动触发

## Quick Start

### Local Setup

1. Clone the repository:
```bash
git clone https://github.com/your-username/submarine-cable-monitor.git
cd submarine-cable-monitor
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Initialize the database:
```bash
python -m src.main --init-db
```

4. Run the monitor:
```bash
python -m src.main
```

### Command Line Options

```bash
# Initialize database
python -m src.main --init-db

# Export data to JSON
python -m src.main --export-json data/export.json

# Export data to CSV
python -m src.main --export-csv data/export.csv

# Clean up old events
python -m src.main --cleanup

# Run specific scrapers
python -m src.main --scrapers google_news github
```

## GitHub Actions & Pages Setup

1. Fork this repository

2. Enable GitHub Pages:
   - Go to Settings → Pages
   - Under "Build and deployment", select:
     - Source: Deploy from a branch
     - Branch: `gh-pages` (will be created automatically) or `main` with folder `/docs`
   - Alternatively, the workflow uses `actions/deploy-pages` which handles this automatically

3. Enable write permissions for GitHub Actions:
   - Go to Settings → Actions → General
   - Under "Workflow permissions", select "Read and write permissions"
   - Also enable "Allow GitHub Actions to create and approve pull requests"

4. The workflow will automatically:
   - Run daily at 02:00 UTC
   - Run on manual trigger (workflow_dispatch)
   - Deploy updates to GitHub Pages

5. Access the webpage at:
   `https://your-username.github.io/submarine-cable-monitor/`

## Project Structure

```
submarine-cable-monitor/
├── src/
│   ├── scrapers/              # Data collection module
│   │   ├── base_scraper.py     # Abstract base class
│   │   ├── telegeography.py    # TeleGeography
│   │   ├── infrapedia.py       # Infrapedia
│   │   ├── cablefaults.py      # CableFaults
│   │   ├── google_news.py      # Google News
│   │   └── github_scraper.py   # GitHub
│   ├── storage/
│   │   ├── database.py         # SQLite database operations
│   │   └── models.py           # Data models
│   ├── utils/
│   │   ├── logger.py           # Logging config
│   │   └── config.py           # Configuration
│   └── main.py                 # Main entry point
├── docs/                       # GitHub Pages frontend
│   ├── index.html              # Main webpage
│   └── data/                   # Exported data for frontend
│       ├── export.json
│       └── export.csv
├── .github/workflows/
│   └── monitor.yml             # GitHub Actions workflow
├── data/                        # Data directory (gitignored)
├── logs/                        # Logs directory (gitignored)
├── requirements.txt
├── config.yaml
└── README.md
```

## Configuration

Edit `config.yaml` to customize:

- Request delays and timeout
- Database path and retention policy
- Scraper enable/disable
- Update intervals

## Data Model

Events table schema:

| Field | Type | Description |
|-------|------|-------------|
| id | INTEGER | Primary key |
| source | TEXT | Data source name |
| event_type | TEXT | Event type (fault/outage/repair/etc) |
| cable_name | TEXT | Cable name |
| location | TEXT | Location description |
| status | TEXT | Current status |
| reported_at | TIMESTAMP | When reported |
| resolved_at | TIMESTAMP | When resolved |
| description | TEXT | Event description |
| url | TEXT | Source URL |
| raw_data | TEXT | Raw JSON data |
| created_at | TIMESTAMP | DB creation time |
| updated_at | TIMESTAMP | DB update time |

## License

MIT License
