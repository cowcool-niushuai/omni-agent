# Contributing to OmniAgent

感谢你愿意改进 OmniAgent。请先阅读下面的约定，尽量让小改动保持可审查、可回滚。

## 开发环境

```bash
uv sync --all-groups
cp .env.example .env
# 填入自己的模型 API Key；不要提交 .env
uv run uvicorn server:app --host 127.0.0.1 --port 8001
```

前端样式由 Tailwind 编译：

```bash
make frontend
```

桌面端：

```bash
cd desktop
npm install
npm test
npm start
```

## 提交前检查

```bash
uv run pytest
uv run ruff check .
uv run mypy
node --check static/inline.js   # 如需单独检查 UI 脚本
```

请不要提交：

- `.env`
- `model_config.json`
- `mcp_config.json`
- `omni_agent.db`
- `data/`
- `workspace/`
- `desktop/release/`
- `desktop/resources/backend/`
- `node_modules/`

## 安全

- 不要把 API Key、Token、管理员密码、用户数据贴进 Issue 或 PR。
- 安全相关问题请按 [SECURITY.md](SECURITY.md) 私下报告。
- 新增工具必须经过权限分类、参数校验和审计记录；不要把任意 shell 执行直接暴露给模型。
- 涉及文件、命令、网络、桌面应用启动的改动，需要在 PR 中说明风险与默认策略。

## Pull Request

请尽量做到：

1. 一个 PR 只解决一个问题。
2. 说明动机、行为变化和验证方式。
3. 新增功能附带测试。
4. 不修改无关文件，不提交生成物。
5. UI 改动附截图或简短描述。

## 代码风格

- Python 使用 `ruff` 和 `mypy`，遵循现有模块结构。
- 前端保持单文件控制台与本地 vendor 依赖，不新增运行时 CDN。
- 错误信息优先使用清晰的中文；面向 API 的错误保持可解析。
