# 菲律宾海缆事件遗漏排查（2026-10-14）

结论：不是简单的 Google News 未收录。已确认 Google 抓取配额导致后续关键词没有执行；10 月初模型因余额不足全部回退规则；SubTel 页面推荐区被当成正文，菲律宾事件被错误识别为 WACS 并合并。旧日志没有逐篇发现/拒绝记录，因此不能确定两条指定 URL 在每一天具体走到了哪一步。

## 线上证据

检查时远程 main 为 `09a2af3e7d90f1014d09f11695521d9c0843173c`，提交时间 2026-10-13 08:57:58 UTC。该版本导出的事件共 451 条，[固定版本数据](https://github.com/li-yang23/submarine-cable-monitor/blob/09a2af3e7d90f1014d09f11695521d9c0843173c/docs/data/events.json)中没有两条指定新闻 URL，但存在同一事件的 SubTel 报道。

| 运行 | Google 日志 | 抽取日志及结果 |
| --- | --- | --- |
| [10 月 1 日](https://github.com/li-yang23/submarine-cable-monitor/actions/runs/36834863827) | 仅 `submarine cable fault`，返回 12 篇 | 23 次 HTTP 402 / code 30001 / account balance is insufficient；23 篇全部回退规则；0 新增、11 合并、12 拒绝、errors=0 |
| [10 月 4 日](https://github.com/li-yang23/submarine-cable-monitor/actions/runs/37186551288) | 仅 `submarine cable fault`，返回 12 篇 | 23 次同样的余额不足；0 新增、12 合并、11 拒绝、errors=0 |
| [10 月 6 日](https://github.com/li-yang23/submarine-cable-monitor/actions/runs/37436316009/job/112178883554) | 仅 `submarine cable fault`，返回 12 篇 | 23 次同样的余额不足；1 新增、9 合并、13 拒绝、errors=0；下述污染记录在本次创建 |
| [10 月 9 日](https://github.com/li-yang23/submarine-cable-monitor/actions/runs/37904607013) | 仅 `submarine cable fault`，返回 12 篇 | 未见模型失败日志；51 候选、2 新增、23 合并、26 拒绝 |
| [10 月 10 日](https://github.com/li-yang23/submarine-cable-monitor/actions/runs/38036349912) | 仅 `submarine cable fault`，返回 12 篇 | 1 次模型 JSON 解析失败后回退；41 候选、1 新增、15 合并、25 拒绝 |
| [10 月 13 日](https://github.com/li-yang23/submarine-cable-monitor/actions/runs/38372172365/job/115211579159) | 仅 `submarine cable fault`，返回 12 篇 | 未见模型失败日志；52 候选、4 新增、22 合并、26 拒绝 |

上述运行均为 success。`EventExtractor.available` 只判断是否配置 API key；旧 pipeline 用它标记 `verified`，没有区分模型成功与规则回退，也没有把回退计入 errors，所以运行成功和 verified 均不足以证明提取正常。

## 发现阶段

旧 `GoogleNewsArticleScraper` 按固定关键词顺序遍历，收齐 12 篇就返回，最大尝试 30 次也是全局共享。已读日志均只出现第一个关键词；`submarine cable outage`、`submarine cable break`、`undersea cable damage` 和两个中文关键词没有执行。当前默认 RSS 查询也没有限定近期窗口。

2026-10-14 实时只读查询得到：

- [submarine cable outage RSS](https://news.google.com/rss/search?q=submarine%20cable%20outage&hl=en-US&gl=US&ceid=US%3Aen) 返回 100 条，GMA 报道在第 12 条，标题日期为 October 2，pubDate 为 2026-10-02。
- [菲律宾定向 RSS](https://news.google.com/rss/search?q=Philippines%20submarine%20cable%20after%3A2026-10-01%20before%3A2026-10-14&hl=en-US&gl=US&ceid=US%3Aen) 返回 23 条，BusinessWorld 在第 6 条、GMA 在第 7 条。

这些结果证明 Google 当前收录了两篇报道，不能反推它们在历史运行中的排名或是否曾被返回。旧日志未保存 RSS 条目清单、URL 解码失败的全部原因、每篇候选与拒绝原因，无法补出缺失的历史链路。

## 已定位的污染事件

事件 UID：`evt_94aed31196865b242bad`；创建于 2026-10-06 08:28:57 UTC。

- title：`Philippines-Singapore C2C Link Restored`
- cable_name：错误的 `WACS`
- occurrence_date：`2026-10-05`，取的是 SubTel 转载发表日
- evidence_urls：`https://subtelforum.com/philippines-singapore-c2c-link-restored/` 和 `https://subtelforum.com/philippines-cable-disruption-slows-internet/`
- original_text：`you might also like Severed cable blamed for Syrian Internet cut Categories: Cable Faults & Maintenance Syria's internet restored after blackout Categories: Cable Faults & Maintenance New WACS outage details Categories: Cable Faults & Maintenance`
- raw_data.article.content：真实 C2C 正文后面混入订阅、编辑推荐和相关阅读内容；raw_data.extracted 与旧规则输出一致。

旧规则不识别 C2C，证据句评分偏爱含已知海缆名及事故词的句子，因此选中了推荐区的 WACS。EventStore 又把同海缆同日期直接判为重复，两个菲律宾报道被并入这条错误记录。线上另有 17 条 WACS 记录也含 `you might also like` 证据，共 18 条需要定向复核，不能把所有 WACS 事件一概删除。

## 日期与事实边界

[GMA 用户链接](https://www.gmanetwork.com/news/scitech/technology/1004568/slow-intermittent-internet-in-ph-on-friday-oct-10-2026-linked-to-submarine-cable-outage/story/)的 URL/搜索标题含 October 10，但搜索索引正文写 Published October 2, 2026 8:08pm；Google News RSS 标题和日期也是 October 2。10 月 10 日是周六，10 月 2 日是周五。直接抓取 GMA 和 BusinessWorld 在本次 Web 访问返回 403；这不能证明历史 Actions 也遭遇同一 403。

可访问的 [SubTel 故障报道](https://subtelforum.com/philippines-cable-disruption-slows-internet/)明确引用 GMA 的 10 月 2 日报道，自身发表于 10 月 5 日；它没有确认具体海缆、物理断点或根因。[SubTel C2C 恢复报道](https://subtelforum.com/philippines-singapore-c2c-link-restored/)引用 10 月 3 日报道，明确 C2C 菲律宾—新加坡段于当天恢复、香港段同时处于维护，但不证明发生物理断缆。

## 定点修复建议

1. 先保留污染记录及其所有 evidence_urls 的修复前快照，重新抓取并剔除非正文区域；审计记录保留旧 UID、新 UID 与修复理由。
2. 对 `evt_94aed31196865b242bad` 按真实正文重建事件，使用 10 月 2 日发生、10 月 3 日恢复的证据；区分 C2C 明确指认与未具名的跨境段报道，避免把约 300 Gbit/s 的运营商容量声明直接泛化成整条 C2C 的损失。
3. 对其余 17 条推荐区污染记录逐条回放：有真实事故则重提取、仅政策/演习/产品报道则剔除；保留正常 WACS 记录。
4. 让关键词公平获得抓取机会；记录发现、跳过、URL 解码、正文长度、抽取模式、回退错误、拒绝原因、入库/合并目标 UID。审计文件作为 Actions artifact 保存，关键异常进入 Job Summary。
5. 对 402 做当次运行降级告警，区分 `llm`、`rules` 和 `rules_fallback`，不要把有 API key 等同于模型验证成功。

本次未修改线上数据或仓库既有代码。读取的日志和 RSS 原始快照位于本机 `/tmp/cable-job-*.log`、`/tmp/cable-google-rss-probe.json`、`/tmp/cable-remote-events-20261013.json`，临时目录不应作为长期审计存储。


## 已完成的本地修复与验证

- 采集改为全部关键词发现后公平选择，并记录未选中的预算、缓存、重复、日期等原因；仅成功模型处理的文章缓存7天，回退和失败仍重试。
- 排除 SubTel 推荐栏、相关海缆和杂志组件；支持 C2C 及明确未具名海缆事故。未知发生日不再回填发表日，预计修复日期不当作已修好日期。
- 每次运行生成轻量 JSONL、JSON/Markdown 摘要和 Actions 附件，支持按标题、URL、article_id、event_uid 查询整条处理链。一次真实 RSS 小样本产生186条审计记录、约126 KiB，不保存正文或模型响应。
- 本地隔离18条推荐栏污染记录并补回C2C（`evt_f31edbfe4d33fc4c02d2`，2026-10-02发生、10-03恢复），451条变为434条。其余433条原始记录指纹和行字节均保持不变，包括12条正常WACS记录。JSON/CSV导出均为434条。
- 修复工具支持逐URL检查点、源数据指纹保护、完整备份、原子替换和幂等重放，不通过模糊合并覆盖现存事件。所有21篇来源、审核候选和未采用线索保留在本地修复目录。
- 49项Python测试与11项登录后端测试通过；浏览器验证了登录、数据接口拦截、两类事件切换、退出、会话失效和移动端布局。

备份与逐条修复映射位于 `logs/repairs/20261014-reviewed-repair-network/`：`final-minimal-plan.before.jsonl`、`final-minimal-plan.applied.jsonl`、`final-minimal-verification.json`。该目录被Git忽略。其他历史重复、推测缆名及部分误用日期已列在同目录的 `same-source-cable-matches.md`，本次不批量改写这些非推荐区污染记录；它们不应被视为已全面清洗。

用户已授权通过PR检查后合并发布采集、日志和定点数据修复，本次发布保留现有GitHub Pages。登录后端和Cloudflare Radar页面另行准备，等待有效Cloudflare凭据后再迁移；真实访问限制上线时需要关闭旧GitHub Pages并将存储事件数据的仓库设为私有。
