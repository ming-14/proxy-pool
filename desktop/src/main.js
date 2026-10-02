'use strict';

/**
 * Electron 主进程：启动器窗口 + 面板窗口 + 托盘 + serve 子进程生命周期。
 *
 * 启动器只承载「代理端口 + 启动」；网页服务器端口是内部页面，由 ServeManager
 * 每次启动时挑一个空闲端口。托盘只提供「打开控制面板 / 重启服务 / 退出」——
 * 改设置与停服不对用户开放，服务要么在跑要么随「退出」结束。
 * 窗口策略：关闭任一窗口只隐藏到托盘（serve 继续跑）；只有托盘「退出」才真正结束。
 * 单实例：第二个实例启动时唤起已有实例的窗口，避免两个进程抢 .cache.db 与端口。
 */

const { app, BrowserWindow, Menu, Tray, ipcMain, nativeImage } = require('electron');
const fs = require('fs');
const path = require('path');

const { ServeManager } = require('./child');
const { resolvePaths } = require('./paths');
const { validateProxyPort } = require('./ports');

const DEFAULT_PROXY_PORT = 8082;
const PREFS_FILE = '.desktop.json';

let paths = null;
let manager = null;
let launcherWindow = null;
let panelWindow = null;
let tray = null;
let quitting = false;
let prefs = { proxyPort: DEFAULT_PROXY_PORT };

// ---------------------------------------------------------------- 偏好（记忆上次代理端口）

function loadPrefs() {
  try {
    const raw = JSON.parse(fs.readFileSync(path.join(paths.dataDir, PREFS_FILE), 'utf8'));
    const check = validateProxyPort(raw.proxyPort);
    if (check.ok) {
      prefs = { proxyPort: check.proxyPort };
    }
  } catch {
    /* 首次运行或文件损坏：用默认端口 */
  }
}

function savePrefs({ proxyPort }) {
  const check = validateProxyPort(proxyPort);
  if (!check.ok) return;
  prefs = { proxyPort: check.proxyPort };
  try {
    fs.writeFileSync(path.join(paths.dataDir, PREFS_FILE), JSON.stringify(prefs), 'utf8');
  } catch (e) {
    console.error('写入偏好失败:', e.message);
  }
}

// ---------------------------------------------------------------- 窗口

/**
 * 窗口/托盘共用图标：assets/icon.png（打包后随 src 进 asar）。
 * 每次调用重新解析，避免跨窗口复用同一个 NativeImage 实例。
 */
function appIcon() {
  return nativeImage.createFromPath(path.join(__dirname, 'assets', 'icon.png'));
}

function createLauncherWindow() {
  launcherWindow = new BrowserWindow({
    width: 420,
    // 高度由渲染层按内容回报（见 app:resizeLauncher），这里只给初始值
    height: 150,
    resizable: false,
    maximizable: false,
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
  // 只留「进面板 / 重启 / 退出」：改设置与停服不开放给用户，退出即停服
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开控制面板', enabled: phase === 'ready', click: openPanel },
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
  // 提示出现/消失时窗口随内容收放，固定高度要么留白要么裁掉提示
  ipcMain.on('app:resizeLauncher', (_e, contentHeight) => {
    if (!launcherWindow || launcherWindow.isDestroyed()) return;
    const height = Math.round(Number(contentHeight));
    if (!Number.isFinite(height)) return;
    const [width] = launcherWindow.getContentSize();
    launcherWindow.setContentSize(width, Math.min(Math.max(height, 120), 260));
  });
}

// ---------------------------------------------------------------- 启动

function bootstrap() {
  paths = resolvePaths();
  fs.mkdirSync(paths.dataDir, { recursive: true });
  loadPrefs();

  // 日志不再推给渲染层（启动器只剩端口 + 启动）；子进程输出仍留在
  // ServeManager 的环形缓冲里，用于失败时拼装 state.message
  manager = new ServeManager({
    paths,
    onState: broadcastState,
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