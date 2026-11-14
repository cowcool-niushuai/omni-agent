# OmniAgent

> 一个可本地运行、可扩展、可恢复的开源 AI Agent 应用。
> 桌面端 + Web UI + CLI + 真实工具执行 + MCP + Skills + Human-in-the-loop 审批。

OmniAgent 是一个**可本地单机运行、也可切换为团队模式**的通用智能体应用：它不只回答问题，还能在工作区里读写文件、执行命令、运行 Python、调用 MCP 工具，并在高危操作前暂停，等待你批准后继续执行。

项目当前已完成 **Phase 3（能力扩展）**、**Phase 4.0 生产化基础**、**Phase 4.1 团队版身份与项目隔离（M2）**、**Phase 4.2 Electron 桌面客户端**，以及 **Phase 4.3 可靠性增强**：Phase 2 的沙箱、命令策略、SSRF 与审计，Phase 3 的本地 Memory/RAG、定时任务、官方 MCP SDK、多厂商模型适配与无头浏览器自动化，以及会话生命周期、可选 API Token、用户/团队/项目、RBAC、项目级工作区、审计导出、原生桌面窗口、持久任务计划、工具契约、混合检索与证据验证器均已落地，目标是成为一个可靠、可审计、可打包分发的开源 Agent 应用。

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
- `web_search`（Tavily / 自托管 SearXNG，返回可引用的搜索结果）
- `web_fetch`（抓取指定 URL 的正文）
- `browser`（无头浏览器：导航、读取、截图、点击/填写/按键）
- `rag_search` / `read_skill_doc`
- `spawn_subagent`（隔离子智能体）

扩展能力：

- **MCP**：基于官方 Python SDK v2（`ClientSession` + `stdio_client`）挂载 stdio MCP Server，动态发现工具、解析标准内容块并自动重连
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

### 10. 定时任务与无人值守执行

定时任务复用同一套 LangGraph 运行时、工具策略、会话历史与审计表：

- `scheduled_tasks` 表保存名称、提示词、间隔、启停状态、下次运行时间、上次结果
- 服务内的单 worker 调度器按 `SCHEDULER_POLL_SECONDS` 轮询到期任务
- 每次运行使用独立会话 `sched_<task_id>`，结果可在 Web 控制台回溯
- **无人值守模式**：任何被判定为 `ASK_USER` 的高危工具调用都会被自动拒绝，不会留下悬挂审批；模型会收到拒绝结果并尝试安全替代方案
- 单次运行超时、结果长度、任务总数、最小/最大间隔均可配置
- 服务重启时，遗留的 `RUNNING` 状态会被标记为 `INTERRUPTED`
- Web 控制台“定时任务”页支持创建、编辑、启停、立即运行、删除和查看上次结果
- REST API：`GET/POST /api/schedules`、`GET/PATCH/DELETE /api/schedules/{id}`、`POST /api/schedules/{id}/run`

### 11. 官方 MCP SDK 客户端

`mcp_manager.py` 已从手写 JSON-RPC 迁移到官方 `mcp` Python SDK v2：

- 使用 `stdio_client` + `ClientSession` 完成握手、工具发现和工具调用
- 每个 Server 的传输与会话由同一后台任务持有，满足 anyio 取消作用域要求
- 同步工具层通过独立事件循环线程调用 SDK，不阻塞 FastAPI 事件循环
- 请求带超时；连接断开或协议错误时自动重连并重试一次
- 支持官方 `CallToolResult` 文本/图片/音频/资源链接内容块，结构化结果自动转 JSON
- `mcp_config.json` 的 Server 配置支持 `command`、`args`、`env`、`cwd`、`enabled`

### 11.1 MCP 可视化管理

设置页的「MCP 服务」现在支持在界面中管理本地 stdio MCP Server，不需要手工编辑配置文件或重启应用：

- 添加本地 MCP Server：名称、启动命令、参数、环境变量、工作目录、说明
- 测试连接：保存前先启动临时进程，探测工具数量和错误原因
- 编辑、启用、禁用、删除 Server
- 保存后热重载：重新连接变更的 Server，保留未变化的连接
- 仅管理员可修改；普通成员只能查看状态和已注册工具

相关 API：

```text
GET    /api/mcp/config
POST   /api/mcp/servers
PATCH  /api/mcp/servers/{name}
DELETE /api/mcp/servers/{name}
POST   /api/mcp/servers/test
POST   /api/mcp/reload
```

配置写入 `mcp_config.json`（可通过 `OMNI_MCP_CONFIG_PATH` 指定，打包版位于用户数据目录），使用原子写入和 `0600` 权限。当前管理能力只覆盖本地 stdio Server；远程 HTTP / SSE / Streamable HTTP、OAuth 和项目级 MCP 配置仍属于后续工作。

