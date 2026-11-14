---
name: 数据深度分析与可视化
description: 当用户需要统计分析数据、清洗 CSV/JSON、生成图表或撰写数据报告时激活此技能。
tags: data, python, visualization, pandas
---

# 数据分析规范指南

## 技能说明
当你面临数据分析、CSV处理或可视化任务时，请遵循以下规范：
1. **工作空间保护**：在执行前，先使用 `file_list` 确认输入数据文件的位置。所有生成的中间清洗文件与图表必须保存在 `./workspace` 中。
2. **标准环境**：使用 `python_eval` 或 `bash_run` 编写 Python 脚本，推荐使用内置的 `pandas`, `json`, `sqlite3` 等标准库或已安装库。
3. **图表输出**：如需输出图表，优先生成 SVG 矢量图或使用 `matplotlib` 保存为 PNG 到 `./workspace/chart.png`，并在最终回复中提供查看路径。
4. **统计摘要**：必须在回复中包含核心指标对比（均值、中位数、趋势变化），不得只贴代码。

