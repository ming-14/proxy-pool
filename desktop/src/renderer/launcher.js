'use strict';

/**
 * 启动器渲染逻辑：只负责「代理端口 + 启动」，外加启动失败提示。
 *
 * 失败必须落到界面上（托盘 tooltip 看不见）：端口被占用时同时给出一个
 * 当前可用的替代端口，点一下即可改用并重试。
 */

const els = {
  app: document.querySelector('.app'),
  proxyPort: document.getElementById('proxy-port'),
  start: document.getElementById('btn-start'),
  notice: document.getElementById('notice'),
  noticeText: document.getElementById('notice-text'),
  suggest: document.getElementById('btn-suggest'),
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

// 需要把 message 摆到界面上的失败态
const FAILED_PHASES = new Set(['error', 'crashed']);

// 服务在跑（或正在切换）时不允许改端口
const LOCKED_PHASES = new Set(['starting', 'ready', 'stopping']);

function render(state) {
  const [label, disabled] = BUTTON_STATE[state.phase] || ['启动', false];
  els.start.textContent = label;
  els.start.disabled = disabled;
  els.proxyPort.disabled = LOCKED_PHASES.has(state.phase);
  renderNotice(state);
}

function renderNotice(state) {
  const message = FAILED_PHASES.has(state.phase) && state.message ? state.message : '';
  els.notice.hidden = message === '';
  els.notice.title = message;
  els.noticeText.textContent = message;

  const suggested = Number.isInteger(state.suggestedProxyPort) ? state.suggestedProxyPort : null;
  els.suggest.hidden = suggested === null;
  if (suggested !== null) {
    els.suggest.dataset.port = String(suggested);
    els.suggest.textContent = `改用 ${suggested}`;
  }
}

function startService() {
  window.desktopApi.start({ proxyPort: els.proxyPort.value });
}

/** 窗口高度跟随内容：提示出现/消失时不留白、不裁切 */
function syncWindowHeight() {
  window.desktopApi.resizeLauncher(els.app.offsetHeight);
}

async function main() {
  const [defaults, state] = await Promise.all([
    window.desktopApi.getDefaults(),
    window.desktopApi.getState(),
  ]);
  els.proxyPort.value = state.proxyPort ?? defaults.proxyPort;
  render(state);
  new ResizeObserver(syncWindowHeight).observe(els.app);
  syncWindowHeight();

  els.start.addEventListener('click', startService);

  // 采纳推荐端口：填回输入框并直接重试，省掉一轮点击
  els.suggest.addEventListener('click', () => {
    els.proxyPort.value = els.suggest.dataset.port;
    startService();
  });

  window.desktopApi.onState(render);
}

main();