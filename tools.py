import os
import subprocess
from pathlib import Path
from typing import Any

import database
import skills_manager
from mcp_manager import mcp_manager
from omni_agent.config import get_settings
from omni_agent.security import PathSecurityError, safe_join

_settings = get_settings()
WORKSPACE_PATH = Path(_settings.workspace_dir).resolve()
WORKSPACE_PATH.mkdir(parents=True, exist_ok=True)
WORKSPACE_ROOT = str(WORKSPACE_PATH)


def resolve_path(filepath: str) -> str:
    """Resolve a workspace-relative path and reject any escape attempt."""
    try:
        return str(safe_join(WORKSPACE_PATH, filepath))
    except PathSecurityError as exc:
        raise PathSecurityError(f"非法路径，已阻止越界访问: {filepath}") from exc


# Tool Implementations
def tool_file_list(directory: str = ".") -> str:
    try:
        target_dir = Path(resolve_path(directory))
    except PathSecurityError as exc:
        return str(exc)
    if not target_dir.exists():
        return f"目录不存在: {directory}"

    entries = []
    for root, dirs, files in os.walk(target_dir):
        rel_root = os.path.relpath(root, WORKSPACE_PATH)
        prefix = "" if rel_root == "." else rel_root + "/"
        for d in dirs:
            entries.append(f"[DIR]  {prefix}{d}")
        for f in files:
            size = os.path.getsize(os.path.join(root, f))
            entries.append(f"[FILE] {prefix}{f} ({size} bytes)")

    if not entries:
        return "工作区当前为空。你可以使用 file_write 创建文件。"
    return "\n".join(entries[:100])


def tool_file_read(filepath: str, start_line: int = 1, end_line: int = 200) -> str:
    try:
        path = Path(resolve_path(filepath))
    except PathSecurityError as exc:
        return str(exc)
    if not path.exists():
        return f"文件不存在: {filepath}"

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total = len(lines)
        start = max(1, start_line)
        end = min(total, end_line)

        output = [f"--- File: {os.path.relpath(path, WORKSPACE_PATH)} (Total {total} lines, showing {start}-{end}) ---"]
        for idx in range(start, end + 1):
            output.append(f"{idx:4d} | {lines[idx - 1].rstrip()}")
        return "\n".join(output)
    except Exception as e:
        return f"读取文件失败: {str(e)}"


def tool_file_write(filepath: str, content: str) -> str:
    try:
        path = Path(resolve_path(filepath))
    except PathSecurityError as exc:
        return str(exc)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"成功写入文件: {os.path.relpath(path, WORKSPACE_PATH)} ({len(content)} 字符)"
    except Exception as e:
        return f"写入文件失败: {str(e)}"