MCP 的 `command` 本质上会启动本地进程，因此管理接口仅限管理员，并把配置变更纳入审计和权限边界。

### 11.2 Git 与 GitHub 管理

设置 → Git 与 GitHub 提供当前工作区仓库和 GitHub 账户的统一管理：

- **本地仓库**：自动扫描工作区内仓库；初始化、分支状态、未提交/已暂存文件、差异、日志、暂存/取消暂存、提交、切换/创建分支。
- **远端操作**：设置 `origin`、push、pull（仅 fast-forward）、fetch、clone。
- **GitHub**：连接 Personal Access Token、浏览/创建仓库、查看分支、管理 Pull Request 和 Issue。
- **多仓库**：下拉切换工作区内不同仓库；GitHub 仓库可一键设为 `origin` 或克隆到工作区。

API：

```text
GET    /api/git/repos
GET    /api/git/status?repo=
GET    /api/git/diff?repo=
GET    /api/git/log?repo=
GET    /api/git/branches?repo=
POST   /api/git/init
POST   /api/git/stage
POST   /api/git/unstage
POST   /api/git/commit
POST   /api/git/checkout
POST   /api/git/remote
POST   /api/git/push
POST   /api/git/pull
POST   /api/git/fetch
POST   /api/git/clone

GET    /api/git/github/account
POST   /api/git/github/connect
POST   /api/git/github/disconnect
GET    /api/git/github/repos
POST   /api/git/github/repos
GET    /api/git/github/branches?repo=
GET    /api/git/github/pulls?repo=
POST   /api/git/github/pulls
GET    /api/git/github/issues?repo=
POST   /api/git/github/issues
```

安全约束：

- Git 仓库必须位于当前项目工作区内；检测到父级仓库在工作区外时按“非仓库”处理，不会访问项目外部仓库。
- 远端 URL 仅允许 HTTPS/SSH，且主机必须在 `GIT_ALLOWED_HOSTS` 中；拒绝带用户名密码的 URL。
- GitHub Token 只保存在本机 `data/credentials/github_<user>.json`，权限 `0600`，不会返回给前端、不会写入提示词或审计参数。
- push/pull/fetch 使用临时 `GIT_ASKPASS` 脚本传递 Token，Token 不出现在命令行参数或日志中。
- 用户在界面中主动发起的推送直接执行；Agent 发起的 push/pull/clone 仍经过命令策略审批和沙箱网络开关。

限制：当前只支持 GitHub.com API，不包含 GitHub Enterprise、OAuth 登录、GitLab/Gitea 专用 API；GitLab/Bitbucket 可通过普通 Git remote 使用。

### 12. 无头浏览器自动化

`browser.py` 基于 Playwright + Chromium 提供受控的网页交互能力：

- 首次使用时惰性启动无头浏览器；独立守护线程持有 Playwright 对象，避免同步 API 的线程亲和问题
- 操作：`goto`、`text`、`screenshot`、`click`、`fill`、`press`、`wait`、`back`、`forward`、`reload`、`status`、`close`
- `goto` 先经过静态 SSRF 校验 + DNS 解析；页面所有子资源请求再经过 `context.route` 校验，私网/回环/云元数据地址会被直接阻断
- 不暴露 `page.evaluate`，不允许执行任意 JavaScript；下载与 Service Worker 默认关闭
- `click` / `fill` / `press` 属于 `ASK_USER`，需要人工审批；定时任务中会被自动拒绝
- 截图只能写入 `workspace/browser/`，只允许 `.png`，并有文件大小上限
- 未配置 `BROWSER_PROXY` 时自动复用环境变量 `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`
- `GET /api/browser/status` 查看运行状态；`/health` 也会返回 `browser_running`

首次部署需安装 Chromium：

```bash
uv run playwright install chromium
```

### 13. 多厂商模型适配

模型体系参考 OpenCode 的 **Provider / Model Registry** 形式，但保持 Python 侧实现简洁：

- 模型使用 `provider/model` 引用，例如 `deepseek/deepseek-v4-pro`
- `model_config.json` 按 provider 分组，记录 `adapter`、`base_url`、`env`、`headers`、`body`
- 每个模型可记录 `model_name`、显示名、上下文/输出上限、能力标记（工具、视觉、推理）
- API Key 仍然只从环境变量读取，绝不写入模型配置
- 旧的扁平 `models` 数组会自动迁移到 v2 结构，`active_model_id` 继续兼容

当前内置适配器：

| 适配器 | 覆盖范围 |
|---|---|
| `openai-compatible` | DeepSeek、OpenAI、Qwen、Kimi、智谱、Gemini OpenAI 兼容端点、硅基流动、Ollama、LM Studio、vLLM、OpenRouter 等 |
| `anthropic` | Anthropic 官方 Messages API（原生工具调用、流式文本、thinking、工具结果转换） |

