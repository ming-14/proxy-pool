'use strict';

/** 启动器渲染逻辑：只负责「代理端口 + 启动」。状态与日志由托盘菜单承载。 */

const els = {
  proxyPort: document.getElementById('proxy-port'),
  start: document.getElementById('btn-start'),
};

// phase -> [按钮文案, 按钮是否禁用]
const BUTTON_STATE = {
  idle: ['启动', false],
  starting: ['启动中…', true],
  ready: ['运行中', true],
  stopping: ['停止中…', true],
  stopped: ['启动', false],
  error: ['启动', false],
  crashed: ['启动', false],
};

// 服务在跑（或正在切换）时不允许改端口
const LOCKED_PHASES = new Set(['starting', 'ready', 'stopping']);

function render(state) {
  const [label, disabled] = BUTTON_STATE[state.phase] || ['启动', false];
  els.start.textContent = label;
  els.start.disabled = disabled;
  els.proxyPort.disabled = LOCKED_PHASES.has(state.phase);
}

async function main() {
  const [defaults, state] = await Promise.all([
    window.desktopApi.getDefaults(),
    window.desktopApi.getState(),
  ]);
  els.proxyPort.value = state.proxyPort ?? defaults.proxyPort;
  render(state);

  els.start.addEventListener('click', () => {
    window.desktopApi.start({ proxyPort: els.proxyPort.value });
  });
  window.desktopApi.onState(render);
}

main();