def tool_file_edit(filepath: str, target: str, replacement: str) -> str:
    try:
        path = Path(resolve_path(filepath))
    except PathSecurityError as exc:
        return str(exc)
    if not path.exists():
        return f"文件不存在: {filepath}"
    try:
        with open(path, encoding="utf-8") as f:
            old_content = f.read()
        if target not in old_content:
            return f"编辑失败：未在文件中找到匹配的目标内容 '{target}'"
        new_content = old_content.replace(target, replacement, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return f"成功局部修改文件: {os.path.relpath(path, WORKSPACE_PATH)}"
    except Exception as e:
        return f"文件编辑失败: {str(e)}"


def tool_bash_run(command: str) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKSPACE_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=25
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        code = result.returncode
        
        res = []
        if out:
            res.append(f"[STDOUT]\n{out[:4000]}")
        if err:
            res.append(f"[STDERR]\n{err[:2000]}")
        if not out and not err:
            res.append(f"[Exit code: {code} - 无输出]")
        return "\n".join(res)
    except subprocess.TimeoutExpired:
        return "命令执行超时 (限制 25 秒)"
    except Exception as e:
        return f"命令执行异常: {str(e)}"

def tool_python_eval(code: str) -> str:
    try:
        result = subprocess.run(
            [os.sys.executable, "-c", code],
            cwd=WORKSPACE_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=25
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        
        res = []
        if out:
            res.append(f"[Output]\n{out[:4000]}")
        if err:
            res.append(f"[Error]\n{err[:2000]}")
        if not out and not err:
            res.append("[Python 代码执行完成，无打印输出]")
        return "\n".join(res)
    except subprocess.TimeoutExpired:
        return "Python 代码执行超时 (25s)"
    except Exception as e:
        return f"Python 执行异常: {str(e)}"

def tool_web_fetch(url: str) -> str:
    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OmniAgent/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            # Clean HTML tags simply
            clean = raw
            import re
            clean = re.sub(r"<script.*?</script>", "", clean, flags=re.DOTALL | re.IGNORECASE)
            clean = re.sub(r"<style.*?</style>", "", clean, flags=re.DOTALL | re.IGNORECASE)
            clean = re.sub(r"<[^>]+>", " ", clean)
            clean = re.sub(r"\s+", " ", clean).strip()
            return f"--- Web Content from {url} ---\n{clean[:3000]}"
    except Exception as e:
        return f"抓取网页失败: {str(e)}"

def tool_rag_search(query: str, limit: int = 3) -> str:
    docs = database.search_knowledge(query, limit)
    if not docs:
        return f"未找到与 '{query}' 相关的本地知识库内容。"
    res = []
    for d in docs:
        res.append(f"【知识文档 #{d['id']}：{d['title']}】\n{d['content']}")
    return "\n\n".join(res)

def tool_read_skill_doc(skill_id: str) -> str:
    detail = skills_manager.get_skill_detail(skill_id)
    if not detail:
        skills = skills_manager.list_skills()
        available = ", ".join([s["id"] for s in skills])
        return f"未找到技能 '{skill_id}'。当前可用技能列表: [{available}]"
    return f"--- 技能说明书: {detail['name']} ({skill_id}) ---\n{detail['content']}"

# Base native tools definition for OpenAI function calling
NATIVE_TOOLS_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "bash_run",
            "description": "在专属工作区 (./workspace) 中执行真实的 Linux Shell 命令。可执行文件编译、运行脚本、git操作、查看系统等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 Shell 命令行。"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "读取工作区中的文件内容，支持分行切片展示。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "工作区内的文件路径（相对路径）。"},
                    "start_line": {"type": "integer", "description": "起始行号（默认 1）"},
                    "end_line": {"type": "integer", "description": "结束行号（默认 200）"}
                },
                "required": ["filepath"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": "在工作区创建新文件或覆盖写入完整文件内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "目标文件相对路径。"},
                    "content": {"type": "string", "description": "写入的文本代码或内容。"}
                },
                "required": ["filepath", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_edit",
            "description": "局部精确替换工作区指定文件中的一段文本（Target -> Replacement）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "目标文件路径。"},
                    "target": {"type": "string", "description": "文件中要被替换的精确原字符串。"},
                    "replacement": {"type": "string", "description": "替换后的新字符串。"}
                },
                "required": ["filepath", "target", "replacement"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_list",
            "description": "列出工作区中的目录结构与所有文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "相对工作区的子目录路径，默认 '.'"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "python_eval",
            "description": "在独立 Python 沙箱环境中执行 Python 代码并返回运行输出。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要运行的完整 Python 脚本代码。"}
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "获取指定 URL 的网页文本内容，自动清洗脚本与标签。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整的 HTTP/HTTPS 网址。"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": "检索企业内部持久化知识库（RAG 增强），查询架构设计、规范要求或业务文档。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词或查询问题。"},
                    "limit": {"type": "integer", "description": "返回匹配条数，默认 3"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill_doc",
            "description": "当遇到专业场景（如数据分析、系统排错、竞品调研）时，读取对应技能的详细操作规范 (SKILL.md)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string", "description": "技能唯一标识名（如 'data_analysis', 'system_diagnostics', 'web_research'）"}
                },
                "required": ["skill_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_subagent",
            "description": "【DeerFlow 架构核心】派发独立的专业子智能体 (Sub-Agent) 专项处理复杂子任务（如专项技术调研、独立写模块代码、做数据报表），返回子智能体的执行成果总结。",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": "子智能体的专业角色，例如：'代码研发专家', '技术深度调研员', '架构安全审计师'。"
                    },
                    "task": {
                        "type": "string",
                        "description": "明确清晰的子任务目标与交付要求。"
                    }
                },
                "required": ["role", "task"]
            }
        }
    }
]

def dispatch_tool(tool_name: str, arguments: dict[str, Any]) -> str:
    """
    Executes a native tool or routes to an MCP Server.
    """
    # 1. Native tools
    if tool_name == "bash_run":
        return tool_bash_run(arguments.get("command", ""))
    elif tool_name == "file_read":
        return tool_file_read(
            arguments.get("filepath", ""),
            int(arguments.get("start_line", 1)),
            int(arguments.get("end_line", 200))
        )
    elif tool_name == "file_write":
        return tool_file_write(arguments.get("filepath", ""), arguments.get("content", ""))
    elif tool_name == "file_edit":
        return tool_file_edit(arguments.get("filepath", ""), arguments.get("target", ""), arguments.get("replacement", ""))
    elif tool_name == "file_list":
        return tool_file_list(arguments.get("directory", "."))
    elif tool_name == "python_eval":
        return tool_python_eval(arguments.get("code", ""))
    elif tool_name == "web_fetch":
        return tool_web_fetch(arguments.get("url", ""))
    elif tool_name == "rag_search":
        return tool_rag_search(arguments.get("query", ""), int(arguments.get("limit", 3)))
    elif tool_name == "read_skill_doc":
        return tool_read_skill_doc(arguments.get("skill_id", ""))
    
    # 2. MCP Tools
    if tool_name.startswith("mcp_"):
        return mcp_manager.call_mcp_tool(tool_name, arguments)
    
    return f"未知工具: {tool_name}"

def get_all_tool_definitions() -> list[dict[str, Any]]:
    """
    Returns native tools + dynamically registered MCP tools.
    """
    mcp_tools = mcp_manager.get_all_mcp_tools()
    # Strip internal keys
    clean_mcp = [{"type": "function", "function": t["function"]} for t in mcp_tools]
    return NATIVE_TOOLS_DEFINITIONS + clean_mcp
