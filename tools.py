"""Native tool implementations.

Phase 2 routes every command through the shared policy engine and the sandbox
runner: no shell, scrubbed environment, resource limits, output caps and
optional bubblewrap isolation. Network fetching is protected against SSRF.
"""

from __future__ import annotations

import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

import database
import search
import skills_manager
from browser import browser_manager
from mcp_manager import mcp_manager
from omni_agent import tenancy
from omni_agent.config import get_settings
from omni_agent.runtime import planning
from omni_agent.security import PathSecurityError, safe_join
from omni_agent.security.commands import DENY as COMMAND_DENY
from omni_agent.security.commands import assess_command
from omni_agent.security.limits import truncate_text
from omni_agent.security.sandbox import run_command
from omni_agent.security.ssrf import (
    UnsafeURLError,
    is_public_ip,
    resolve_and_validate,
    validate_url_static,
)

_settings = get_settings()


def _workspace_path() -> Path:
    """Current project workspace, resolved for path containment checks."""
    path = tenancy.current_workspace().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path

_REDIRECT_STATUS = {301, 302, 303, 307, 308}


def resolve_path(filepath: str) -> str:
    """Resolve a workspace-relative path and reject any escape attempt."""
    try:
        return str(safe_join(_workspace_path(), filepath))
    except PathSecurityError as exc:
        raise PathSecurityError(f"非法路径，已阻止越界访问: {filepath}") from exc


def _settings_snapshot() -> Any:
    return get_settings()


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def tool_file_list(directory: str = ".") -> str:
    try:
        target_dir = Path(resolve_path(directory))
    except PathSecurityError as exc:
        return str(exc)
    if not target_dir.exists():
        return f"目录不存在: {directory}"

    entries: list[str] = []
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [entry for entry in dirs if entry not in {".home", ".tmp"}]
        rel_root = os.path.relpath(root, _workspace_path())
        prefix = "" if rel_root == "." else rel_root + "/"
        for entry in dirs:
            entries.append(f"[DIR]  {prefix}{entry}")
        for entry in files:
            size = os.path.getsize(os.path.join(root, entry))
            entries.append(f"[FILE] {prefix}{entry} ({size} bytes)")
        if len(entries) >= 500:
            break

    if not entries:
        return "工作区当前为空。你可以使用 file_write 创建文件。"
    text = "\n".join(entries[:500])
    if len(entries) > 500:
        text += "\n... [目录条目过多，已截断]"
    return truncate_text(text, _settings.tool_max_output_chars)


def tool_file_read(filepath: str, start_line: int = 1, end_line: int = 200) -> str:
    try:
        path = Path(resolve_path(filepath))
    except PathSecurityError as exc:
        return str(exc)
    if not path.exists() or path.is_dir():
        return f"文件不存在: {filepath}"

    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()

        total = len(lines)
        start = max(1, int(start_line))
        end = min(total, int(end_line), start + 2000)
        if end < start:
            return f"行号范围无效: {start}-{end_line}"

        output = [
            f"--- File: {os.path.relpath(path, _workspace_path())} "
            f"(Total {total} lines, showing {start}-{end}) ---"
        ]
        for index in range(start, end + 1):
            output.append(f"{index:4d} | {lines[index - 1].rstrip()}")
        return truncate_text("\n".join(output), _settings.tool_max_output_chars)
    except Exception as exc:
        return f"读取文件失败: {exc}"


def _check_write_size(size: int) -> str | None:
    if size > _settings.max_file_write_bytes:
        limit_mb = _settings.max_file_write_bytes // (1024 * 1024)
        return f"写入内容过大（{size} bytes，上限 {limit_mb} MB）"
    return None


def tool_file_write(filepath: str, content: str) -> str:
    try:
        path = Path(resolve_path(filepath))
    except PathSecurityError as exc:
        return str(exc)
    error = _check_write_size(len(content.encode("utf-8")))
    if error:
        return error
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return f"成功写入文件: {os.path.relpath(path, _workspace_path())} ({len(content)} 字符)"
    except Exception as exc:
        return f"写入文件失败: {exc}"


