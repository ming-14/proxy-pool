'use strict';

/**
 * Electron 主进程：启动器窗口 + 面板窗口 + 托盘 + serve 子进程生命周期。
 *
 * 窗口策略：关闭任一窗口只隐藏到托盘（serve 继续跑）；只有托盘「退出」才真正结束。
 * 单实例：第二个实例启动时唤起已有实例的窗口，避免两个进程抢 .cache.db 与端口。
 */

const { app, BrowserWindow, Menu, Tray, ipcMain, nativeImage, shell } = require('electron');
const fs = require('fs');
const path = require('path');

const { ServeManager } = require('./child');
const { TRAY_ICON_PNG_BASE64 } = require('./icons');
const { resolvePaths } = require('./paths');
const { validatePorts } = require('./ports');

const DEFAULT_PROXY_PORT = 8082;
const DEFAULT_WEB_PORT = 8083;
const PREFS_FILE = '.desktop.json';

let paths = null;
let manager = null;
let launcherWindow = null;
let panelWindow = null;
let tray = null;
let quitting = false;
let prefs = { proxyPort: DEFAULT_PROXY_PORT, webPort: DEFAULT_WEB_PORT };

// ---------------------------------------------------------------- 偏好（记忆上次端口）

function loadPrefs() {
  try {
    const raw = JSON.parse(fs.readFileSync(path.join(paths.dataDir, PREFS_FILE), 'utf8'));
    const check = validatePorts(raw.proxyPort, raw.webPort);
    if (check.ok) {
      prefs = { proxyPort: check.proxyPort, webPort: check.webPort };
    }
  } catch {
    /* 首次运行或文件损坏：用默认端口 */
  }
}

function savePrefs({ proxyPort, webPort }) {
  const check = validatePorts(proxyPort, webPort);
  if (!check.ok) return;
  prefs = { proxyPort: check.proxyPort, webPort: check.webPort };
  try {
    fs.writeFileSync(path.join(paths.dataDir, PREFS_FILE), JSON.stringify(prefs), 'utf8');
  } catch (e) {
    console.error('写入偏好失败:', e.message);
  }
}

// ---------------------------------------------------------------- 窗口

/** 窗口/托盘共用图标（每次新建实例，避免跨窗口复用同一 NativeImage） */
function appIcon() {
  return nativeImage.createFromBuffer(Buffer.from(TRAY_ICON_PNG_BASE64, 'base64'));
}

function createLauncherWindow() {
  launcherWindow = new BrowserWindow({
    width: 520,
    height: 640,
    minWidth: 460,
    minHeight: 520,
    title: 'proxy-pool 启动器',
    icon: appIcon(),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  launcherWindow.loadFile(path.join(__dirname, 'renderer', 'launcher.html'));
  launcherWindow.on('close', (e) => {
    if (!quitting) {
      e.preventDefault();
      launcherWindow.hide();
    }
  });
  launcherWindow.on('closed', () => {
    launcherWindow = null;
  });
}

function showLauncher() {
  if (!launcherWindow) createLauncherWindow();
  launcherWindow.show();
  launcherWindow.focus();
}

/** 打开控制面板窗口（已存在则刷新并聚焦）。调用前须已就绪（见调用方判断）。 */
function openPanel() {
  const url = `http://127.0.0.1:${manager.state.webPort}/`;
  if (panelWindow) {
    panelWindow.loadURL(url);
    panelWindow.show();
    panelWindow.focus();
    return;
  }
  panelWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 960,
    minHeight: 640,
    title: 'proxy-pool 控制面板',
    icon: appIcon(),
    backgroundColor: '#f5f6f8',
  });
  panelWindow.loadURL(url);
  panelWindow.on('close', (e) => {
    if (!quitting) {
      e.preventDefault();
      panelWindow.hide();
    }
  });
  panelWindow.on('closed', () => {
    panelWindow = null;
  });
}

function openInBrowser() {
  const { webPort, phase } = manager.state;
  if (webPort != null && phase === 'ready') {
    shell.openExternal(`http://127.0.0.1:${webPort}/`);
  }
}

// ---------------------------------------------------------------- 状态广播与托盘

function broadcastState(state) {
  if (launcherWindow && !launcherWindow.isDestroyed()) {
    launcherWindow.webContents.send('state', state);
  }
  refreshTray();
  if (state.phase === 'ready') {
    openPanel();
    if (launcherWindow && launcherWindow.isVisible()) {
      launcherWindow.hide();
    }
  }
}

function refreshTray() {
  if (!tray) return;
  const { phase } = manager.state;
  const running = phase === 'starting' || phase === 'ready';
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开控制面板', enabled: phase === 'ready', click: openPanel },
    { label: '在浏览器中打开', enabled: phase === 'ready', click: openInBrowser },
    { label: '打开设置', click: showLauncher },
    { type: 'separator' },
    { label: '停止服务', enabled: running, click: () => manager.stop() },
    { label: '重启服务', enabled: running, click: () => manager.restart() },
    { type: 'separator' },
    { label: '退出', click: () => app.quit() },
  ]));
  tray.setToolTip(`proxy-pool - ${manager.state.message}`);
}

function createTray() {
  tray = new Tray(appIcon());
  tray.on('click', () => {
    if (manager.state.phase === 'ready') openPanel();
    else showLauncher();
  });
  refreshTray();
}

// ---------------------------------------------------------------- IPC

function setupIpc() {
  ipcMain.handle('app:getState', () => manager.getState());
  ipcMain.handle('app:getDefaults', () => ({ ...prefs }));
  ipcMain.handle('app:start', async (_e, payload) => {
    const result = await manager.start(payload);
    if (result.ok) savePrefs(payload);
    return result;
  });
  ipcMain.handle('app:stop', () => manager.stop());
  ipcMain.handle('app:restart', () => manager.restart());
}

// ---------------------------------------------------------------- 启动

function bootstrap() {
  paths = resolvePaths();
  fs.mkdirSync(paths.dataDir, { recursive: true });
  loadPrefs();

  manager = new ServeManager({
    paths,
    onState: broadcastState,
    // 日志只推给启动器（面板窗口加载的是外部页面，没有 preload、也不消费这些消息）
    onLog: (lines) => {
      if (launcherWindow && !launcherWindow.isDestroyed()) {
        launcherWindow.webContents.send('log', lines);
      }
    },
  });

  Menu.setApplicationMenu(null);
  setupIpc();
  createLauncherWindow();
  createTray();
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (manager && manager.state.phase === 'ready') openPanel();
    else showLauncher();
  });

  app.whenReady().then(bootstrap);

  // 关掉所有窗口不退出：托盘常驻、serve 继续运行
  app.on('window-all-closed', () => {});

  app.on('before-quit', (e) => {
    if (quitting) return;
    e.preventDefault();
    quitting = true;
    const finish = () => {
      if (tray) tray.destroy();
      app.exit(0);
    };
    manager.stop().then(finish, finish);
  });

  // 进程异常退出时的同步兜底，避免 serve 变孤儿进程
  process.on('exit', () => {
    if (manager) manager.killSync();
  });
}