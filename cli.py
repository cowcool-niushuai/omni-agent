#!/usr/bin/env python3
"""
OmniAgent CLI - Standalone Terminal Interactive Agent
Inspired by DeepSeek Harness (dsh), earendil-works/pi, and Claude Code / OpenCode.
"""
import asyncio
import os
import sys

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

import database
import permission
import skills_manager
from agent_core import agent_core
from mcp_manager import mcp_manager

console = Console()

BANNER = """
[bold #59D6B3]╭─────────────────────────────────────────────────────────────╮[/bold #59D6B3]
[bold #59D6B3]│[/bold #59D6B3]  [bold #15201D on #59D6B3] Ω OmniAgent [/bold #15201D on #59D6B3]  [bold #EDF2F0]Standalone Autonomous Terminal Agent[/bold #EDF2F0]       [bold #59D6B3]│[/bold #59D6B3]
[bold #59D6B3]│[/bold #59D6B3]  [dim]Harness: DeepSeek-V3 | ReAct | MCP | Skills | SubAgent[/dim]      [bold #59D6B3]│[/bold #59D6B3]
[bold #59D6B3]╰─────────────────────────────────────────────────────────────╯[/bold #59D6B3]
"""

def _render_event(event: dict) -> None:
    ev_type = event.get("type")

    if ev_type == "thought":
        thought_text = event.get("content", "")
        console.print(Panel(
            Text(thought_text, style="#8B9B97"),
            title="[dim]🧠 思考推理 (Reasoning)[/dim]",
            title_align="left",
            border_style="#24332F",
            padding=(0, 1)
        ))
    elif ev_type == "tool_start":
        fn = event.get("name")
        args = event.get("arguments", {})
        console.print(f"[bold #267F68]⚙ 工具调用:[/bold #267F68] [bold #59D6B3]{fn}[/bold #59D6B3] [dim]{args}[/dim]")
    elif ev_type == "subagent_start":
        role = event.get("role")
        task = event.get("task")
        console.print(Panel(
            f"[bold #59D6B3]指派子任务:[/bold #59D6B3] {task}",
            title=f"[bold #59D6B3]🤖 启动子智能体 (Sub-Agent): {role}[/bold #59D6B3]",
            title_align="left",
            border_style="#59D6B3",
            padding=(0, 1)
        ))
    elif ev_type == "subagent_done":
        role = event.get("role")
        res = event.get("result", "")
        console.print(Panel(
            Text(res[:500] + ("..." if len(res) > 500 else ""), style="#EDF2F0"),
            title=f"[bold #267F68]✅ 子智能体汇报就绪 ({role})[/bold #267F68]",
            title_align="left",
            border_style="#267F68",
            padding=(0, 1)
        ))
    elif ev_type == "tool_result":
        res = event.get("result", "")
        snippet = res[:300] + ("\n... (截断展示)" if len(res) > 300 else "")
        console.print(f"[dim green]↳ 返回结果:[/dim green] [dim]{snippet.strip()}[/dim]\n")
    elif ev_type == "compaction":
        console.print(
            f"[dim]🗜 已压缩 {event.get('dropped', 0)} 条较早消息"
            f"（摘要 {event.get('summary_chars', 0)} 字）[/dim]"
        )
    elif ev_type == "permission_intercept":
        requests = event.get("requests") or []
        lines = ["[bold yellow]警告：此操作需要人工审批[/bold yellow]", str(event.get("reason") or "")]
        for item in requests:
            lines.append(
                f"[bold]{item.get('tool_name')}[/bold] {item.get('arguments')}\n"
                f"[dim]{item.get('reason')}[/dim]"
            )
        console.print(Panel(
            "\n".join(lines) + f"\n请求ID: {event.get('request_id')}",
            title="[bold yellow]🛡️ 权限安全拦截[/bold yellow]",
            border_style="yellow",
        ))
    elif ev_type == "answer_chunk":
        print(event.get("content", ""), end="", flush=True)
    elif ev_type == "answer_discard":
        print()
    elif ev_type == "error":
        console.print(f"[bold red]错误:[/bold red] {event.get('message')}")
    elif ev_type == "done":
        print()


async def _consume_events(events) -> dict | None:
    """Render events and return the permission event, if the graph paused."""
    permission_event = None
    async for event in events:
        if event.get("type") == "permission_intercept":
            permission_event = event
            _render_event(event)
        else:
            _render_event(event)
    return permission_event


async def run_cli_stream(session_id: str, prompt: str):
    console.print(f"\n[bold #59D6B3]❯[/bold #59D6B3] [bold #EDF2F0]{prompt}[/bold #EDF2F0]\n")

    events = agent_core.run_react_stream(session_id, prompt)
    while True:
        permission_event = await _consume_events(events)
        if not permission_event:
            break

        request_id = str(permission_event.get("request_id", ""))
        answer = console.input(
            "[bold yellow]🛡️ 高危操作，是否允许执行？[y/N] [/bold yellow]"
        ).strip().lower()
        action = "APPROVE" if answer in {"y", "yes"} else "REJECT"
        request_ids = [
            str(item.get("request_id"))
            for item in permission_event.get("requests", [])
            if item.get("request_id")
        ]
        if not request_ids:
            request_ids = permission.pending_request_ids(session_id)
        if action == "APPROVE":
            permission.approve_requests(request_ids)
        else:
            permission.reject_requests(request_ids)

        console.print(f"[dim]已提交: {action}，继续执行…[/dim]")
        events = agent_core.resume_stream(session_id, request_id, action)

    print("\n")

