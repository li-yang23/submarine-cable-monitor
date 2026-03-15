帮我创建一个名为 submarine-cable-monitor 的项目，实现全球海缆事件自动监控。

## 项目结构要求

submarine-cable-monitor/
├── src/
│   ├── scrapers/              # 数据采集模块
│   │   ├── __init__.py
│   │   ├── base_scraper.py     # 抽象基类
│   │   ├── telegeography.py    # TeleGeography 海缆地图
│   │   ├── infrapedia.py       # Infrapedia 海缆数据库
│   │   ├── cablefaults.py      # CableFaults 故障报告
│   │   ├── google_news.py      # 新闻监控（海缆故障关键词）
│   │   └── github_scraper.py   # 抓取海缆相关的 GitHub 技术仓库
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── database.py         # SQLite 数据库操作
│   │   └── models.py           # 数据模型定义
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── logger.py           # 日志配置
│   │   └── config.py           # 配置管理
│   └── main.py                 # 主入口
├── .github/
│   └── workflows/
│       └── monitor.yml         # GitHub Actions 工作流
├── data/                        # 数据存储目录（gitignore）
├── logs/                        # 日志目录（gitignore）
├── requirements.txt
├── config.yaml                  # 配置文件
├── README.md
└── .gitignore

## 功能需求

1. **数据采集**：
   - 从 TeleGeography Submarine Cable Map API 获取海缆状态
   - 监控 Infrapedia 的海缆基础设施数据
   - 抓取 CableFaults 的故障报告（包括故障位置、修复时间）
   - Google News RSS 监控关键词："submarine cable fault"、"海缆故障"、"cable break"
   - 支持增量更新，避免重复抓取

2. **数据存储**：
   - 使用 SQLite 存储事件数据
   - 表结构：events(id, source, event_type, cable_name, location, status, reported_at, resolved_at, description, url, raw_data)
   - 支持导出为 JSON/CSV
   - 数据保留策略：保留最近2年数据

3. **GitHub Actions 自动化**：
   - 每天 UTC 02:00 自动运行
   - 支持手动触发（workflow_dispatch）
   - 失败时发送通知（通过 GitHub Issues 或邮件）
   - 自动提交数据更新到仓库（可选）

4. **其他要求**：
   - 使用 Python 3.11+
   - 添加类型提示
   - 包含错误重试机制（指数退避）
   - 遵守 robots.txt 和网站爬取礼仪
   - 添加 User-Agent 和请求延迟

## 配置参数示例

config.yaml 应包含：
- 各数据源的 URL 和更新频率
- 请求间隔（默认 1-3 秒随机延迟）
- 超时设置（默认 30 秒）
- 重试次数（默认 3 次）

## 交付标准

1. 代码可以直接运行 `python -m src.main` 测试
2. 包含 setup 脚本初始化数据库
3. README 包含本地运行和 GitHub Actions 配置说明
4. 添加 MIT 许可证
