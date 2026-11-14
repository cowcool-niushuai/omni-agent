# OmniAgent

> 一个可本地运行、可扩展、可恢复的开源 AI Agent 应用。
> Web UI + CLI + 真实工具执行 + MCP + Skills + Human-in-the-loop 审批。

OmniAgent 是一个**单机单用户**的通用智能体应用：它不只回答问题，还能在工作区里读写文件、执行命令、运行 Python、调用 MCP 工具，并在高危操作前暂停，等待你批准后继续执行。

项目当前处于 **Phase 1（Agent 内核可信化）完成** 的状态，目标是成为一个可靠、可审计、可打包分发的开源 Agent 应用。

---

## 核心能力

### 1. 真正的流式 Agent 内核

基于 **LangGraph 显式 StateGraph**，而不是黑盒 Agent：

```text
START -> agent -> (需要审批?) -> approval -> tools -> agent -> ... -> END
```

- 模型 token 通过 SSE 逐字返回，不再使用“完整回复后切片”的假流式
- 工具调用、工具结果、思考摘要、子代理进度都实时推送到前端
- 同一会话内的独立工具调用并行执行
- 每一步状态写入 SQLite checkpoint，进程重启后仍可恢复

### 2. Human-in-the-loop 审批，可以真正恢复执行

高危命令不会只弹一个“已拦截”的提示：

1. `approval` 节点调用 LangGraph `interrupt()` 暂停图
2. 前端展示审批卡片，Web 与 CLI 都支持批准 / 拒绝
3. 批准后通过 `Command(resume=...)` 从断点继续
4. 工具真正执行，结果进入上下文，Agent 继续推理

审批请求 ID 是确定性的，节点重放不会产生重复记录。

### 3. 上下文压缩与断点续跑

- 使用 `tiktoken` 估算 token，超过预算后自动把较早消息压缩成摘要
- 最近消息保留原文，避免丢失当前任务细节
- checkpoint 保存完整消息、工具调用和审批状态
- 同一会话重复打开、服务重启后仍能继续

### 4. 工具、MCP 与 Skills

原生工具：

- `file_list` / `file_read` / `file_write` / `file_edit`
- `bash_run` / `python_eval`
- `web_fetch`
- `rag_search` / `read_skill_doc`
- `spawn_subagent`（隔离子智能体）

扩展能力：

- **MCP**：通过 stdio JSON-RPC 挂载外部 MCP Server，动态发现工具
- **Skills**：扫描 `skills/*/SKILL.md`，按需注入技能规范
- **子代理**：主 Agent 可以把独立子任务委派给专职子 Agent

### 5. 权限策略

四级风险模型：

| 级别 | 行为 |
|---|---|
| `ALLOW` | 只读、安全操作，自动执行 |
| `SAFE_EXEC` | 工作区文件操作、常规命令，执行并记录 |
| `ASK_USER` | `rm`、`kill`、`chmod`、`sudo` 等高危命令，暂停等待审批 |
| `DENY` | 系统目录、非只读 SQL 等，直接阻断 |

> 注意：当前命令策略仍是**黑名单 + 审批**，不是操作系统级沙箱。真正的进程/容器隔离、SSRF 防护、资源配额属于 Phase 2。

### 6. 会话、记忆与知识库

- 会话与消息持久化到 SQLite
- 长期记忆（memories）注入系统提示词
- 知识库检索作为 Agent 工具使用，而不是把 RAG 当成唯一能力
- 工作区文件可在 Web UI 中浏览和预览

---

## 架构

```text
┌──────────────────────────────┐
│  Web UI (static/index.html)  │
│  CLI (cli.py / ./omni)       │
└──────────────┬───────────────┘
               │ HTTP + SSE
┌──────────────▼───────────────┐
│  FastAPI (server.py)         │
│  /api/chat/stream            │
│  /api/chat/resume            │
│  /api/permission/confirm     │
└──────────────┬───────────────┘
               │
┌──────────────▼────────────────────────────┐
│  AgentRuntime (LangGraph StateGraph)      │
│  agent -> approval -> tools -> agent      │
│  AsyncSqliteSaver checkpoint              │
└──────┬───────────────┬────────────────────┘
       │               │
┌──────▼──────┐  ┌─────▼─────────────────────┐
│ Model APIs  │  │ Tools / MCP / Skills      │
│ DeepSeek... │  │ permission policy         │
└─────────────┘  └───────────────────────────┘
```

---

## 目录结构

