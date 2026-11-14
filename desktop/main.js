'use strict';

const fs = require('node:fs');
const path = require('node:path');
const {
  app,
  BrowserWindow,
  Menu,
  Tray,
  dialog,
  ipcMain,
  nativeImage,
  screen,
  shell,
} = require('electron');

const { BackendManager } = require('./backend');

const APP_NAME = 'OmniAgent';
const PORT = Number(process.env.OMNI_DESKTOP_PORT) || 8001;
const REPO_ROOT =
  process.env.OMNI_DESKTOP_REPO_ROOT || path.resolve(__dirname, '..');
const ICON_PATH = path.join(__dirname, 'resources', 'icon.png');
const isDev = process.argv.includes('--dev');
const isSmokeTest = process.argv.includes('--smoke-test');

let mainWindow = null;
let splashWindow = null;
let tray = null;
let backend = null;
let bootstrapping = false;
let quitting = false;

app.setName(APP_NAME);
app.setAppUserModelId('ai.omniagent.desktop');

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    showMainWindow();
  });
}

function windowStatePath() {
  return path.join(app.getPath('userData'), 'window-state.json');
}

function readWindowState() {
  const fallback = { width: 1440, height: 900 };
  try {
    const saved = JSON.parse(fs.readFileSync(windowStatePath(), 'utf8'));
    const width = Math.max(1024, Number(saved.width) || fallback.width);
    const height = Math.max(720, Number(saved.height) || fallback.height);
    const state = { width, height };
    const x = Number(saved.x);
    const y = Number(saved.y);
    if (Number.isFinite(x) && Number.isFinite(y)) {
      const visible = screen.getAllDisplays().some((display) => {
        const area = display.workArea;
        return (
          x + width > area.x &&
          y + height > area.y &&
          x < area.x + area.width &&
          y < area.y + area.height
        );
      });
      if (visible) {
        state.x = x;
        state.y = y;
      }
    }
    return state;
  } catch {
    return fallback;
  }
}

function saveWindowState() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const bounds = mainWindow.getNormalBounds();
  try {
    fs.mkdirSync(app.getPath('userData'), { recursive: true });
    fs.writeFileSync(windowStatePath(), JSON.stringify(bounds, null, 2));
  } catch {
    // Window state is a convenience, never a startup blocker.
  }
}

function createSplashWindow() {
  if (splashWindow && !splashWindow.isDestroyed()) return splashWindow;
  splashWindow = new BrowserWindow({
    width: 460,
    height: 330,
    resizable: false,
    frame: false,
    show: false,
    backgroundColor: '#0F1715',
    icon: ICON_PATH,
    webPreferences: {
      preload: path.join(__dirname, 'splash-preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  splashWindow.loadFile(path.join(__dirname, 'renderer', 'loading.html'), {
    query: { version: app.getVersion() },
  });
  splashWindow.once('ready-to-show', () => splashWindow.show());
  splashWindow.on('closed', () => {
    splashWindow = null;
  });
  return splashWindow;
}

function showSplashError(message) {
  const splash = createSplashWindow();
  const logPath = backend ? backend.logPath : '';
  splash.loadFile(path.join(__dirname, 'renderer', 'loading.html'), {
    query: {
      error: message,
      logPath,
      version: app.getVersion(),
    },
  });
  if (!splash.isVisible()) splash.show();
}

function createMainWindow() {
  const state = readWindowState();
  mainWindow = new BrowserWindow({
    ...state,
    minWidth: 1024,
    minHeight: 720,
    show: false,
    title: APP_NAME,
    backgroundColor: '#F5F8F7',
    icon: ICON_PATH,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });

  // Keep the native menu available with Alt, but hide the dark menu strip.
  mainWindow.setAutoHideMenuBar(true);
  mainWindow.setMenuBarVisibility(false);

  const allowedOrigin = new URL(backend.url).origin;
  const wc = mainWindow.webContents;

  wc.setWindowOpenHandler(({ url }) => {
    try {
      if (new URL(url).origin !== allowedOrigin) shell.openExternal(url);
    } catch {
      // Ignore malformed or non-http URLs.
    }
    return { action: 'deny' };
  });

  wc.on('will-navigate', (event, url) => {
    try {
      if (new URL(url).origin !== allowedOrigin) {
        event.preventDefault();
        if (/^https?:\/\//i.test(url)) shell.openExternal(url);
      }
    } catch {
      event.preventDefault();
    }
  });

  wc.session.setPermissionRequestHandler((_contents, permission, callback) => {
    const allowed = new Set([
      'notifications',
      'clipboard-read',
      'clipboard-sanitized-write',
    ]);
    callback(allowed.has(permission));
  });

  let loadRetries = 0;
  wc.on('did-fail-load', (_event, code, description, _url, isMainFrame) => {
    if (!isMainFrame || code === -3) return;
    if (loadRetries < 3) {
      loadRetries += 1;
      setTimeout(() => {
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.loadURL(backend.url);
        }
      }, 800);
      return;
    }
    dialog.showMessageBox(mainWindow, {
      type: 'error',
      title: 'OmniAgent 页面加载失败',
      message: `无法加载本地界面：${description}`,
      detail: `请查看日志：${backend.logPath}`,
      buttons: ['重试', '退出'],
      defaultId: 0,
      cancelId: 1,
    }).then((result) => {
      if (result.response === 0) {
        loadRetries = 0;
        mainWindow.loadURL(backend.url);
      } else {
        app.quit();
      }
    });
  });

  wc.on('render-process-gone', (_event, details) => {
    dialog.showErrorBox(
      'OmniAgent 渲染进程异常',
      `界面进程退出：${details.reason}\n日志：${backend.logPath}`,
    );
  });

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
    if (splashWindow && !splashWindow.isDestroyed()) splashWindow.close();
  });
  mainWindow.on('close', saveWindowState);
  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  mainWindow.loadURL(backend.url);
  if (isDev) wc.openDevTools({ mode: 'detach' });

  if (isSmokeTest) {
    wc.once('did-finish-load', () => {
      setTimeout(async () => {
        try {
          const image = await wc.capturePage();
          const output = path.join(app.getPath('temp'), 'omni-desktop-smoke.png');
          fs.writeFileSync(output, image.toPNG());
          console.log(`desktop smoke ok: ${output}`);
        } catch (error) {
          console.error(`desktop smoke failed: ${error.message}`);
          process.exitCode = 1;
        } finally {
          app.quit();
        }
      }, 1500);
    });
  }
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    if (backend) createMainWindow();
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function openLogs() {
  if (backend && fs.existsSync(backend.logPath)) {
    shell.showItemInFolder(backend.logPath);
    return;
  }
  shell.openPath(app.getPath('userData'));
}

function toggleWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    showMainWindow();
  } else if (mainWindow.isVisible() && !mainWindow.isMinimized()) {
    mainWindow.hide();
  } else {
    showMainWindow();
  }
}

