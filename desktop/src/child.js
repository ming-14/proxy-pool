'use strict';

/**
 * serve 子进程管理：启动内嵌 Python 跑 `cli.py serve`，等待面板就绪，并负责停止。
 *
 * 关键点：
 * - 端口预检在 spawn 之前做，冲突时给明确原因（pproxy 在首轮 fill 之后才 bind，
 *   不预检的话端口冲突要等十几秒才以「子进程退出」的形式暴露，用户看不出原因）。
 * - `._pth` 存在时解释器忽略 PYTHON* 环境变量，故用命令行开关：
 *   `-u` 不缓冲、`-B` 不写 pyc 到只读 resources、`-s` 不挂宿主机 user site-packages
 *   （否则宿主同名依赖会覆盖内嵌运行时）、`-X utf8` 保证管道读取不乱码。
 * - 就绪三段式：TCP 可连（已 bind）→ /api/status 200（不依赖静态产物）→
 *   / 返回 200 且含 `#app` 挂载点（专捕「前端忘了 build」的静默失败）。
 */

const { spawn, spawnSync, execFile } = require('child_process');
const fs = require('fs');
const http = require('http');
const net = require('net');
const path = require('path');

const { validateProxyPort, isPortFree, findFreePort, recommendFreePort } = require('./ports');

const READY_TIMEOUT_MS = 180000; // 首轮 fill 要探测外网（10~30s），留足余量
const POLL_INTERVAL_MS = 400;
const PROBE_TIMEOUT_MS = 1500;
const STOP_TIMEOUT_MS = 8000;
const LOG_RING_MAX = 200;

/** 尝试连接 127.0.0.1:port，能连上即视为已监听 */
function tcpReachable(port, timeoutMs = PROBE_TIMEOUT_MS) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: '127.0.0.1', port });
    const done = (ok) => {
      socket.removeAllListeners();
      socket.destroy();
      resolve(ok);
    };
    socket.setTimeout(timeoutMs);
    socket.once('connect', () => done(true));
    socket.once('timeout', () => done(false));
    socket.once('error', () => done(false));
  });
}

/** 取回 HTTP 状态码与响应体（面板校验用） */
function httpGet(url, timeoutMs = PROBE_TIMEOUT_MS) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({ status: res.statusCode || 0, body: Buffer.concat(chunks).toString('utf8') }));
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error('timeout')));
    req.on('error', () => resolve({ status: 0, body: '' }));
  });
}