运行时仍然只面向 OpenAI 形态的 `chat.completions`，适配器负责把不同厂商的流式事件、工具调用和消息结构统一成同一套事件，因此 LangGraph、审批、审计和前端无需感知厂商差异。

管理接口：

- `GET /api/models`：返回带 `ref`、`adapter`、能力信息的模型列表
- `POST /api/models/active`：接受模型 ID 或 `provider/model`
- `POST /api/models/test`：测试 provider 连接，返回延迟和可用模型
- `POST /api/models/discover`：从 OpenAI 兼容端点或 Ollama 发现模型并写入注册表
- `POST /api/models/custom`：新增自定义 provider / 模型，可指定适配器

Web UI 的模型配置弹窗已支持厂商预设、适配器选择、连接测试和模型发现。输入框底部的模型名称可以直接点击，在当前对话内切换模型，无需回到设置页。

> Google Gemini 目前使用其 OpenAI 兼容端点；Anthropic 使用原生适配器。OAuth、ChatGPT/Copilot 登录和 models.dev 全量目录不在当前范围内。

### 14. 会话管理与生产化基础

对话记录不再只是“只增不减”的日志：

- 会话列表支持按标题、ID 和消息内容搜索
- 展示消息数、最后一条消息和相对时间
- 支持重命名、删除、清空消息
- 删除会话会同时清理消息、审批请求、子代理任务、审计记录和 LangGraph checkpoint
- 清空消息会保留会话壳，但清除上下文与运行痕迹
- 支持导出为 Markdown
- 首次用户消息会自动生成会话标题
- 助手回答渲染经过 DOMPurify 清洗，降低 Markdown XSS 风险
- Tailwind、Lucide、Marked、DOMPurify、Highlight.js 全部本地化，断网环境也能正常打开控制台

相关 API：

```text
GET    /api/sessions?q=&limit=&offset=
GET    /api/sessions/{id}
PATCH  /api/sessions/{id}
DELETE /api/sessions/{id}
DELETE /api/sessions/{id}/messages
GET    /api/sessions/{id}/export
GET    /api/system/info
```

### 15. 团队版：身份、RBAC 与项目隔离

默认仍是**本地单管理员模式**，不改变原有使用方式。设置：

```bash
AUTH_REQUIRED=true
```

后切换为团队模式：

- 用户名 / 密码登录，密码使用 PBKDF2-SHA256 加盐哈希存储
- HttpOnly Cookie 会话，写操作强制 CSRF 校验
- 登录限流、失败锁定、会话过期与主动退出
- 用户角色：`admin` / `member` / `viewer`
- 团队角色：`owner` / `admin` / `member`
- 项目成员继承团队角色；`viewer` 全局只读，不能创建会话或修改资源
- 会话、记忆、知识库、定时任务、审计记录和工作区目录全部按项目隔离
- 默认项目继续使用 `workspace/`；其他项目使用 `data/project_workspaces/<project_id>/`，不会嵌套在默认工作区中，避免默认项目通过路径或沙箱访问其他项目文件
- 侧边栏可切换项目，前端自动发送 `X-Project-ID`
- API Token 支持作用域（如 `sessions:read`、`chat:write`），明文只在创建时展示一次
- 审计记录支持按项目导出 CSV，管理员可按保留天数清理

相关 API：

```text
GET    /api/auth/status
POST   /api/auth/login
POST   /api/auth/logout
GET    /api/auth/me
POST   /api/auth/password
GET    /api/users
POST   /api/users
PATCH  /api/users/{id}
GET    /api/teams
POST   /api/teams
GET    /api/teams/{id}/members
POST   /api/teams/{id}/members
GET    /api/projects
POST   /api/projects
PATCH  /api/projects/{id}
GET    /api/projects/{id}/stats
GET    /api/tokens
POST   /api/tokens
DELETE /api/tokens/{id}
GET    /api/audit/export
POST   /api/audit/purge
```

首次启动且数据库中没有用户时，会自动创建管理员：

- 用户名由 `ADMIN_USERNAME` 决定，默认 `admin`
- 密码优先读取 `OMNI_ADMIN_PASSWORD`
- 未设置时生成随机密码，写入 `data/.initial_admin_password`（权限 `0600`）

项目管理、团队成员、用户和 Token 都可以在 Web UI 的“团队管理”页完成。旧的单用户数据会自动迁移到默认团队 `默认团队` 和默认项目 `默认项目`，原 `workspace/` 继续作为默认项目工作区。

企业级差距与后续路线见 [`docs/ENTERPRISE_ROADMAP.md`](docs/ENTERPRISE_ROADMAP.md)。