def tool_file_edit(filepath: str, target: str, replacement: str) -> str:
    try:
        path = Path(resolve_path(filepath))
    except PathSecurityError as exc:
        return str(exc)
    if not path.exists():
        return f"文件不存在: {filepath}"
    if not target:
        return "编辑失败：target 不能为空"
    try:
        with open(path, encoding="utf-8") as handle:
            old_content = handle.read()
        if target not in old_content:
            return f"编辑失败：未在文件中找到匹配的目标内容 '{target}'"
        new_content = old_content.replace(target, replacement, 1)
        error = _check_write_size(len(new_content.encode("utf-8")))
        if error:
            return error
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(new_content)
        return f"成功局部修改文件: {os.path.relpath(path, _workspace_path())}"
    except Exception as exc:
        return f"文件编辑失败: {exc}"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _format_command_result(
    *,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    timed_out: bool,
    truncated: bool,
    duration_ms: int,
    timeout: float,
    backend: str,
    prefix: str = "STDOUT",
) -> str:
    if timed_out:
        return f"命令执行超时（限制 {timeout:g} 秒，沙箱: {backend}）"
    parts: list[str] = []
    if stdout.strip():
        parts.append(f"[{prefix}]\n{stdout.strip()}")
    if stderr.strip():
        parts.append(f"[STDERR]\n{stderr.strip()}")
    if not parts:
        parts.append(f"[Exit code: {exit_code} - 无输出]")
    elif exit_code not in (0, None):
        parts.append(f"[Exit code: {exit_code}]")
    if truncated:
        parts.append("[输出超过上限，已截断]")
    parts.append(f"[耗时 {duration_ms} ms | 沙箱 {backend}]")
    return truncate_text("\n".join(parts), _settings.tool_max_output_chars)


def tool_bash_run(command: str) -> str:
    """Run a single command (no shell) inside the sandbox.

    DENY decisions are enforced here as a last line of defence. ASK_USER calls
    reach this function only after the runtime's approval node or the
    sub-agent policy explicitly allowed them.
    """
    assessment = assess_command(
        command,
        _workspace_path(),
        policy_mode=_settings.command_policy_mode,
    )
    if assessment.risk == COMMAND_DENY:
        return f"[安全策略拒绝] {assessment.reason}"

    result = run_command(
        assessment.argv,
        cwd=_workspace_path(),
        timeout=_settings.command_timeout_seconds,
        backend=_settings.sandbox_backend,
        network=_settings.sandbox_network,
        memory_mb=_settings.command_max_memory_mb,
        cpu_seconds=_settings.command_max_cpu_seconds,
        file_mb=_settings.command_max_file_mb,
        max_output_bytes=_settings.command_max_output_bytes,
        extra_ro_binds=_settings.sandbox_ro_bind_list,
    )
    return _format_command_result(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        truncated=result.truncated,
        duration_ms=result.duration_ms,
        timeout=_settings.command_timeout_seconds,
        backend=result.backend,
    )


def tool_python_eval(code: str) -> str:
    result = run_command(
        [sys.executable, "-c", code],
        cwd=_workspace_path(),
        timeout=_settings.python_eval_timeout_seconds,
        backend=_settings.sandbox_backend,
        network=_settings.sandbox_network,
        memory_mb=_settings.command_max_memory_mb,
        cpu_seconds=_settings.command_max_cpu_seconds,
        file_mb=_settings.command_max_file_mb,
        max_output_bytes=_settings.command_max_output_bytes,
        extra_ro_binds=_settings.sandbox_ro_bind_list,
    )
    return _format_command_result(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        truncated=result.truncated,
        duration_ms=result.duration_ms,
        timeout=_settings.python_eval_timeout_seconds,
        backend=result.backend,
        prefix="Output",
    )