```text
omni-agent/
├── agent_core.py              # 兼容 facade，转发到 LangGraph runtime
├── omni_agent/
│   ├── config.py              # pydantic-settings 配置
│   ├── logging_config.py      # structlog
│   ├── security/paths.py      # 路径越界防护
│   └── runtime/
│       ├── graph.py           # LangGraph StateGraph 与 AgentRuntime
│       └── context.py         # token 计数与上下文压缩
├── permission.py              # 风险分类、审批请求、批量状态更新
├── tools.py                   # 原生工具与 MCP 工具分发
├── prompts.py                 # 动态系统提示词
├── database.py                # SQLite 持久化
├── model_manager.py           # 多 provider 模型管理（密钥只从环境变量读取）
├── mcp_manager.py             # MCP stdio 客户端
├── skills_manager.py          # Skills 扫描与写入
├── server.py                  # FastAPI 入口与 SSE
├── cli.py                     # Rich CLI / 审批交互
├── static/index.html          # Web 控制台
├── tests/                     # pytest
├── workspace/                 # Agent 工作区（不入库）
├── data/                      # checkpoint 等运行数据（不入库）
└── mcp_servers/sqlite_mcp.py  # 示例 MCP Server
```

---

## 快速开始

要求 Python 3.12 与 [uv](https://docs.astral.sh/uv/)。

```bash
cp .env.example .env
# 编辑 .env，填入至少一个 provider 的 key，例如：
# DEEPSEEK_API_KEY=sk-...

uv sync --all-groups
uv run uvicorn server:app --host 127.0.0.1 --port 8001 --reload
```

打开 `http://127.0.0.1:8001`。

也可以直接用 CLI：

```bash
./omni "分析 workspace 下的数据并生成报告"
./omni --skills
./omni --mcp
```

CLI 遇到高危操作会询问 `y/N`，批准后会从 checkpoint 继续执行，而不是重新开始任务。

### 常用命令

```bash
make test       # pytest
make lint       # ruff
make typecheck  # mypy
make check      # 全部检查
```

---

## 配置

主要环境变量：

| 变量 | 说明 | 默认 |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API Key | 无 |
| `OPENAI_API_KEY` | OpenAI API Key | 无 |
| `ANTHROPIC_API_KEY` | Anthropic API Key | 无 |
| `HOST` | 监听地址 | `127.0.0.1` |
| `PORT` | 监听端口 | `8001` |
| `WORKSPACE_DIR` | 工作区目录 | `./workspace` |
| `DATA_DIR` | checkpoint 等数据目录 | `./data` |
| `CONTEXT_MAX_TOKENS` | 上下文压缩阈值 | `24000` |
| `CONTEXT_KEEP_LAST` | 压缩时保留的最近消息数 | `12` |
| `MAX_ITERATIONS` | 单轮最大推理步数 | `8` |

模型 provider 与模型列表在 `model_config.json` 中管理；**API Key 不会写入该文件**，只从环境变量读取。可参考 `model_config.example.json`。

---

## 安全约定

- `.env`、`model_config.json`、`*.db`、`workspace/`、`data/` 均不入库
- 密钥只从环境变量读取；`.env` 权限应为 `600`
- 默认只监听 `127.0.0.1`，不对外暴露
- 文件路径统一经过 `safe_join`，阻止 `../` 与符号链接逃逸
- 高危命令通过审批暂停，但**不是沙箱**；不要把不可信任务直接放到生产主机上运行

如果曾经把 API Key 提交到源码、日志或截图，请立即到 provider 控制台轮换。

---

## 测试

```bash
uv run pytest -q
uv run ruff check .
uv run mypy
```

当前结果：

- `pytest`：28 passed
- `ruff`：All checks passed
- `mypy`：Success，8 source files

测试覆盖包括：路径穿越、密钥不泄露、技能 ID 校验、API 冒烟、真流式、工具调用、审批批准/拒绝、checkpoint 恢复、上下文压缩。

---

## 路线图

| 阶段 | 状态 | 内容 |
|---|---|---|
| Phase 0 | ✅ 完成 | 安全止血与工程底座：路径安全、密钥治理、日志、测试、CI |
| Phase 1 | ✅ 完成 | LangGraph 内核：真流式、审批恢复、checkpoint、上下文压缩、并行工具、子代理 |
| Phase 2 | 计划中 | 沙箱与工具策略：进程/容器隔离、命令策略、SSRF、资源配额 |
| Phase 3 | 计划中 | 能力扩展：Memory/RAG、官方 MCP SDK、Skills、定时任务、浏览器 |
| Phase 4 | 计划中 | 单机交付：本地数据目录、密钥加密、桌面壳/安装包、自动更新、可观测性 |
| Phase 5 | 计划中 | 开源发布：LICENSE、文档、示例、CI Release |

---

## License

License 将在开源发布阶段补充（计划 MIT 或 Apache-2.0）。