### 16. Electron 桌面客户端

`desktop/` 提供原生桌面窗口，不再依赖浏览器标签页：

- 原生窗口、系统托盘、启动画面和窗口位置记忆
- 自动探测并复用已运行的 `127.0.0.1:8001` 后端
- 后端未启动时自动拉起仓库 `.venv` 中的 `uvicorn`
- 后端日志写入系统应用数据目录的 `backend.log`，菜单和托盘可直接打开
- 退出时只停止由桌面客户端自己启动的后端，不影响外部服务
- 外部链接交给系统浏览器，页面导航限制在本地后端源
- 单实例锁、加载失败重试、渲染进程崩溃提示、窗口边界校验

快速启动：

```bash
cd desktop
npm install
npm start
```

或者从仓库根目录：

```bash
make desktop-install
make desktop
```

也可以执行 `./launch_desktop.sh`。如果 Electron 依赖尚未安装，脚本会回退到 Chrome/Chromium 独立应用窗口。

常用命令：

```bash
make desktop-test    # Electron 后端管理单元测试
make desktop-smoke   # 无界面截图冒烟测试
make desktop-backend # 用 PyInstaller 构建内嵌 Python 后端
make desktop-pack    # 构建后端 + 生成未压缩应用目录
make desktop-dist    # 构建后端 + 生成 Linux AppImage + deb
```

桌面端环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OMNI_DESKTOP_PORT` | `8001` | 后端端口 |
| `OMNI_DESKTOP_REPO_ROOT` | 仓库根目录 | 开发模式下的 Python 后端源码目录 |
| `OMNI_DESKTOP_PYTHON` | 仓库 `.venv` Python | 开发模式启动后端使用的解释器 |
| `OMNI_DESKTOP_BACKEND` | 空 | 直接指定后端可执行文件，优先级最高 |
| `OMNI_DESKTOP_USER_DATA` | 系统用户数据目录 | 打包版数据、工作区、模型与 MCP 配置存放位置 |

打包版默认**内嵌 PyInstaller 构建的 Python 后端**，最终用户无需安装 Python。`npm run pack` / `npm run dist` 会先执行 `npm run backend:build`，再把 onedir 后端放进 Electron 的 `resources/backend`。运行时数据写入系统用户数据目录（Linux 为 `~/.local/share/OmniAgent`），升级或重装不会覆盖会话、工作区和密钥。开发模式仍直接复用仓库 `.venv`；需要自定义后端时可用 `OMNI_DESKTOP_BACKEND` 覆盖。完整说明见 [`desktop/README.md`](desktop/README.md)。

---

### 17. 可靠性增强：计划、工具契约、混合检索与验证器

这一阶段解决“会调用工具”与“能可靠完成任务”之间的差距：

- **持久任务计划**
  - 新增 `update_plan(goal, done_when, steps)` 工具
  - 每个步骤包含 `status`、`acceptance` 和可核验的 `evidence`
  - 计划写入 LangGraph checkpoint，刷新页面、审批恢复后仍然存在
  - Web 控制台顶部展示实时计划面板（完成数、状态、验收条件、证据）
- **工具契约层**
  - 所有原生工具使用 Pydantic 参数模型校验；未知字段、缺参、类型错误在进入权限和沙箱前直接拒绝
  - MCP 工具保留官方 JSON Schema 校验
  - 统一错误分类：`INVALID_ARGUMENTS`、`TIMEOUT`、`BLOCKED`、`TOOL_ERROR`、`UNKNOWN_TOOL`
  - 仅对只读且幂等的工具自动重试，避免重复写文件或重复执行命令
  - 审计表新增 `error_code`，便于统计工具失败原因
- **混合检索与结构化摘要**
  - BM25 + 本地 hashed embedding 混合打分，不依赖外部 embedding API
  - 中文单字/双字切分，标题、标签、记忆 key 与重要度加权
  - 上下文压缩改为结构化摘要：用户目标、决策、文件、证据、错误、未完成事项、下一步
- **Actor + Verifier 自检**
  - 模型尝试结束时，验证器检查计划：存在未完成步骤，或 completed 步骤没有 evidence，则打回继续执行
  - 最多 `VERIFICATION_MAX_RETRIES` 次修复，避免无限循环
  - Web UI 显示“执行中 / 待补充证据 / 通过 / 未通过”
  - 原则：**没有证据，不算完成**
- **可观测性**
  - SSE 新增 `plan_update` 与 `verification` 事件
  - 审计记录包含工具契约错误码、决策、状态、耗时和输出摘要

相关 API：

```text
GET /api/sessions/{session_id}/plan   # 当前持久计划与验证状态
```

当前实现使用本地 hashed embedding，不是语义 embedding 模型；它适合离线和小型知识库。需要更强语义召回时，可在 `retrieval.py` 中替换为可配置的 embedding provider，接口无需变化。

---

### 18. 联网搜索：Tavily 与 SearXNG

`web_search(query, max_results)` 是独立于本地知识库的联网检索工具。项目不内置搜索 Key，也不默认联网；未配置时会明确返回“联网搜索未启用”，不会编造结果。

支持的搜索后端：

- **Tavily**：托管服务，配置 `SEARCH_PROVIDER=tavily` + `SEARCH_API_KEY`，开箱即用
- **SearXNG**：自托管元搜索，配置 `SEARCH_PROVIDER=searxng` + `SEARCH_BASE_URL`，适合本地/隐私部署；需要开启 JSON 输出

推荐工作流：

```text
web_search -> 选择结果 -> web_fetch 读取正文 -> 综合回答并附来源 URL
```

安全边界：

- 搜索服务地址同样经过 SSRF 校验；默认禁止私网、回环和链路本地地址
- 自托管 SearXNG 位于本机或内网时，才设置 `SEARCH_ALLOW_PRIVATE_NETWORK=true`
- 标题、摘要和网页正文都视为不可信数据，不能当作指令执行
- 搜索关键词、结果 URL 和工具调用都会进入审计（密钥自动脱敏）
- 回答应引用来源 URL；需要全文时用 `web_fetch` 再抓取，不能只凭搜索摘要下结论

配置示例：

```dotenv
# Tavily
SEARCH_PROVIDER=tavily
SEARCH_API_KEY=tvly-...