# ---------------------------------------------------------------------------
# Web fetch
# ---------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    """Small, dependency-free HTML-to-text converter."""

    _BLOCK_TAGS = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "header",
        "footer",
        "pre",
        "blockquote",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", "".join(self._parts))).strip()


def _html_to_text(raw: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(raw)
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw)
    return parser.text()


def _peer_address(response: httpx.Response) -> str | None:
    """Return the actual peer IP when the transport exposes it."""
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    try:
        address = stream.get_extra_info("server_addr")
    except Exception:
        return None
    if not address:
        return None
    return str(address[0])


def tool_web_fetch(url: str) -> str:
    settings = _settings_snapshot()
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) OmniAgent/0.1",
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.5",
    }
    current = url
    try:
        with httpx.Client(
            timeout=settings.web_fetch_timeout_seconds,
            follow_redirects=False,
            headers=headers,
            trust_env=False,
        ) as client:
            for _ in range(settings.web_fetch_max_redirects + 1):
                validated = validate_url_static(
                    current,
                    allowed_ports=settings.web_fetch_port_list,
                    allow_private=settings.allow_private_network,
                )
                resolve_and_validate(
                    validated, allow_private=settings.allow_private_network
                )
                with client.stream("GET", current) as response:
                    if response.status_code in _REDIRECT_STATUS:
                        location = response.headers.get("location")
                        if not location:
                            return f"抓取网页失败: {response.status_code} 重定向缺少 Location"
                        current = urljoin(str(response.url), location)
                        continue

                    peer = _peer_address(response)
                    if not settings.allow_private_network and peer and not is_public_ip(peer):
                        return f"网页抓取被安全策略阻止: 实际连接地址为非公网 IP {peer}"

                    content_type = response.headers.get("content-type", "").lower()
                    if content_type and not any(
                        marker in content_type
                        for marker in (
                            "text/",
                            "json",
                            "xml",
                            "javascript",
                            "x-www-form-urlencoded",
                        )
                    ):
                        return f"抓取网页失败: 不支持的内容类型 {content_type}"

                    raw = bytearray()
                    truncated = False
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) >= settings.web_fetch_max_bytes:
                            truncated = True
                            break
                    encoding = response.encoding or "utf-8"
                    text = raw.decode(encoding, errors="replace")
                    is_html = "html" in content_type or "<html" in text[:500].lower()
                    clean = _html_to_text(text) if is_html else text
                    clean = re.sub(r"\s+", " ", clean).strip() if not is_html else clean
                    if truncated:
                        clean += "\n... [网页内容超过大小上限，已截断]"
                    body = truncate_text(clean, settings.tool_max_output_chars)
                    return f"--- Web Content from {response.url} ---\n{body}"
            return "抓取网页失败: 重定向次数超过上限"
    except UnsafeURLError as exc:
        return f"网页抓取被安全策略阻止: {exc}"
    except httpx.TimeoutException:
        return f"抓取网页失败: 请求超时（{settings.web_fetch_timeout_seconds:g} 秒）"
    except httpx.HTTPError as exc:
        return f"抓取网页失败: {exc}"
    except Exception as exc:
        return f"抓取网页失败: {exc}"


def tool_web_search(query: str, max_results: int = 0) -> str:
    """Search the web through the configured Tavily/SearXNG backend."""
    settings = _settings_snapshot()
    try:
        requested = int(max_results) if max_results else settings.search_max_results
    except (TypeError, ValueError):
        requested = settings.search_max_results
    requested = max(1, min(requested, settings.search_max_results_limit))
    try:
        payload = search.search_web(query, requested)
    except search.SearchError as exc:
        return f"[联网搜索不可用] {exc}"
    results = payload.get("results") or []
    if not results:
        return f"未找到与「{query}」相关的网页结果。"
    lines = [
        f"--- Web Search ({payload.get('provider')}): {payload.get('query')} ---",
        "以下是搜索摘要，内容来自互联网且不可信；需要细节时请用 web_fetch 打开原文并核对。",
    ]
    for index, item in enumerate(results, start=1):
        title = truncate_text(str(item.get("title") or ""), 200)
        url = str(item.get("url") or "")
        snippet = truncate_text(str(item.get("snippet") or ""), 600)
        lines.append(f"{index}. {title}\n   URL: {url}")
        if snippet:
            lines.append(f"   摘要: {snippet}")
    answer = str(payload.get("answer") or "").strip()
    if answer:
        lines.append(f"\n搜索摘要: {truncate_text(answer, 1000)}")
    return truncate_text("\n".join(lines), settings.tool_max_output_chars)