/** 结束进程树（Windows 无 SIGTERM，child.kill() 不保证杀干净；pproxy/aiohttp 均在进程内） */
function killTree(pid) {
  return new Promise((resolve) => {
    if (!pid) return resolve();
    execFile('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true }, () => resolve());
  });
}

/** 退出钩子里的同步兜底 */
function killTreeSync(pid) {
  if (!pid) return;
  try {
    spawnSync('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true });
  } catch {
    /* 进程可能已退出 */
  }
}

class ServeManager {
  /**
   * @param {object} opts
   * @param {{pythonExe: string, pyappDir: string, dataDir: string}} opts.paths
   * @param {(state: object) => void} opts.onState 状态变化回调
   */
  constructor({ paths, onState }) {
    this.paths = paths;
    this.onState = onState;
    this.child = null;
    this._exitPromise = null;
    this._intentionalStop = false;
    this._logLines = [];
    this._partialLine = '';
    // suggestedProxyPort：代理端口冲突时给出的可用替代端口，供启动器一键改用
    this.state = {
      phase: 'idle',
      message: '未启动',
      proxyPort: null,
      webPort: null,
      pid: null,
      suggestedProxyPort: null,
    };
  }

  getState() {
    return { ...this.state };
  }

  _set(partial) {
    this.state = { ...this.state, ...partial };
    this.onState(this.getState());
  }

  _appendLog(text) {
    const combined = this._partialLine + text;
    const parts = combined.split(/\r?\n/);
    this._partialLine = parts.pop() ?? '';
    const lines = parts.filter((l) => l.length > 0);
    if (lines.length === 0) return;
    this._logLines.push(...lines);
    if (this._logLines.length > LOG_RING_MAX) {
      this._logLines = this._logLines.slice(-LOG_RING_MAX);
    }
  }

  /** 最近若干行输出（stdout+stderr 合并），用于启动失败时回显线索 */
  _logTail(n = 20) {
    return this._logLines.slice(-n).join('\n');
  }

  /** 启动：校验代理端口 → 预检占用 → 挑空闲面板端口 → spawn → 等待就绪 */
  async start({ proxyPort }) {
    if (this.child) {
      return { ok: false, error: '服务已在运行' };
    }
    const check = validateProxyPort(proxyPort);
    if (!check.ok) {
      // 清掉上一轮的推荐端口，避免非法输入时还挂着旧建议
      this._set({ phase: 'error', message: check.error, suggestedProxyPort: null });
      return check;
    }

    this._set({
      phase: 'starting',
      message: '正在检查端口…',
      proxyPort: check.proxyPort,
      webPort: null,
      pid: null,
      suggestedProxyPort: null,
    });
    if (!(await isPortFree(check.proxyPort))) {
      // 占用是用户最常撞到的问题，必须给出原因 + 一个确实可用的替代端口
      const suggestedProxyPort = await recommendFreePort(check.proxyPort);
      const error = `代理端口 ${check.proxyPort} 已被占用`;
      this._set({ phase: 'error', message: error, suggestedProxyPort });
      return { ok: false, error, suggestedProxyPort };
    }
    const webPort = await findFreePort(check.proxyPort);

    if (!fs.existsSync(this.paths.pythonExe) || !fs.existsSync(path.join(this.paths.pyappDir, 'cli.py'))) {
      const error = '内嵌 Python 运行时缺失，请先执行 npm run prepare:runtime 构建';
      this._set({ phase: 'error', message: error });
      return { ok: false, error };
    }

    fs.mkdirSync(this.paths.dataDir, { recursive: true });

    const cliPath = path.join(this.paths.pyappDir, 'cli.py');
    // 两个端口都只监听 127.0.0.1：面板无鉴权，不暴露到局域网
    // -s：不挂宿主机的 user site-packages（site 会按 APPDATA 找到宿主目录）；
    // 否则用户机器上若装有同名依赖，会覆盖内嵌运行时，破坏自包含
    const args = [
      '-u', '-B', '-s', '-X', 'utf8', cliPath, 'serve',
      '--host', '127.0.0.1', '--port', String(check.proxyPort),
      '--web-host', '127.0.0.1', '--web-port', String(webPort),
    ];
    this._partialLine = '';
    this._intentionalStop = false;
    this._set({ phase: 'starting', message: '正在启动服务并探测代理…', webPort });

    const child = spawn(this.paths.pythonExe, args, {
      cwd: this.paths.dataDir,
      env: { ...process.env, PROXY_POOL_DATA: this.paths.dataDir },
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    this.child = child;
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (d) => this._appendLog(d));
    child.stderr.on('data', (d) => this._appendLog(d));

    this._exitPromise = new Promise((resolve) => {
      child.once('exit', (code) => resolve(code));
    });
    child.once('exit', (code) => {
      const wasIntentional = this._intentionalStop;
      this.child = null;
      if (wasIntentional) {
        this._set({ phase: 'stopped', message: '已停止', pid: null });
      } else {
        const message = `服务进程意外退出（退出码 ${code}）`;
        this._set({ phase: 'crashed', message, pid: null });
      }
    });

    this._set({ pid: child.pid });

    try {
      await this._waitReady(webPort, Date.now() + READY_TIMEOUT_MS);
    } catch (e) {
      const error = e && e.message ? e.message : String(e);
      await this.stop();
      this._set({ phase: 'error', message: error });
      return { ok: false, error };
    }
    this._set({ phase: 'ready', message: `代理服务 127.0.0.1:${check.proxyPort} · 控制面板 127.0.0.1:${webPort}`, pid: child.pid });
    return { ok: true };
  }

  /** 轮询直到满足条件；期间子进程退出或主动停止立即失败 */
  async _pollUntil(fn, deadline, timeoutMessage) {
    for (;;) {
      if (this._intentionalStop) throw new Error('已取消');
      if (this.child === null) throw new Error(`服务进程已退出。\n${this._logTail()}`);
      if (await fn()) return;
      if (Date.now() > deadline) throw new Error(`${timeoutMessage}\n${this._logTail()}`);
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    }
  }

  async _waitReady(webPort, deadline) {
    const base = `http://127.0.0.1:${webPort}`;
    await this._pollUntil(() => tcpReachable(webPort), deadline, '等待面板端口监听超时');
    this._set({ message: '面板已绑定，正在校验接口…' });
    await this._pollUntil(
      async () => (await httpGet(`${base}/api/status`)).status === 200,
      deadline,
      '等待面板接口就绪超时',
    );
    this._set({ message: '正在校验前端产物…' });
    await this._pollUntil(async () => {
      const res = await httpGet(`${base}/`);
      return res.status === 200 && res.body.includes('<div id="app"');
    }, deadline, '等待面板页面就绪超时（前端产物可能未构建：请运行 frontend 的 npm run build）');
  }

  /** 停止并等待退出 */
  async stop() {
    if (!this.child) {
      if (this.state.phase !== 'error' && this.state.phase !== 'stopped' && this.state.phase !== 'crashed') {
        this._set({ phase: 'idle', message: '未启动', pid: null });
      }
      return { ok: true };
    }
    const pid = this.child.pid;
    this._intentionalStop = true;
    this._set({ phase: 'stopping', message: '正在停止…' });
    await killTree(pid);
    const exited = await Promise.race([
      this._exitPromise.then(() => true),
      new Promise((r) => setTimeout(() => r(false), STOP_TIMEOUT_MS)),
    ]);
    if (!exited) {
      return { ok: false, error: '停止服务超时，进程可能仍在运行' };
    }
    return { ok: true };
  }

  /** 停止后按原代理端口重启（面板端口重新挑） */
  async restart() {
    const { proxyPort } = this.state;
    if (proxyPort == null) {
      return { ok: false, error: '尚未启动过服务' };
    }
    await this.stop();
    return this.start({ proxyPort });
  }

  /** 进程退出钩子用的同步兜底（异步清理来不及执行时） */
  killSync() {
    if (this.child && this.child.pid) {
      killTreeSync(this.child.pid);
    }
  }
}

module.exports = { ServeManager };