# 或自托管 SearXNG
# SEARCH_PROVIDER=searxng
# SEARCH_BASE_URL=http://127.0.0.1:8080
```

不配置任何搜索后端时，Agent 仍可使用本地知识库、文件、命令和浏览器；只是不会声称自己完成了联网搜索。

### 19. 完全访问模式（Full Access）

默认是 **严格模式**：命令在 bubblewrap 中执行，默认无网络，文件读取限制在工作区，高危操作需要审批。

管理员可以在 **设置 → 权限与安全** 切换为 **完全访问**。聊天输入框左下角的盾牌按钮也可以快速切换：显示“询问”时为每次审批，显示“完全”时为完全访问。该设置按项目保存到 `project_settings` 表，只影响当前项目。

完全访问会关闭：

- bubblewrap 沙箱，命令直接在宿主机执行
- 工作区路径限制，允许读写工作区外的本地文件
- 网络隔离，允许联网命令
- 常规工具审批，Agent 不再逐次询问
- 浏览器交互审批

仍然保留的硬保护：

- `/etc`、`/root`、`/sys`、`/proc`、`/boot`、`/dev` 等系统目录的写入会被拒绝
- `rm -rf /` 等灾难性命令会被拒绝
- SSRF、回环、私网和云元数据地址保护仍然生效
- 文件大小、命令输出、超时和审计记录仍然生效

接口：

```http
GET  /api/security/policy
PATCH /api/security/policy
{ "mode": "strict" | "full_access" }
```

> ⚠️ 完全访问可以读取宿主机的环境变量、SSH 配置和本地密钥。只应在可信设备上临时开启，完成任务后切回严格模式。

### 20. 对话附件与拖拽上传

首页输入框和对话输入框都支持附件；左侧有附件按钮，也可以把文件直接拖进输入框：

- 支持多文件，默认最多 5 个
- 单文件默认上限 20MB
- 图片、文本、PDF、Office 文件等都可以上传
- 支持 `Ctrl+V` 直接粘贴剪贴板图片
- 上传后显示文件名和大小，可逐个移除

文件保存到：

```text
workspace/.uploads/<session_id>/<随机前缀>_<原文件名>
```

文件权限为 `0600`，`.uploads` 不会出现在工作区文件列表里。

发送消息时，前端会先把附件上传到：

```http
POST /api/uploads
```

然后消息里附带文件清单：

```text
[用户附加文件]
- report.pdf (2.4 MB) -> .uploads/.../report.pdf
```

Agent 会把附件内容视为不可信数据，需要时通过 `file_read`、`python_eval` 或专用解析工具读取。附件路径同样受当前项目工作区限制。

### 图片理解

当模型声明 `capabilities.vision=true`（或模型名称包含 `视觉` / `vision` / `gpt-4o` / `gpt-4.1` / `claude` / `gemini` / `qwen-vl`）时，图片附件会自动转成多模态内容块发送给模型：

- OpenAI 兼容接口：`image_url` + `data:image/...;base64,...`
- Anthropic：`image` + `source.base64`

默认限制：

```text
最多 4 张图片
单张图片最大 8MB
```

图片必须是工作区内的 `.png` / `.jpg` / `.jpeg` / `.webp` / `.gif`，路径经过工作区逃逸校验。数据库和 checkpoint 只保存文字与附件路径，不保存图片 base64。

如果当前模型不支持视觉，图片仍会作为附件保存，Agent 只能读取文件元数据或通过工具处理，不会声称自己“看懂了图片”。

当前限制：

- 不会自动 OCR 图片，也不会自动解析 PDF/Word 版式；需要 Agent 调用相应工具
- 附件不会自动清理，长期使用建议定期清理 `.uploads`
- 完全访问模式下附件路径可能指向工作区外，需自行确认安全

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
│  agent -> approval/tools -> agent         │
│  agent -> verify(evidence) -> agent/END   │
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
│   ├── tenancy.py             # 请求级租户上下文（用户/团队/项目/工作区）
│   ├── security/
│   │   ├── paths.py           # 路径越界防护
│   │   ├── commands.py        # 命令白名单、参数校验、SQL 只读策略
│   │   ├── sandbox.py         # process / bubblewrap 沙箱执行
│   │   ├── ssrf.py            # URL 静态校验、DNS 解析与私网拦截
│   │   └── limits.py          # 输出截断
│   └── runtime/
│       ├── graph.py           # LangGraph StateGraph 与 AgentRuntime
│       ├── contracts.py       # 工具参数契约、错误分类与重试策略
│       ├── planning.py        # 持久计划、计划事件与验证规则
│       └── context.py         # token 计数与结构化上下文压缩
├── permission.py              # 风险分类、审批请求、批量状态更新
├── audit.py                   # 工具执行审计（脱敏、统计）
├── tools.py                   # 原生工具与 MCP 工具分发
├── prompts.py                 # 动态系统提示词（稳定+相关记忆注入）
├── retrieval.py               # BM25 + 本地向量混合检索（记忆/知识库）
├── search.py                  # 联网搜索后端（Tavily / SearXNG，SSRF 校验）
├── scheduler.py               # 定时任务调度器（无人值守 auto_reject）
├── browser.py                 # Playwright 无头浏览器（SSRF/审批/截图沙箱）
├── database.py                # SQLite 持久化（含 tool_audit / scheduled_tasks）
├── model_manager.py           # Provider/Model Registry（密钥只从环境变量读取）
├── provider_adapters.py       # OpenAI-compatible / Anthropic 原生适配器
├── mcp_manager.py             # 官方 MCP SDK 客户端桥接（独立事件循环/超时/重连）
├── mcp_config.py              # MCP 配置文件校验、持久化与热重载
├── mcp_api.py                 # MCP Server 管理 API（仅管理员）
├── skills_manager.py          # Skills 扫描与写入
├── server.py                  # FastAPI 入口与 SSE
├── cli.py                     # Rich CLI / 审批交互 / 审计查询
├── static/index.html          # Web 控制台（含审计页）
├── desktop/                   # Electron 桌面客户端（窗口/托盘/后端生命周期）
├── docs/                      # 企业级路线与企业部署文档
├── Dockerfile                 # 生产镜像（含 bwrap / Chromium）
├── docker-compose.yml         # 单机生产编排
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

### Docker 部署

```bash
cp .env.example .env
# 至少填写一个模型 Key；暴露到 localhost 之外时务必设置 OMNI_API_TOKEN
docker compose build
docker compose up -d
```

默认映射 `127.0.0.1:8001`，包含 Chromium、Playwright 依赖与 bubblewrap。`workspace/`、`data/` 和 `.env` 通过 volume 持久化。若要通过公网访问，请在前面加 TLS 反向代理，并启用 `OMNI_API_TOKEN` 或接入统一身份认证。

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
| `SEARCH_PROVIDER` | 联网搜索后端：`disabled` / `tavily` / `searxng` | `disabled` |
| `SEARCH_API_KEY` | Tavily API Key | 无 |
| `SEARCH_BASE_URL` | Tavily 自定义地址或 SearXNG 地址 | 无 |
| `SEARCH_MAX_RESULTS` | 默认返回结果数 | `5` |
| `SEARCH_MAX_RESULTS_LIMIT` | 单次搜索最大结果数 | `10` |
| `SEARCH_MAX_QUERY_CHARS` | 搜索关键词最大长度 | `500` |
| `SEARCH_TIMEOUT_SECONDS` | 搜索请求超时（秒） | `10` |
| `SEARCH_LANGUAGE` | SearXNG 搜索语言 | `zh-CN` |
| `SEARCH_ALLOW_PRIVATE_NETWORK` | 允许搜索后端位于私网（仅可信自托管） | `false` |
| `HOST` | 监听地址 | `127.0.0.1` |
| `PORT` | 监听端口 | `8001` |
| `OMNI_API_TOKEN` | 可选单管理员 API Token（兼容模式）；留空关闭 | 无 |
| `AUTH_REQUIRED` | 启用团队模式（登录 + Cookie 会话 + RBAC） | `false` |
| `AUTH_COOKIE_NAME` | 登录 Cookie 名称 | `omni_session` |
| `AUTH_COOKIE_SECURE` | Cookie 是否仅 HTTPS；生产环境应开启 | `false` |
| `AUTH_COOKIE_SAMESITE` | Cookie SameSite 策略 | `lax` |
| `AUTH_SESSION_DAYS` | 登录会话有效天数 | `7` |
| `AUTH_RATE_LIMIT_PER_MINUTE` | 登录接口每分钟限流 | `30` |
| `AUTH_MAX_FAILED_ATTEMPTS` | 连续失败锁定阈值 | `5` |
| `AUTH_LOCKOUT_SECONDS` | 锁定时长 | `300` |
| `ADMIN_USERNAME` | 初始管理员用户名 | `admin` |
| `ADMIN_EMAIL` | 初始管理员邮箱 | 无 |
| `OMNI_ADMIN_PASSWORD` | 初始管理员密码；留空自动生成 | 无 |
| `AUDIT_RETENTION_DAYS` | 审计保留天数 | `180` |
| `WORKSPACE_DIR` | 默认项目工作区目录 | `./workspace` |
| `DATA_DIR` | checkpoint 等数据目录 | `./data` |
| `CONTEXT_MAX_TOKENS` | 上下文压缩阈值 | `24000` |
| `CONTEXT_KEEP_LAST` | 压缩时保留的最近消息数 | `12` |
| `MEMORY_INJECTION_LIMIT` | 系统提示词注入记忆上限 | `6` |
| `MEMORY_ALWAYS_INCLUDE` | 固定注入的高重要度记忆数 | `3` |
| `MEMORY_MAX_CONTENT_CHARS` | 单条记忆长度上限 | `8000` |
| `KNOWLEDGE_MAX_CONTENT_CHARS` | 单篇知识文档长度上限 | `20000` |
| `MAX_ITERATIONS` | 单轮最大推理步数 | `20` |
| `ATTACHMENT_MAX_BYTES` | 单个附件最大字节数 | `20971520` |
| `ATTACHMENT_MAX_FILES` | 单次最多附件数 | `5` |
| `VISION_MAX_IMAGES` | 单次最多发送给模型的图片数 | `4` |
| `VISION_MAX_IMAGE_BYTES` | 单张视觉图片最大字节数 | `8388608` |
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
| `GIT_ENABLED` | 启用 Git/GitHub 管理 | `true` |
| `GIT_ALLOWED_HOSTS` | 允许的 Git 远程主机（逗号分隔） | `github.com,gitlab.com,bitbucket.org` |
| `GIT_TIMEOUT_SECONDS` | Git 命令超时（秒） | `60` |
| `GIT_MAX_DIFF_CHARS` | diff 输出最大字符数 | `200000` |
| `AUDIT_ENABLED` | 是否写入工具审计 | `true` |
| `TOOL_RETRY_ATTEMPTS` | 幂等只读工具最大尝试次数 | `2` |
| `TOOL_RETRY_BACKOFF_SECONDS` | 工具重试退避基数 | `0.3` |
| `VERIFICATION_MAX_RETRIES` | 计划验证未通过时的最大修复轮数 | `2` |
| `RETRIEVAL_HYBRID_WEIGHT` | 混合检索向量权重（0-1） | `0.7` |
| `RETRIEVAL_EMBEDDING_DIM` | 本地 hashed embedding 维度 | `256` |
| `SCHEDULER_ENABLED` | 是否启动定时任务调度器 | `true` |
| `SCHEDULER_POLL_SECONDS` | 调度器轮询间隔 | `15` |
| `SCHEDULER_TASK_TIMEOUT_SECONDS` | 单次定时任务超时 | `600` |
| `SCHEDULER_MIN_INTERVAL_SECONDS` | 最小运行间隔 | `60` |
| `SCHEDULER_MAX_TASKS` | 任务数量上限 | `100` |
| `BROWSER_ENABLED` | 是否启用浏览器工具 | `true` |
| `BROWSER_HEADLESS` | 是否无头运行 Chromium | `true` |
| `BROWSER_TIMEOUT_SECONDS` | 浏览器操作超时 | `20` |
| `BROWSER_MAX_TEXT_CHARS` | 单次提取正文上限 | `8000` |
| `BROWSER_SCREENSHOT_MAX_BYTES` | 截图文件大小上限 | `5242880` |
| `BROWSER_PROXY` | 浏览器代理；留空复用环境变量 | 无 |

完整列表见 `.env.example`。模型 provider 与模型列表在 `model_config.json` 中管理，支持 `provider/model` 引用与 `openai-compatible` / `anthropic` 适配器；**API Key 不会写入该文件**，只从环境变量读取。可参考 `model_config.example.json`。

---

## 安全约定

- `.env`、`model_config.json`、`*.db`、`workspace/`、`data/` 均不入库
- 密钥只从环境变量读取；`.env` 权限应为 `600`
- 默认只监听 `127.0.0.1`，不对外暴露；暴露到网络前必须设置 `AUTH_REQUIRED=true`（团队模式）或 `OMNI_API_TOKEN`（兼容 Token 模式），并建议放在 TLS 反向代理之后
- 团队模式务必设置 `AUTH_COOKIE_SECURE=true`，并使用强随机 `OMNI_ADMIN_PASSWORD`
- 文件路径统一经过 `safe_join`，阻止 `../` 与符号链接逃逸
- 命令默认在 bubblewrap 沙箱中执行，无 shell、默认无网络、只写工作区
- **完全访问模式会关闭沙箱、审批和路径限制，并能读取宿主机环境变量与密钥；只应在可信设备上临时开启**
- `web_fetch` 拒绝私网、回环、链路本地与云元数据地址
- `web_search` 的搜索服务地址同样经过 SSRF 校验；只有可信自托管 SearXNG 才应开启 `SEARCH_ALLOW_PRIVATE_NETWORK`
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

- `pytest`：167 passed
- `ruff`：All checks passed
- `mypy`：Success，15 source files

测试覆盖包括：路径穿越、命令白名单与 shell 绕过、沙箱超时/输出上限/环境变量清洗、SSRF（回环/元数据/私网/端口）、密钥不泄露、技能 ID 校验、API 冒烟、真流式、工具调用、审批批准/拒绝、自动拒绝模式、checkpoint 恢复、上下文压缩、审计脱敏与统计、BM25 中文检索、记忆/知识 CRUD、记忆 upsert 与提示词召回、定时任务 CRUD/到期判定/无人值守执行/中断恢复、官方 MCP SDK 工具发现与调用、MCP 配置校验/增删改/启停/测试/热重载、浏览器参数校验/私网拦截/交互审批、Provider/Model Registry 迁移、`provider/model` 选择、Anthropic 消息/工具/流式转换、模型发现与连接探测、会话搜索/重命名/清空/删除/导出、自动标题、API Token 保护、密码哈希、登录与 CSRF、项目隔离、跨项目访问拒绝、viewer 只读、Token 作用域、审批请求项目隔离、最后一个管理员保护、工具参数契约与非法参数拦截、工具错误分类、幂等重试策略、计划规范化与验证规则、验证器修复循环、计划 API 与 UI、全访问模式转换、受保护路径与权限策略 API 往返、附件上传路径/文件名清洗/大小限制、图片多模态内容块与 Anthropic 图片转换、默认推理步数上限。

---

## 路线图

| 阶段 | 状态 | 内容 |
|---|---|---|
| Phase 0 | ✅ 完成 | 安全止血与工程底座：路径安全、密钥治理、日志、测试、CI |
| Phase 1 | ✅ 完成 | LangGraph 内核：真流式、审批恢复、checkpoint、上下文压缩、并行工具、子代理 |
| Phase 2 | ✅ 完成 | 沙箱与工具策略：bubblewrap/进程隔离、命令白名单、SSRF、资源配额、审计 |
| Phase 3 | ✅ 完成 | 能力扩展：Memory/RAG、定时任务、官方 MCP SDK、多厂商模型适配、Skills、无头浏览器自动化 |
| Phase 4.0 | ✅ 完成 | 生产化基础：会话生命周期、搜索/导出、可选 API Token、系统信息、前端交互完善 |
| Phase 4.1 | ✅ 完成 | 团队版 M2：用户/团队/项目、登录与 CSRF、RBAC、API Token 作用域、项目隔离、审计导出 |
| Phase 4.2 | ✅ 完成 | Electron 桌面客户端：原生窗口、托盘、启动画面、后端自动拉起与日志、窗口状态记忆 |
| Phase 4.3 | ✅ 完成 | 可靠性增强：持久计划、工具契约与幂等重试、混合检索、结构化摘要、证据验证器 |
| Phase 4 | 🚧 进行中 | 企业级平台：内嵌后端安装包、文件/预览、可观测性、Docker/备份、插件与 SDK |
| Phase 5 | 计划中 | 开源发布：LICENSE、文档、示例、CI Release |

---

## License

License 将在开源发布阶段补充（计划 MIT 或 Apache-2.0）。