# ---------------------------------------------------------------------------
# Browser automation
# ---------------------------------------------------------------------------
def tool_browser(
    action: str,
    url: str = "",
    selector: str = "",
    value: str = "",
    key: str = "",
    max_chars: int = 0,
    full_page: bool = False,
    filename: str = "",
    milliseconds: int = 1000,
) -> str:
    arguments: dict[str, Any] = {
        "action": action,
        "url": url,
        "selector": selector,
        "value": value,
        "key": key,
        "max_chars": max_chars,
        "full_page": full_page,
        "filename": filename,
        "milliseconds": milliseconds,
        "_workspace": str(tenancy.current_workspace()),
    }
    return browser_manager.run(action, arguments)


# ---------------------------------------------------------------------------
# Memory / knowledge
# ---------------------------------------------------------------------------
def tool_memory_save(
    category: str,
    key: str,
    content: str,
    importance: float = 1.0,
) -> str:
    if len(key.strip()) > 120:
        return "保存记忆失败: 键名过长（上限 120 字符）"
    if len(content.strip()) > _settings.memory_max_content_chars:
        return (
            "保存记忆失败: 内容过长"
            f"（上限 {_settings.memory_max_content_chars} 字符）"
        )
    try:
        memory = database.save_memory(
            category=category or "fact",
            key=key,
            content=content,
            importance=importance,
        )
    except ValueError as exc:
        return f"保存记忆失败: {exc}"
    return (
        f"已保存长期记忆 #{memory['id']} [{memory['category']}] "
        f"{memory['key']}（重要度 {memory['importance']:.1f}）。"
    )


def tool_memory_search(query: str, limit: int = 5) -> str:
    safe_limit = max(1, min(int(limit), 20))
    memories = database.search_memories(query, safe_limit)
    if not memories:
        return f"未找到与 '{query}' 相关的长期记忆。"
    lines = [
        f"- [#{item['id']}] [{item['category']}] {item['key']}: {item['content']}"
        for item in memories
    ]
    return truncate_text("\n".join(lines), _settings.tool_max_output_chars)


def tool_knowledge_add(title: str, content: str, tags: str = "") -> str:
    if len(title.strip()) > 200:
        return "写入知识库失败: 标题过长（上限 200 字符）"
    if len(content.strip()) > _settings.knowledge_max_content_chars:
        return (
            "写入知识库失败: 正文过长"
            f"（上限 {_settings.knowledge_max_content_chars} 字符）"
        )
    try:
        doc = database.add_knowledge(title=title, content=content, tags=tags)
    except ValueError as exc:
        return f"写入知识库失败: {exc}"
    return f"已写入知识库文档 #{doc['id']}：{doc['title']}（标签: {doc['tags'] or '无'}）。"


def tool_rag_search(query: str, limit: int = 3) -> str:
    safe_limit = max(1, min(int(limit), 10))
    docs = database.search_knowledge(query, safe_limit)
    if not docs:
        return f"未找到与 '{query}' 相关的本地知识库内容。"
    blocks = [
        f"【知识文档 #{doc['id']}：{doc['title']}】\n{doc['content']}" for doc in docs
    ]
    return truncate_text("\n\n".join(blocks), _settings.tool_max_output_chars)


