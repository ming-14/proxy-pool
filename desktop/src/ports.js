'use strict';

/**
 * 端口校验（纯函数，无 electron 依赖，便于单测）。
 * 启动器只暴露这两个端口，非法值必须在 spawn 之前拦下并给出明确原因。
 */

const MIN_PORT = 1;
const MAX_PORT = 65535;

/** 解析端口输入：仅接受纯十进制整数串/整数，其余返回 NaN */
function parsePort(value) {
  if (typeof value === 'number') {
    return Number.isInteger(value) ? value : NaN;
  }
  const text = String(value == null ? '' : value).trim();
  if (!/^\d+$/.test(text)) return NaN;
  return Number(text);
}

/**
 * 校验两个端口并返回规范化结果。
 * @returns {{ok: true, proxyPort: number, webPort: number} | {ok: false, error: string}}
 */
function validatePorts(proxyPortInput, webPortInput) {
  const proxyPort = parsePort(proxyPortInput);
  const webPort = parsePort(webPortInput);
  if (!Number.isInteger(proxyPort) || proxyPort < MIN_PORT || proxyPort > MAX_PORT) {
    return { ok: false, error: `代理端口必须是 ${MIN_PORT}-${MAX_PORT} 之间的整数` };
  }
  if (!Number.isInteger(webPort) || webPort < MIN_PORT || webPort > MAX_PORT) {
    return { ok: false, error: `网页服务器端口必须是 ${MIN_PORT}-${MAX_PORT} 之间的整数` };
  }
  if (proxyPort === webPort) {
    return { ok: false, error: '代理端口与网页服务器端口不能相同' };
  }
  return { ok: true, proxyPort, webPort };
}

module.exports = { parsePort, validatePorts };