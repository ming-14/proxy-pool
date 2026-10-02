'use strict';

/**
 * 端口工具（无 electron 依赖，便于单测）。
 *
 * 启动器只暴露代理端口，非法值必须在 spawn 之前拦下并给出明确原因；
 * 网页服务器端口是内部页面，由本模块挑一个当前空闲端口，不交给用户填。
 */

const net = require('net');

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
 * 校验代理端口并返回规范化结果。
 * @returns {{ok: true, proxyPort: number} | {ok: false, error: string}}
 */
function validateProxyPort(proxyPortInput) {
  const proxyPort = parsePort(proxyPortInput);
  if (!Number.isInteger(proxyPort) || proxyPort < MIN_PORT || proxyPort > MAX_PORT) {
    return { ok: false, error: `代理端口必须是 ${MIN_PORT}-${MAX_PORT} 之间的整数` };
  }
  return { ok: true, proxyPort };
}

/**
 * 取一个当前空闲的本地端口：绑 0 交给系统在临时端口段分配，读到端口号后立即释放。
 *
 * 面板是内部页面、地址不对外，用系统临时端口可避免与用户机器上的其它服务撞车。
 * 释放到子进程重新 bind 之间存在极短空窗（与 Vite/webpack-dev-server 同做法）；
 * 若真被抢占，子进程会 bind 失败，由就绪轮询给出超时错误。
 * @param {number} [exclude] 需要避开的端口（代理端口已占用，不能撞上）
 */
function findFreePort(exclude) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => {
        if (port === exclude) findFreePort(exclude).then(resolve, reject);
        else resolve(port);
      });
    });
  });
}

module.exports = { parsePort, validateProxyPort, findFreePort };
