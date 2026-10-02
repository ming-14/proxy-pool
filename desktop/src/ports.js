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
const HOST = '127.0.0.1';
/** 推荐端口的邻近探测步数，超出则退回系统临时端口 */
const RECOMMEND_SCAN_LIMIT = 20;

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

/** 试占端口：能 listen 说明当前空闲。探测失败一律视为「不可用」，不区分原因。 */
function isPortFree(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once('error', () => resolve(false));
    server.once('listening', () => server.close(() => resolve(true)));
    server.listen(port, HOST);
  });
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
    server.listen(0, HOST, () => {
      const { port } = server.address();
      server.close(() => {
        if (port === exclude) findFreePort(exclude).then(resolve, reject);
        else resolve(port);
      });
    });
  });
}

/**
 * 为已占用的端口推荐一个替代端口。
 *
 * 从 preferred 起向后顺序探测：代理端口会被用户写进浏览器/客户端配置，
 * 邻近端口（8082 → 8083）比系统临时端口（49152+）更可预期、更好记。
 * 邻近区间全部占用时退回 findFreePort，保证任何情况下都给得出建议。
 * @param {number} preferred 被占用的端口
 */
async function recommendFreePort(preferred) {
  if (!Number.isInteger(preferred)) return findFreePort();
  for (let offset = 1; offset <= RECOMMEND_SCAN_LIMIT; offset += 1) {
    const candidate = preferred + offset;
    if (candidate > MAX_PORT) break;
    if (await isPortFree(candidate)) return candidate;
  }
  return findFreePort(preferred);
}

module.exports = { parsePort, validateProxyPort, isPortFree, findFreePort, recommendFreePort };
