'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { parsePort, validatePorts } = require('../src/ports');

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

test('validatePorts 通过合法输入并规范化', () => {
  assert.deepEqual(validatePorts('8082', '8083'), { ok: true, proxyPort: 8082, webPort: 8083 });
  assert.deepEqual(validatePorts(1, 65535), { ok: true, proxyPort: 1, webPort: 65535 });
});

test('validatePorts 拒绝越界', () => {
  assert.equal(validatePorts('0', '8083').ok, false);
  assert.equal(validatePorts('65536', '8083').ok, false);
  assert.equal(validatePorts('8082', '99999').ok, false);
});

test('validatePorts 拒绝相同端口', () => {
  const result = validatePorts('8082', '8082');
  assert.equal(result.ok, false);
  assert.match(result.error, /不能相同/);
});

test('validatePorts 报错信息区分两个字段', () => {
  assert.match(validatePorts('abc', '8083').error, /代理端口/);
  assert.match(validatePorts('8082', 'abc').error, /网页服务器端口/);
});