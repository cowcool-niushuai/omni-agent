# OmniAgent Desktop

Electron 桌面客户端。它复用现有 FastAPI / LangGraph 后端，提供原生窗口、系统托盘、启动画面、后端生命周期管理和后端日志入口。

## 前置条件

- Node.js 20+（当前验证版本：Node 24）
- npm 10+
- 已安装 OmniAgent Python 依赖（仓库根目录存在 `.venv`）
- Linux 桌面环境（X11 或 Wayland）；Windows / macOS 也可运行，但打包配置当前以 Linux 为主

## 快速开始

```bash
cd desktop
npm install
npm start
```

或从仓库根目录：

```bash
make desktop-install
make desktop
```

也可以直接运行 `./launch_desktop.sh`。脚本会优先启动 Electron；如果桌面依赖尚未安装，会回退到浏览器独立窗口。

默认连接 `http://127.0.0.1:8001`：

- 如果该地址已有健康的 OmniAgent 后端，客户端直接复用，不重复启动
- 如果端口空闲，客户端自动启动仓库 `.venv` 中的 `uvicorn`
- 如果端口被其他程序占用，会显示错误页和后端日志路径

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OMNI_DESKTOP_PORT` | `8001` | 后端监听端口 |
| `OMNI_DESKTOP_REPO_ROOT` | 当前仓库根目录 | Python 后端所在目录 |
| `OMNI_DESKTOP_PYTHON` | `.venv/bin/python` / `.venv/Scripts/python.exe` / `python3` | 启动后端使用的 Python |
| `OMNI_DESKTOP_BACKEND` | 空 | 直接指定后端可执行文件；优先于 Python 命令 |

示例：

```bash
OMNI_DESKTOP_PORT=8100 npm start
OMNI_DESKTOP_PYTHON=/opt/omni/bin/python npm start
OMNI_DESKTOP_BACKEND=/opt/omni/bin/omni-agent-server npm start
```

## 架构

```text
Electron main process
  ├─ BackendManager
  │    ├─ probe /health，复用已运行服务
  │    ├─ spawn uvicorn（开发模式）或 OMNI_DESKTOP_BACKEND
  │    ├─ 等待健康检查（最长 60 秒）
  │    └─ 退出时仅停止自己启动的进程
  ├─ Splash窗口（460×330）
  ├─ BrowserWindow（加载后端 Web UI）
  ├─ 系统托盘 / 菜单
  └─ window-state.json（窗口位置与大小）
```

主窗口启用 `contextIsolation`、关闭 `nodeIntegration` 并启用 `sandbox`。外部链接交给系统浏览器；页面导航限制在本地后端源。

后端标准输出和错误写入：

```text
~/.config/OmniAgent/backend.log
```

Windows 对应 `%APPDATA%/OmniAgent/`，macOS 对应 `~/Library/Application Support/OmniAgent/`。

## 测试与冒烟

```bash
cd desktop
npm test

# 无界面截图冒烟测试
env -u ELECTRON_RUN_AS_NODE npx electron . --smoke-test
```

截图保存到系统临时目录的 `omni-desktop-smoke.png`。

如果当前 shell 设置了 `ELECTRON_RUN_AS_NODE=1`（某些 Agent/IDE 环境会这样），必须使用 `env -u ELECTRON_RUN_AS_NODE`，否则 Electron 会退化成普通 Node 进程。

## 打包

```bash
npm run pack   # 仅生成未压缩目录 release/linux-unpacked
npm run dist   # 生成 Linux AppImage 和 deb
```

当前桌面包**尚未嵌入 Python 后端**。打包后的应用需要：

1. 目标机器上已有可用的 OmniAgent 后端（端口 8001），或
2. 通过 `OMNI_DESKTOP_BACKEND` 指向独立后端可执行文件，或
3. 通过 `OMNI_DESKTOP_REPO_ROOT` + `OMNI_DESKTOP_PYTHON` 指向源码环境

后续计划是把 Python 运行时、依赖和启动器一起打进安装包，实现真正的单文件安装。

## 已知限制

- 当前没有内嵌 Python 运行时；AppImage/deb 需要外部后端
- 未做代码签名、自动更新、崩溃上报
- 托盘在部分 Linux 桌面发行版需要 `libappindicator` 支持
- 主要验证环境是 Linux/X11；其他平台需要补充 CI 验证
