# OmniAgent

> 一个可本地运行、可扩展、可恢复的开源 AI Agent 应用。
> Web UI + CLI + 真实工具执行 + MCP + Skills + Human-in-the-loop 审批。

OmniAgent 是一个**单机单用户**的通用智能体应用：它不只回答问题，还能在工作区里读写文件、执行命令、运行 Python、调用 MCP 工具，并在高危操作前暂停，等待你批准后继续执行。

项目当前处于 **Phase 3（能力扩展）进行中** 的状态：Phase 2 的沙箱、命令策略、SSRF 与审计已完成，Phase 3 的本地 Memory/RAG 已落地，目标是成为一个可靠、可审计、可打包分发的开源 Agent 应用。

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
| `ASK_USER` | `rm`、`chmod`、网络访问、未知命令等，暂停等待审批 |
| `DENY` | 系统目录、shell 操作符、SSRF 目标、非只读 SQL 等，直接阻断 |

命令策略已经从“正则黑名单”升级为三层校验：

1. **语法解析**：`shlex` 解析后 **不使用 shell** 执行；严格模式直接拒绝 `; & | < > $()` 等操作符
2. **可执行文件白名单**：只读 / 工作区写入 / 需审批 / 禁止执行四类；未知二进制需要人工批准
3. **参数校验**：路径参数必须留在工作区；`find -exec`、`git -c`、`pip install` 等高危参数会升级为审批

### 6. 沙箱与资源配额

- **bubblewrap 后端**（默认 `auto`，检测到 `bwrap` 即启用）：只读系统目录、可写工作区、私有 `/tmp`、默认无网络
- **process 后端**：无 shell + 独立进程组 + 环境变量清洗 + `RLIMIT_CPU/AS/FSIZE/NOFILE` 限制
- 命令超时、输出字节上限、工具执行超时、并发上限、单文件写入上限
- 子智能体共享同一套策略，遇到高危操作直接拒绝而不是暂停父图

### 7. SSRF 防护

`web_fetch` 只允许 `http/https` 与白名单端口，拒绝：

- `localhost`、`127.0.0.0/8`、`10/8`、`172.16/12`、`192.168/16`
- 链路本地地址、云元数据地址（`169.254.169.254`）
- CGNAT、保留地址、IPv6 回环/映射地址
- URL 中的用户名密码、非白名单端口

每次重定向都会重新校验；请求建立后还会检查实际连接的 peer IP，关闭 DNS rebinding 窗口。

### 8. 审计与执行记录

每次工具调用（包括被拒绝的调用）都会写入 `tool_audit` 表：

- 风险级别、审批决策、执行状态
- 沙箱后端、退出码、耗时、输出字符数与摘要
- 参数中的 `api_key` / `token` / `secret` 等字段自动脱敏

Web 控制台新增“审计记录”页，也可以调用 `GET /api/audit`、`GET /api/audit/stats`；CLI 使用 `/audit` 查看当前会话记录。

### 9. 会话、记忆与知识库

- 会话与消息持久化到 SQLite
- **本地 BM25 检索**：`omni_agent/retrieval.py` 提供中英文混合分词与排序，中文按 unigram + bigram 处理，不依赖嵌入服务或网络
- **查询相关记忆注入**：系统提示词固定注入最重要的 3 条用户画像，再按当前问题召回相关记忆（默认上限 6 条）
- **记忆自动落库**：Agent 可通过 `memory_save` 保存用户偏好/稳定事实/任务结论，通过 `memory_search` 回忆；相同 `category+key` 会更新而不是重复插入
- **知识库可写**：`knowledge_add` 写入文档，`rag_search` 用 BM25 检索；Web UI 支持新增、编辑、删除、搜索
- **REST API**：`GET/POST /api/memories`、`PATCH/DELETE /api/memories/{id}`、`GET/POST /api/knowledge`、`PATCH/DELETE /api/knowledge/{id}`，均支持 `?q=` 检索
- 工作区文件可在 Web UI 中浏览和预览

记忆与知识库都存放在本地 SQLite（`omni_agent.db`）。记忆写入走统一权限分类：`memory_search` 只读放行，`memory_save` / `knowledge_add` 作为本地安全写入自动执行并进入审计日志。

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
│ DeepSeek... │  │ command policy + sandbox  │
└─────────────┘  │ SSRF guard + audit trail  │
                 └───────────────────────────┘