def print_help():
    console.print("""
[bold]内置交互指令：[/bold]
  [#59D6B3]/skills[/]    查看当前已安装的技能库
  [#59D6B3]/mcp[/]       查看当前挂载的 MCP 服务器与工具
  [#59D6B3]/audit[/]     查看最近的工具执行审计记录
  [#59D6B3]/files[/]     列出 ./workspace 工作区文件
  [#59D6B3]/clear[/]     清空终端屏幕
  [#59D6B3]/help[/]      显示本帮助
  [#59D6B3]exit/quit[/]  退出终端智能体
""")

async def interactive_mode():
    console.print(BANNER)
    session_id = "cli_" + os.urandom(4).hex()
    database.init_db()
    skills_manager.init_default_skills()
    
    console.print(f"[dim]已激活会话: {session_id} | 工作区: ./workspace[/dim]")
    console.print("[dim]输入任意任务指令开始，或输入 /help 查看指令[/dim]\n")
    
    while True:
        try:
            prompt = console.input("[bold #59D6B3]OmniAgent ❯ [/bold #59D6B3]").strip()
            if not prompt:
                continue
            if prompt in ["exit", "quit", "q"]:
                console.print("[dim]会话已结束，再见！[/dim]")
                break
            elif prompt == "/clear":
                console.clear()
                console.print(BANNER)
                continue
            elif prompt == "/help":
                print_help()
                continue
            elif prompt == "/skills":
                skills = skills_manager.list_skills()
                for s in skills:
                    console.print(f"• [bold #59D6B3]{s['id']}[/bold #59D6B3]: {s['name']} - [dim]{s['description']}[/dim]")
                continue
            elif prompt == "/mcp":
                servers = mcp_manager.list_server_status()
                for s in servers:
                    console.print(f"• [bold #59D6B3]{s['name']}[/bold #59D6B3]: {s['tools_count']} tools [{', '.join(s['tools'])}]")
                continue
            elif prompt == "/audit":
                import audit
                rows = audit.list_tool_calls(session_id=session_id, limit=20)
                if not rows:
                    console.print("[dim]当前会话暂无审计记录。[/dim]")
                for row in rows:
                    console.print(
                        f"[dim]#{row['id']}[/dim] [bold #59D6B3]{row['tool_name']}[/bold #59D6B3] "
                        f"{row['risk_level']}/{row['decision']}/{row['status']} "
                        f"[dim]{row['duration_ms']}ms[/dim]"
                    )
                continue
            elif prompt == "/models":
                from model_manager import model_manager
                active = model_manager.get_active_model_info()
                console.print(f"[bold #59D6B3]当前激活模型:[/bold #59D6B3] {active['label']} ({active['provider_name']})")
                console.print("[dim]可用模型列表（输入 /model <id> 切换）：[/dim]")
                for m in model_manager.get_all_models():
                    mark = "✓" if m["is_active"] else " "
                    console.print(f" [{mark}] [#59D6B3]{m['id']}[/#59D6B3]: {m['label']} - [dim]{m['provider_name']}[/dim]")
                continue
            elif prompt.startswith("/model "):
                from model_manager import model_manager
                target_id = prompt.split(" ", 1)[1].strip()
                model_manager.set_active_model(target_id)
                new_active = model_manager.get_active_model_info()
                console.print(f"[bold green]✓ 已切换至模型:[/bold green] {new_active['label']} ({new_active['provider_name']})")
                continue
            elif prompt == "/files":
                import tools
                console.print(tools.tool_file_list("."))
                continue
                
            await run_cli_stream(session_id, prompt)
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]强制退出。[/dim]")
            break

def main():
    if len(sys.argv) > 1 and sys.argv[1] in {"--skills", "-s"}:
        skills_manager.init_default_skills()
        for skill in skills_manager.list_skills():
            console.print(
                f"• [bold #59D6B3]{skill['id']}[/bold #59D6B3]: "
                f"{skill['name']} - [dim]{skill['description']}[/dim]"
            )
        return

    if len(sys.argv) > 1 and sys.argv[1] == "--mcp":
        for server in mcp_manager.list_server_status():
            console.print(f"• [bold #59D6B3]{server.get('name', '?')}[/bold #59D6B3]: {server}")
        return

    if len(sys.argv) > 1:
        # One-shot command line mode: omni "task"
        task = " ".join(sys.argv[1:])
        session_id = "cli_oneshot_" + os.urandom(4).hex()
        database.init_db()
        skills_manager.init_default_skills()
        asyncio.run(run_cli_stream(session_id, task))
    else:
        # Interactive REPL mode
        asyncio.run(interactive_mode())

if __name__ == "__main__":
    main()
