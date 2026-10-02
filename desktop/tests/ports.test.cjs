'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const net = require('node:net');

const { parsePort, validateProxyPort, isPortFree, findFreePort, recommendFreePort } = require('../src/ports');

// 绑一下 port，能绑上即视为空闲
function canBind(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once('error', () => resolve(false));
    server.listen(port, '127.0.0.1', () => server.close(() => resolve(true)));
  });
}

/** 占用 port 直到回调结束（返回释放函数） */
async function occupy(port) {
  const server = net.createServer();
  await new Promise((resolve) => server.listen(port, '127.0.0.1', resolve));
  return () => new Promise((resolve) => server.close(resolve));
}

/** 找一段连续空闲端口，便于构造「相邻端口可用」的确定性场景 */
async function findFreeRun(length, from = 18000) {
  for (let start = from; start < from + 2000; start += 1) {
    let free = true;
    for (let i = 0; i < length; i += 1) {
      if (!(await isPortFree(start + i))) {
        free = false;
        break;
      }
    }
    if (free) return start;
  }
  throw new Error('未找到连续空闲端口');
}

test('parsePort 接受纯数字与整数', () => {
  assert.equal(parsePort('8082'), 8082);
  assert.equal(parsePort(8082), 8082);
  assert.equal(parsePort(' 80 '), 80);
});

test('parsePort 拒绝非数字输入', () => {
  assert.ok(Number.isNaN(parsePort('')));
  assert.ok(Number.isNaN(parsePort('abc')));
  assert.ok(Number.isNaN(parsePort('80 82')));
  assert.ok(Number.isNaN(parsePort('8.5')));
  assert.ok(Number.isNaN(parsePort('-1')));
  assert.ok(Number.isNaN(parsePort(null)));
  assert.ok(Number.isNaN(parsePort(8082.5)));
});

test('validateProxyPort 通过合法输入并规范化', () => {
  assert.deepEqual(validateProxyPort('8082'), { ok: true, proxyPort: 8082 });
  assert.deepEqual(validateProxyPort(1), { ok: true, proxyPort: 1 });
  assert.deepEqual(validateProxyPort(65535), { ok: true, proxyPort: 65535 });
});

test('validateProxyPort 拒绝越界与非数字', () => {
  assert.equal(validateProxyPort('0').ok, false);
  assert.equal(validateProxyPort('65536').ok, false);
  assert.equal(validateProxyPort('abc').ok, false);
  assert.match(validateProxyPort('abc').error, /代理端口/);
});

test('findFreePort 返回一个当前可绑定的空闲端口', async () => {
  const port = await findFreePort();
  assert.ok(Number.isInteger(port) && port > 0 && port <= 65535);
  assert.equal(await canBind(port), true);
});

test('findFreePort 避开已占用的端口', async () => {
  const taken = await findFreePort();
  const release = await occupy(taken);
  try {
    assert.notEqual(await findFreePort(taken), taken);
  } finally {
    await release();
  }
});

test('isPortFree 区分空闲与占用', async () => {
  const port = await findFreePort();
  assert.equal(await isPortFree(port), true);

  const release = await occupy(port);
  try {
    assert.equal(await isPortFree(port), false);
  } finally {
    await release();
  }
});

test('recommendFreePort 优先取相邻的空闲端口', async () => {
  const base = await findFreeRun(2);
  const release = await occupy(base);
  try {
    assert.equal(await recommendFreePort(base), base + 1);
  } finally {
    await release();
  }
});

test('recommendFreePort 对任意被占用端口都给出可绑定且不相同的端口', async () => {
  const taken = await findFreePort();
  const release = await occupy(taken);
  try {
    const suggested = await recommendFreePort(taken);
    assert.notEqual(suggested, taken);
    assert.equal(await canBind(suggested), true);
  } finally {
    await release();
  }
});

test('recommendFreePort 对非法输入退回系统临时端口', async () => {
  const suggested = await recommendFreePort(NaN);
  assert.equal(await canBind(suggested), true);
});
