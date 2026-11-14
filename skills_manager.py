import os
import re
from typing import Any

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")

SKILL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def validate_skill_id(skill_id: str) -> str:
    """Allow only safe directory names for skills."""
    if not SKILL_ID_PATTERN.fullmatch(skill_id or ""):
        raise ValueError("技能 ID 只能包含小写字母、数字、下划线和连字符，且不超过 64 个字符")
    return skill_id


def ensure_skills_dir() -> None:
    os.makedirs(SKILLS_DIR, exist_ok=True)


def parse_skill_md(file_path: str) -> dict[str, Any]:
    if not os.path.exists(file_path):
        return {}

    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    meta: dict[str, Any] = {
        "name": os.path.basename(os.path.dirname(file_path)),
        "description": "",
        "tags": [],
        "content": content,
    }

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            fm_text = parts[1]
            body_text = parts[2].strip()
            meta["content"] = body_text

            for line in fm_text.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key == "name":
                        meta["name"] = value
                    elif key == "description":
                        meta["description"] = value
                    elif key == "tags":
                        meta["tags"] = [tag.strip() for tag in value.split(",") if tag.strip()]

    return meta


def list_skills() -> list[dict[str, Any]]:
    ensure_skills_dir()
    skills: list[dict[str, Any]] = []
    for item in os.listdir(SKILLS_DIR):
        item_path = os.path.join(SKILLS_DIR, item)
        if os.path.isdir(item_path):
            skill_md = os.path.join(item_path, "SKILL.md")
            if os.path.exists(skill_md):
                parsed = parse_skill_md(skill_md)
                parsed["id"] = item
                parsed["path"] = skill_md
                skills.append(parsed)
    return skills


def get_skill_detail(skill_id: str) -> dict[str, Any] | None:
    try:
        validate_skill_id(skill_id)
    except ValueError:
        return None
    skill_md = os.path.join(SKILLS_DIR, skill_id, "SKILL.md")
    if os.path.exists(skill_md):
        parsed = parse_skill_md(skill_md)
        parsed["id"] = skill_id
        parsed["path"] = skill_md
        return parsed
    return None


def save_skill(
    skill_id: str,
    name: str,
    description: str,
    content: str,
    tags: list[str] | None = None,
) -> None:
    validate_skill_id(skill_id)
    ensure_skills_dir()
    target_dir = os.path.join(SKILLS_DIR, skill_id)
    os.makedirs(target_dir, exist_ok=True)
    skill_md = os.path.join(target_dir, "SKILL.md")

    tags_str = ", ".join(tags) if tags else ""
    full_text = f"""---
name: {name}
description: {description}
tags: {tags_str}
---

{content}
"""
    with open(skill_md, "w", encoding="utf-8") as f:
        f.write(full_text)


def init_default_skills() -> None:
    ensure_skills_dir()

    if not os.path.exists(os.path.join(SKILLS_DIR, "data_analysis", "SKILL.md")):
        save_skill(
            skill_id="data_analysis",
            name="数据深度分析与可视化",
            description="当用户需要统计分析数据、清洗 CSV/JSON、生成图表或撰写数据报告时激活此技能。",
            content="""# 数据分析规范指南

## 技能说明
当你面临数据分析、CSV处理或可视化任务时，请遵循以下规范：
1. **工作空间保护**：在执行前，先使用 `file_list` 确认输入数据文件的位置。所有生成的中间清洗文件与图表必须保存在 `./workspace` 中。
2. **标准环境**：使用 `python_eval` 或 `bash_run` 编写 Python 脚本，推荐使用内置的 `pandas`, `json`, `sqlite3` 等标准库或已安装库。
3. **图表输出**：如需输出图表，优先生成 SVG 矢量图或使用 `matplotlib` 保存为 PNG 到 `./workspace/chart.png`，并在最终回复中提供查看路径。
4. **统计摘要**：必须在回复中包含核心指标对比（均值、中位数、趋势变化），不得只贴代码。
""",
            tags=["data", "python", "visualization", "pandas"],
        )

    if not os.path.exists(os.path.join(SKILLS_DIR, "system_diagnostics", "SKILL.md")):
        save_skill(
            skill_id="system_diagnostics",
            name="Linux 生产环境健康诊断",
            description="当需要排查系统负载、内存泄漏、端口占用、日志排错或进程状态时激活此技能。",
            content="""# 系统健康诊断规范

## 技能说明
执行环境诊断命令时，必须恪守只读与最小影响原则：
1. **推荐安全命令**：
   - 查看负载与资源：`free -m`, `uptime`, `df -h`
   - 查看进程与端口：`ps aux --sort=-%mem | head -n 10`, `ss -tuln`
   - 查看最近系统日志：`dmesg | tail -n 20`
2. **高危拦截提醒**：
   - 严禁执行 `kill -9` 未知进程、`rm -rf /` 或重启网络服务的指令；
   - 任何涉及杀死进程或更改服务配置的操作必须触发权限审批卡片。
""",
            tags=["ops", "linux", "diagnostics", "performance"],
        )

    if not os.path.exists(os.path.join(SKILLS_DIR, "web_research", "SKILL.md")):
        save_skill(
            skill_id="web_research",
            name="自主网络调研与深度摘要",
            description="当用户需要抓取线上资讯、分析网页内容、整理技术文档或进行竞品调研时激活此技能。",
            content="""# 网络调研与深度摘要规范

## 技能说明
1. **抓取工具优先**：使用 `web_fetch` 工具获取网页 Markdown 或文本内容。
2. **内容提炼**：忽略页面冗余的导航栏、广告和页脚，提取核心正文、要点与数据指标。
3. **结构化产出**：若用户要求输出报告，将调研成果以 Markdown 格式保存到 `./workspace/research_report.md`，并在回答中展示摘要大纲。
""",
            tags=["web", "scraper", "research", "markdown"],
        )


if __name__ == "__main__":
    init_default_skills()
    print("Skills initialized. Found:", [skill["id"] for skill in list_skills()])
