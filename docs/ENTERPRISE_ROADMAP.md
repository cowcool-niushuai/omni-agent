# OmniAgent 企业级演进路线

本文参考以下开源项目，梳理 OmniAgent 从“本地单机 Agent”走向“可部署、可治理、可扩展的企业级 Agent 平台”的差距和落地顺序：

- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)：插件优先架构、Web UI、安全说明、开发者体验。
- [DeerFlow](https://github.com/bytedance/deer-flow)：LangGraph、子代理、Memory、Sandbox、Skills、MCP、项目、定时任务、文件预览、可观测性、API Token、部署与安全指引。
- [Pi](https://github.com/earendil-works/pi)：多厂商模型抽象、Durable Runtime、Telemetry 契约、RPC/SDK、供应链加固、容器化边界。

## 1. 当前基础

OmniAgent 已经具备：

- LangGraph 显式状态机、真流式、checkpoint、审批中断/恢复
- 命令策略、bubblewrap/process 沙箱、SSRF、资源配额、审计
- Memory/RAG、定时任务、官方 MCP SDK、Skills、子代理、无头浏览器
- OpenAI-compatible + Anthropic 原生模型适配
- 本地 Web UI + CLI
- 会话删除、重命名、搜索、清空、Markdown 导出
- 可选单管理员 API Token
- `/api/system/info` 运行状态与存储统计

这些能力足以支撑单机、可信环境下的重度使用，但还不是企业级多租户平台。

## 2. 企业级差距矩阵

| 能力 | 当前状态 | 目标 | 优先级 |
|---|---|---|---|
| 会话生命周期 | 删除/重命名/搜索/清空/导出 | 归档、批量操作、保留策略、回收站 | P0 |
| 认证 | 可选单管理员 Token | 用户、登录、会话 Cookie、API Token 管理 | P0 |
| 权限 | 工具级风险审批 | RBAC、项目级工具权限、审批人角色 | P0 |
| 多租户 | 单机单用户 | 用户/团队/项目隔离，数据归属 | P0 |
| 项目 | 单一 workspace | Project、项目指令、项目文件、项目记忆 | P1 |
| 文件 | 工作区文件列表 | 上传、预览、下载、配额、Office/PDF | P1 |
| 可观测性 | structlog、审计、system info | Token 用量、Trace、Metrics、OTel/Langfuse | P1 |
| 部署 | 本地 uvicorn | Docker、Compose、健康检查、备份恢复 | P1 |
| 数据治理 | 硬删除 | 保留策略、导出、软删除、合规清理 | P1 |
| 扩展性 | Skills/MCP | 插件 SDK、RPC、事件总线、主题/i18n | P2 |
| 协作 | 单用户 | 会话分享、只读快照、IM 渠道 | P2 |
| SDK | HTTP API | Python/TypeScript Client、嵌入式运行 | P2 |
| 供应链 | uv lock、CI | SBOM、依赖审计、镜像签名、发布校验 | P2 |

## 3. 分阶段路线

### Phase 4.0 生产化基础（当前迭代）

- [x] 会话列表：搜索、消息数、最后消息、相对时间
- [x] 会话重命名
- [x] 会话删除：消息、审批、子代理、审计、checkpoint 一并清理
- [x] 会话清空：保留会话壳，清理上下文
- [x] Markdown 导出
- [x] 自动标题
- [x] 可选 API Token（`OMNI_API_TOKEN`）
- [x] `/api/system/info`
- [x] Markdown XSS 清洗（DOMPurify）
- [x] 前端 Toast、确认弹窗、空状态、复制回答、输入框自适应

验收标准：

- 删除对话后，消息/审批/审计/checkpoint 不再残留
- 未授权请求返回 401，`/health` 仍可用于探针
- 导出文件不包含 API Key
- 前端不因历史对话为空/被删除而白屏

### Phase 4.1 身份、权限与租户

- `users`、`api_tokens`、`projects` 表
- 会话归属 `owner_id` / `project_id`
- 管理员、成员、只读角色
- 项目级工具策略：允许/拒绝/需审批
- 审计保留策略与导出
- 登录限流、CSRF、防暴力破解
- API Token 作用域（只读、会话、工具）

### Phase 4.2 项目与工作区

- Project 实体：名称、指令、默认模型、工具策略
- 每个 Project 独立 workspace、memory、knowledge
- 项目文件树、上传、下载、配额
- 项目级定时任务与子代理
- 项目归档与恢复

### Phase 4.3 文件、附件与预览

- 聊天附件上传
- 图片/PDF/Office/文本预览
- 文件大小、类型、数量配额
- 生成文件可下载
- 文件访问审计

### Phase 4.4 可观测性与成本

- 每次模型调用记录 input/output tokens、模型、耗时、成本
- Token 使用面板：按会话/项目/模型聚合
- OpenTelemetry Trace：HTTP -> Agent -> Tool -> MCP
- 可选 Langfuse/LangSmith 导出
- Prometheus 指标端点
- 审计与运行日志分离

### Phase 4.5 部署与数据治理

- Dockerfile + docker-compose
- 数据目录、备份、恢复、迁移
- SQLite -> PostgreSQL 可选
- 健康检查/就绪检查
- 密钥加密与轮换
- 配置校验和启动前 doctor
- 版本升级与回滚说明

### Phase 4.6 扩展性与生态

- 插件 manifest、生命周期、工具注册
- RPC / Python SDK / TypeScript Client
- 事件总线：session/tool/approval/usage
- 主题、i18n、可访问性
- 技能包与 MCP 模板市场

### Phase 4.7 协作与分发

- 会话只读分享链接与过期时间
- 会话快照、工作区复制
- 飞书/Slack/Telegram/Discord 渠道
- 团队审计与合规导出
- 桌面壳/安装包（可选）

## 4. 工程红线

企业级不是“功能多”，而是以下底线必须成立：

1. **默认安全**：默认只监听 localhost；暴露公网必须启用 Token/登录。
2. **最小权限**：工具、文件、网络、MCP 默认最小授权。
3. **可审计**：谁在何时对哪个会话执行了什么工具，可追溯。
4. **可恢复**：checkpoint、数据库、工作区都有备份与恢复路径。
5. **可删除**：用户数据删除必须覆盖消息、审计、checkpoint 和派生文件。
6. **可测试**：关键路径有自动化测试，CI 必须通过 lint/typecheck/test。
7. **可升级**：配置与数据库迁移有版本号和回退方案。
8. **不泄露密钥**：密钥不进 JSON、不进日志、不进导出、不进前端。

## 5. 建议的下一步

优先做 **Phase 4.1 身份与租户**，因为它是多用户、项目隔离、审计合规和 API Token 作用域的前置条件。做完后再进入 Project/文件/可观测性，避免在单用户模型上堆叠无法治理的功能。