function createTray() {
  try {
    const image = nativeImage.createFromPath(ICON_PATH).resize({
      width: 22,
      height: 22,
    });
    tray = new Tray(image);
    tray.setToolTip(APP_NAME);
    tray.setContextMenu(
      Menu.buildFromTemplate([
        { label: '打开 OmniAgent', click: showMainWindow },
        { label: '显示 / 隐藏', click: toggleWindow },
        { type: 'separator' },
        { label: '打开后端日志', click: openLogs },
        { type: 'separator' },
        { label: '退出', click: () => app.quit() },
      ]),
    );
    tray.on('click', toggleWindow);
  } catch {
    tray = null;
  }
}

function createMenu() {
  const template = [
    {
      label: APP_NAME,
      submenu: [
        {
          label: `关于 ${APP_NAME}`,
          click: () => {
            dialog.showMessageBox({
              type: 'info',
              title: `关于 ${APP_NAME}`,
              message: `${APP_NAME} ${app.getVersion()}`,
              detail: '本地优先、可审计、可扩展的智能体工作台。',
              buttons: ['好'],
            });
          },
        },
        { type: 'separator' },
        { label: '打开后端日志', click: openLogs },
        { type: 'separator' },
        { role: 'quit', label: '退出' },
      ],
    },
    {
      label: '编辑',
      submenu: [
        { role: 'undo', label: '撤销' },
        { role: 'redo', label: '重做' },
        { type: 'separator' },
        { role: 'cut', label: '剪切' },
        { role: 'copy', label: '复制' },
        { role: 'paste', label: '粘贴' },
        { role: 'selectAll', label: '全选' },
      ],
    },
    {
      label: '视图',
      submenu: [
        { role: 'reload', label: '重新加载' },
        { role: 'forceReload', label: '强制重新加载' },
        { role: 'toggleDevTools', label: '开发者工具' },
        { type: 'separator' },
        { role: 'resetZoom', label: '实际大小' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' },
      ],
    },
    {
      label: '窗口',
      submenu: [
        { role: 'minimize', label: '最小化' },
        { role: 'close', label: '关闭' },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

async function bootstrap() {
  if (bootstrapping) return { ok: false, error: '正在启动中' };
  bootstrapping = true;
  createSplashWindow();
  try {
    backend = new BackendManager({
      repoRoot: REPO_ROOT,
      appData: app.getPath('userData'),
      port: PORT,
      packaged: app.isPackaged,
    });
    await backend.ensureRunning();
    if (!mainWindow || mainWindow.isDestroyed()) {
      createMainWindow();
    } else {
      mainWindow.loadURL(backend.url);
      showMainWindow();
    }
    return { ok: true, url: backend.url };
  } catch (error) {
    const message =
      error && error.message ? error.message : '未知错误，无法启动本地后端。';
    showSplashError(message);
    return { ok: false, error: message };
  } finally {
    bootstrapping = false;
  }
}

ipcMain.handle('omni:pick-folder', async () => {
  const owner = mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined;
  const result = await dialog.showOpenDialog(owner, {
    title: '选择项目文件夹',
    properties: ['openDirectory', 'createDirectory'],
  });
  return result.canceled ? null : result.filePaths[0] || null;
});

ipcMain.handle('omni:retry', () => bootstrap());
ipcMain.handle('omni:open-logs', () => {
  openLogs();
  return true;
});
ipcMain.handle('omni:quit', () => {
  app.quit();
  return true;
});

app.whenReady().then(async () => {
  createMenu();
  createTray();
  await bootstrap();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      if (backend) createMainWindow();
      else bootstrap();
    } else {
      showMainWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', (event) => {
  if (quitting) return;
  if (backend && backend.owned) {
    event.preventDefault();
    quitting = true;
    backend
      .stop()
      .catch(() => {})
      .finally(() => app.quit());
  }
});

app.on('will-quit', () => {
  if (tray) {
    tray.destroy();
    tray = null;
  }
});