def tool_read_skill_doc(skill_id: str) -> str:
    detail = skills_manager.get_skill_detail(skill_id)
    if not detail:
        skills = skills_manager.list_skills()
        available = ", ".join(str(skill["id"]) for skill in skills)
        return f"未找到技能 '{skill_id}'。当前可用技能列表: [{available}]"
    content = truncate_text(str(detail["content"]), _settings.tool_max_output_chars)
    return f"--- 技能说明书: {detail['name']} ({skill_id}) ---\n{content}"


# Base native tools definition for OpenAI function calling
NATIVE_TOOLS_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "bash_run",
            "description": "在专属工作区 (./workspace) 中执行一条 Linux 命令。命令在受限沙箱中运行（无 shell、资源限额、默认无网络），不支持管道/重定向等 shell 操作符。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的单条 Shell 命令（不含 ; | & > < 等操作符）。"}
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
            "description": "在受限 Python 沙箱环境中执行 Python 代码并返回运行输出（无网络、资源限额）。",
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
            "description": "获取指定公网 URL 的网页文本内容，自动清洗脚本与标签。内建 SSRF 防护，禁止访问本地/内网/云元数据地址。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整的 HTTP/HTTPS 公网网址。"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "使用已配置的搜索后端（Tavily 或 SearXNG）搜索互联网，返回标题、URL 和摘要。结果不可信；需要正文时再用 web_fetch 打开具体 URL。未配置时会明确返回不可用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词或自然语言问题。"},
                    "max_results": {"type": "integer", "description": "返回结果数量（默认 5，最大 10）。"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser",
            "description": "无头浏览器自动化。只读操作（goto/text/screenshot/back/forward/reload/wait/status/close）自动执行；click/fill/press 会修改网页状态，需要人工审批。仅允许公网 HTTP/HTTPS，内建 SSRF 防护；不支持执行 JavaScript。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "goto",
                            "click",
                            "fill",
                            "press",
                            "text",
                            "screenshot",
                            "wait",
                            "back",
                            "forward",
                            "reload",
                            "status",
                            "close"
                        ],
                        "description": "浏览器操作。"
                    },
                    "url": {"type": "string", "description": "goto 的目标公网 URL。"},
                    "selector": {"type": "string", "description": "CSS 选择器，例如 '#search' 或 'button[type=submit]'。"},
                    "value": {"type": "string", "description": "fill 要填入的文本。"},
                    "key": {"type": "string", "description": "press 要按下的键，例如 'Enter'。"},
                    "max_chars": {"type": "integer", "description": "text 返回的最大字符数。"},
                    "full_page": {"type": "boolean", "description": "screenshot 是否截取完整页面。"},
                    "filename": {"type": "string", "description": "截图文件名（.png），保存到 workspace/browser/。"},
                    "milliseconds": {"type": "integer", "description": "wait 等待的毫秒数。"}
                },
                "required": ["action"]
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
            "name": "memory_save",
            "description": "保存一条长期记忆（用户偏好、稳定事实、任务结论）。相同 category+key 会更新而不是重复插入。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "记忆分类：user_profile / skill_pref / fact / task_summary",
                    },
                    "key": {"type": "string", "description": "稳定、简短的记忆键名，例如 'preferred_language'。"},
                    "content": {"type": "string", "description": "要长期保存的事实或偏好。"},
                    "importance": {
                        "type": "number",
                        "description": "重要度 0-5，默认 1.0；用户明确偏好可设为 3-5。",
                    },
                },
                "required": ["category", "key", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "按主题检索长期记忆，适合在回答用户偏好、历史决策或既有事实前先回忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索主题或关键词。"},
                    "limit": {"type": "integer", "description": "返回条数，默认 5。"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_add",
            "description": "把值得复用的资料写入本地知识库（RAG），之后可用 rag_search 检索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "知识文档标题。"},
                    "content": {"type": "string", "description": "知识文档正文。"},
                    "tags": {"type": "string", "description": "逗号分隔的标签，便于检索。"},
                },
                "required": ["title", "content"],
            },
        },
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
            "name": "update_plan",
            "description": "创建或更新本轮任务计划。多步骤任务必须先调用本工具；每完成一步就更新状态与验证证据 (evidence)。完成标准写入 done_when，completed 步骤必须提供 evidence。",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "本轮任务的明确目标。"},
                    "done_when": {"type": "string", "description": "可验证的完成标准，例如 pytest 通过、文件存在、接口返回 200。"},
                    "steps": {
                        "type": "array",
                        "description": "计划步骤，按执行顺序排列；完成后更新状态和证据。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "稳定步骤 ID，省略时自动生成 step-N。"},
                                "title": {"type": "string", "description": "步骤标题。"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "blocked"]},
                                "acceptance": {"type": "string", "description": "该步骤的验收条件。"},
                                "evidence": {"type": "string", "description": "完成证据：测试输出、命令退出码、文件路径、接口响应或来源。"}
                            },
                            "required": ["title", "status"]
                        }
                    }
                },
                "required": ["goal", "steps"]
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


