---
name: 自主网络调研与深度摘要
description: 当用户需要抓取线上资讯、分析网页内容、整理技术文档或进行竞品调研时激活此技能。
tags: web, scraper, research, markdown
---

# 网络调研与深度摘要规范

## 技能说明
1. **抓取工具优先**：使用 `web_fetch` 工具获取网页 Markdown 或文本内容。
2. **内容提炼**：忽略页面冗余的导航栏、广告和页脚，提取核心正文、要点与数据指标。
3. **结构化产出**：若用户要求输出报告，将调研成果以 Markdown 格式保存到 `./workspace/research_report.md`，并在回答中展示摘要大纲。

