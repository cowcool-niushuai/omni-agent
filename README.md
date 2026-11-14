# OmniAgent - 企业级全自主智能体平台 (Autonomous Multi-Tool Agent Platform)

> 具备真实环境交互、Model Context Protocol (MCP) 标准协议外挂、动态 Skills 扩展、ReAct 思考行动闭环与企业级安全权限拦截的生产级 AI Agent 架构。

---

## 🌟 项目核心亮点 (Core Highlights)

区别于市面上 90%“只能做文档问答的纯 RAG 玩具项目”，OmniAgent 是一个**真正具备业务闭环与自主执行力**的通用智能体：

1. **真实环境交互 (Execution Kernel)**：
   - 具备独立的工作区沙箱 (`./workspace`)。
   - 可自主编写代码、多轮编辑文件、执行真实 Linux Shell 指令、运行 Python 脚本并自检结果。
2. **Model Context Protocol (MCP) 标准协议集成**：
   - 遵循 Anthropic MCP 协议标准（JSON-RPC 2.0 over stdio/SSE）。
   - 实现了企业级 SQLite MCP Server，动态发现工具 (`tools/list`) 并挂载给大模型调用 (`tools/call`)。
3. **动态技能库架构 (Skills Engine)**：
   - 遵循 OpenClaw / Antigravity 行业标准 `SKILL.md` 规范。
   - 支持动态发现技能、按需读取规范（数据深度分析、生产环境排错、网络深度调研），亦支持在线扩展新技能。
4. **ReAct 思考行动闭环 (Reasoning + Acting)**：
   - 采用 `Thought` -> `Action` -> `Observation` -> `Verification` 范式。
   - 摆脱单次问答限制，支持多跳复杂推理（例如：查表结构 -> 统计行数 -> 写脚本分析 -> 输出报表）。
5. **企业级权限防御网关 (Permission & Human-in-the-Loop)**：
   - 4-Stage 权限分级：只读操作自动放行，高危命令（如 `rm`, `kill`, `chmod`, 格式化等）触发审批拦截，在前端生成人工授权卡片，防止模型失控。
6. **三层记忆与知识中枢 (3-Layer Memory & RAG)**：
   - 短期记忆：会话上下文滑窗与压缩。
   - 长期记忆：SQLite 持久化用户画像与偏好。
   - 语义知识：RAG 降级为 Agent 的外部检索工具。
7. **全实时 SSE 交互与高颜值暗色控制台**：
   - Server-Sent Events (SSE) 毫秒级流式响应。
   - 思考链折叠卡片、实时终端输出、工作区文件即时在线代码预览。

---

## 📁 核心目录架构

```bash
omni-agent/
├── agent_core.py       # ReAct 循环引擎，支持流式 SSE 生成器
├── tools.py            # 原生工具集 (Shell、文件读写/编辑、Python沙箱、网络抓取)
├── prompts.py          # 动态系统提示词构建（身份、技能、MCP、长期记忆融合）
├── mcp_manager.py      # MCP 协议客户端，支持 JSON-RPC 2.0 工具发现与调用
├── mcp_config.json     # MCP 服务器挂载配置文件
├── mcp_servers/        # 独立的 MCP 微服务
│   └── sqlite_mcp.py   # 标准 SQLite MCP Server
├── skills_manager.py   # 技能库扫描、解析与注入引擎
├── skills/             # 预置技能目录
│   ├── data_analysis/  # 数据分析与可视化技能 (SKILL.md)
│   ├── system_diagnostics/ # Linux 环境健康排查技能 (SKILL.md)
│   └── web_research/   # 深度网络调研技能 (SKILL.md)
├── permission.py       # 4 级安全权限拦截器 (Human-in-the-loop)
├── database.py         # SQLite 持久化层 (Sessions, Messages, Memories, Knowledge)
├── server.py           # FastAPI 服务入口 (SSE 流式输出与完整 REST API)
├── omni_agent/         # 新增的配置、日志与安全基础设施
│   ├── config.py
│   ├── logging_config.py
│   └── security/
├── tests/              # pytest 测试
├── workspace/          # Agent 专属工作区沙箱 (生成的代码与文件存放于此)
└── static/
    └── index.html      # SaaS 级暗色交互控制台
```

---

## 🚀 本地开发

要求 Python 3.12 与 [uv](https://docs.astral.sh/uv/)。

```bash
cp .env.example .env      # 填入 provider key
uv sync --all-groups
uv run uvicorn server:app --host 127.0.0.1 --port 8001 --reload
```

打开 👉 **`http://127.0.0.1:8001`**。

常用命令：

```bash
make test       # pytest
make lint       # ruff
make typecheck  # mypy
make check      # 全部检查
```

安全约定：

- `.env`、`model_config.json`、`*.db`、`workspace/` 均不入库。
- 密钥只从环境变量读取，不写进源码或 JSON。
- 默认只监听 `127.0.0.1`；内网部署请放在反向代理/网关之后，并先完成 Phase 1 的登录与 RBAC。
- 如果曾经把 API Key 写进过源码或截图，请立即到 provider 控制台轮换。
