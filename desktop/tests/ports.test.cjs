'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const net = require('node:net');

const { parsePort, validateProxyPort, findFreePort } = require('../src/ports');

// 绑一下 port，能绑上即视为空闲
function canBind(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once('error', () => resolve(false));
    server.listen(port, '127.0.0.1', () => server.close(() => resolve(true)));
  });
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
  const server = net.createServer();
  await new Promise((resolve) => server.listen(taken, '127.0.0.1', resolve));
  try {
    assert.notEqual(await findFreePort(taken), taken);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});