```

---

## 目录结构

```text
omni-agent/
├── agent_core.py              # 兼容 facade，转发到 LangGraph runtime
├── omni_agent/
│   ├── config.py              # pydantic-settings 配置
│   ├── logging_config.py      # structlog
│   ├── security/
│   │   ├── paths.py           # 路径越界防护
│   │   ├── commands.py        # 命令白名单、参数校验、SQL 只读策略
│   │   ├── sandbox.py         # process / bubblewrap 沙箱执行
│   │   ├── ssrf.py            # URL 静态校验、DNS 解析与私网拦截
│   │   └── limits.py          # 输出截断
│   └── runtime/
│       ├── graph.py           # LangGraph StateGraph 与 AgentRuntime
│       └── context.py         # token 计数与上下文压缩
├── permission.py              # 风险分类、审批请求、批量状态更新
├── audit.py                   # 工具执行审计（脱敏、统计）
├── tools.py                   # 原生工具与 MCP 工具分发
├── prompts.py                 # 动态系统提示词（稳定+相关记忆注入）
├── retrieval.py               # BM25 + 中英文混合分词（记忆/知识库）
├── database.py                # SQLite 持久化（含 tool_audit 表）
├── model_manager.py           # 多 provider 模型管理（密钥只从环境变量读取）
├── mcp_manager.py             # MCP stdio 客户端（超时/重连/并发锁）
├── skills_manager.py          # Skills 扫描与写入
├── server.py                  # FastAPI 入口与 SSE
├── cli.py                     # Rich CLI / 审批交互 / 审计查询
├── static/index.html          # Web 控制台（含审计页）
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
| `MEMORY_INJECTION_LIMIT` | 系统提示词注入记忆上限 | `6` |
| `MEMORY_ALWAYS_INCLUDE` | 固定注入的高重要度记忆数 | `3` |
| `MEMORY_MAX_CONTENT_CHARS` | 单条记忆长度上限 | `8000` |
| `KNOWLEDGE_MAX_CONTENT_CHARS` | 单篇知识文档长度上限 | `20000` |
| `MAX_ITERATIONS` | 单轮最大推理步数 | `8` |
| `SANDBOX_BACKEND` | `auto` / `bwrap` / `process` | `auto` |
| `SANDBOX_NETWORK` | 沙箱是否允许网络 | `false` |
| `COMMAND_POLICY_MODE` | `strict` / `permissive` | `strict` |
| `COMMAND_TIMEOUT_SECONDS` | 单条命令超时 | `30` |
| `COMMAND_MAX_MEMORY_MB` | 命令内存上限 | `512` |
| `TOOL_TIMEOUT_SECONDS` | 单次工具执行超时 | `60` |
| `TOOL_MAX_OUTPUT_CHARS` | 工具输出字符上限 | `12000` |
| `TOOL_MAX_CONCURRENCY` | 并行工具调用上限 | `4` |
| `MAX_FILE_WRITE_BYTES` | 单文件写入上限 | `10485760` |
| `WEB_FETCH_TIMEOUT_SECONDS` | 网页抓取超时 | `10` |
| `WEB_FETCH_ALLOWED_PORTS` | 允许访问的端口 | `80,443,8080,8443` |
| `ALLOW_PRIVATE_NETWORK` | 是否允许抓取内网地址 | `false` |
| `MCP_REQUEST_TIMEOUT_SECONDS` | MCP 请求超时 | `15` |
| `AUDIT_ENABLED` | 是否写入工具审计 | `true` |

完整列表见 `.env.example`。模型 provider 与模型列表在 `model_config.json` 中管理；**API Key 不会写入该文件**，只从环境变量读取。可参考 `model_config.example.json`。

---

## 安全约定

- `.env`、`model_config.json`、`*.db`、`workspace/`、`data/` 均不入库
- 密钥只从环境变量读取；`.env` 权限应为 `600`
- 默认只监听 `127.0.0.1`，不对外暴露
- 文件路径统一经过 `safe_join`，阻止 `../` 与符号链接逃逸
- 命令默认在 bubblewrap 沙箱中执行，无 shell、默认无网络、只写工作区
- `web_fetch` 拒绝私网、回环、链路本地与云元数据地址
- 高危命令通过审批暂停，但 `process` 后端不是强安全边界；不要把不可信任务直接放到生产主机上运行
- 审计表会记录参数摘要，已自动脱敏密钥字段；仍建议不要在工作区存放生产密钥

如果曾经把 API Key 提交到源码、日志或截图，请立即到 provider 控制台轮换。

---

## 测试

```bash
uv run pytest -q
uv run ruff check .
uv run mypy
```

当前结果：

- `pytest`：79 passed
- `ruff`：All checks passed
- `mypy`：Success，12 source files

测试覆盖包括：路径穿越、命令白名单与 shell 绕过、沙箱超时/输出上限/环境变量清洗、SSRF（回环/元数据/私网/端口）、密钥不泄露、技能 ID 校验、API 冒烟、真流式、工具调用、审批批准/拒绝、checkpoint 恢复、上下文压缩、审计脱敏与统计、BM25 中文检索、记忆/知识 CRUD、记忆 upsert 与提示词召回。

---

## 路线图

| 阶段 | 状态 | 内容 |
|---|---|---|
| Phase 0 | ✅ 完成 | 安全止血与工程底座：路径安全、密钥治理、日志、测试、CI |
| Phase 1 | ✅ 完成 | LangGraph 内核：真流式、审批恢复、checkpoint、上下文压缩、并行工具、子代理 |
| Phase 2 | ✅ 完成 | 沙箱与工具策略：bubblewrap/进程隔离、命令白名单、SSRF、资源配额、审计 |
| Phase 3 | 🚧 进行中 | 能力扩展：Memory/RAG（✅ 本地 BM25 + 记忆/知识 CRUD + UI）、官方 MCP SDK、Skills、定时任务、浏览器 |
| Phase 4 | 计划中 | 单机交付：本地数据目录、密钥加密、桌面壳/安装包、自动更新、可观测性 |
| Phase 5 | 计划中 | 开源发布：LICENSE、文档、示例、CI Release |

---

## License

License 将在开源发布阶段补充（计划 MIT 或 Apache-2.0）。
