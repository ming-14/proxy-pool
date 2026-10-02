'use strict';

/** 启动器渲染逻辑：绑定端口输入、启动/停止/重启，并跟随主进程状态与日志 */

const els = {
  proxyPort: document.getElementById('proxy-port'),
  webPort: document.getElementById('web-port'),
  start: document.getElementById('btn-start'),
  stop: document.getElementById('btn-stop'),
  restart: document.getElementById('btn-restart'),
  badge: document.getElementById('status-badge'),
  message: document.getElementById('status-message'),
  logs: document.getElementById('logs'),
  logOutput: document.getElementById('log-output'),
};

// phase -> [徽标文案, 徽标配色]
const PHASE_LABEL = {
  idle: ['未启动', ''],
  starting: ['启动中', 'warn'],
  ready: ['运行中', 'ok'],
  stopping: ['停止中', 'warn'],
  stopped: ['已停止', ''],
  error: ['启动失败', 'err'],
  crashed: ['服务已退出', 'err'],
};

const STOPPED_PHASES = new Set(['idle', 'stopped', 'error', 'crashed']);
const BUSY_PHASES = new Set(['starting', 'stopping']);

function appendLog(text) {
  els.logOutput.textContent += text;
  els.logOutput.scrollTop = els.logOutput.scrollHeight;
}

function render(state) {
  const [label, variant] = PHASE_LABEL[state.phase] || ['未知', ''];
  els.badge.textContent = label;
  els.badge.className = 'badge' + (variant ? ` ${variant}` : '');
  els.message.textContent = state.message || '';

  const canEdit = STOPPED_PHASES.has(state.phase);
  const busy = BUSY_PHASES.has(state.phase);
  const running = state.phase === 'starting' || state.phase === 'ready';

  els.proxyPort.disabled = !canEdit;
  els.webPort.disabled = !canEdit;
  els.start.disabled = !canEdit;
  els.stop.disabled = !running;
  els.restart.disabled = busy || state.webPort == null;

  if (state.phase === 'error' || state.phase === 'crashed') {
    els.logs.open = true;
  }
}

function readPorts() {
  return { proxyPort: els.proxyPort.value, webPort: els.webPort.value };
}

async function main() {
  const defaults = await window.desktopApi.getDefaults();
  els.proxyPort.value = defaults.proxyPort;
  els.webPort.value = defaults.webPort;

  const state = await window.desktopApi.getState();
  if (state.logLines && state.logLines.length) {
    els.logOutput.textContent = state.logLines.join('\n') + '\n';
    els.logOutput.scrollTop = els.logOutput.scrollHeight;
  }
  if (state.proxyPort != null) els.proxyPort.value = state.proxyPort;
  if (state.webPort != null) els.webPort.value = state.webPort;
  render(state);

  els.start.addEventListener('click', () => {
    // 失败原因由主进程推进状态里，无需在这里回显
    window.desktopApi.start(readPorts());
  });
  els.stop.addEventListener('click', () => window.desktopApi.stop());
  els.restart.addEventListener('click', () => window.desktopApi.restart());

  window.desktopApi.onState(render);
  window.desktopApi.onLog((lines) => appendLog(lines.join('\n') + '\n'));
}

main();