def tool_update_plan(
    goal: str, done_when: str = "", steps: list[dict[str, Any]] | None = None
) -> str:
    plan = planning.normalize_plan(
        {"goal": goal, "done_when": done_when, "steps": steps or []}
    )
    if not plan["goal"]:
        return "[参数错误] 计划目标不能为空。"
    if not plan["steps"]:
        return "[参数错误] 计划至少需要一个步骤。"
    return "计划已更新：\n" + json.dumps(plan, ensure_ascii=False, indent=2)


def dispatch_tool(tool_name: str, arguments: dict[str, Any]) -> str:
    """Execute a native tool or route to an MCP server."""
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
    elif tool_name == "web_search":
        return tool_web_search(
            str(arguments.get("query", "")),
            int(arguments.get("max_results", 0) or 0),
        )
    elif tool_name == "browser":
        return tool_browser(
            str(arguments.get("action", "")),
            str(arguments.get("url", "")),
            str(arguments.get("selector", "")),
            str(arguments.get("value", "")),
            str(arguments.get("key", "")),
            int(arguments.get("max_chars", 0) or 0),
            bool(arguments.get("full_page", False)),
            str(arguments.get("filename", "")),
            int(arguments.get("milliseconds", 1000) or 1000),
        )
    elif tool_name == "rag_search":
        return tool_rag_search(
            arguments.get("query", ""),
            int(arguments.get("limit", _settings.knowledge_search_limit)),
        )
    elif tool_name == "memory_save":
        return tool_memory_save(
            str(arguments.get("category", "fact")),
            str(arguments.get("key", "")),
            str(arguments.get("content", "")),
            float(arguments.get("importance", 1.0)),
        )
    elif tool_name == "memory_search":
        return tool_memory_search(
            str(arguments.get("query", "")),
            int(arguments.get("limit", _settings.memory_search_limit)),
        )
    elif tool_name == "knowledge_add":
        return tool_knowledge_add(
            str(arguments.get("title", "")),
            str(arguments.get("content", "")),
            str(arguments.get("tags", "")),
        )
    elif tool_name == "read_skill_doc":
        return tool_read_skill_doc(arguments.get("skill_id", ""))
    elif tool_name == "update_plan":
        return tool_update_plan(
            str(arguments.get("goal", "")),
            str(arguments.get("done_when", "")),
            arguments.get("steps") or [],
        )

    # MCP Tools
    if tool_name.startswith("mcp_"):
        return mcp_manager.call_mcp_tool(tool_name, arguments)

    return f"未知工具: {tool_name}"


def get_all_tool_definitions() -> list[dict[str, Any]]:
    """Return native tools plus dynamically registered MCP tools."""
    mcp_tools = mcp_manager.get_all_mcp_tools()
    clean_mcp = [{"type": "function", "function": item["function"]} for item in mcp_tools]
    return NATIVE_TOOLS_DEFINITIONS + clean_mcp
