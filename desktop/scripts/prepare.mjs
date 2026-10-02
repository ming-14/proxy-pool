// 一键构建：内嵌 Python 运行时 -> pyapp payload -> 安装依赖并自检。
// 顺序上 payload 先于依赖安装：自检需要从 pyapp 里 import pproxy/pspeed。
import { log, runMain } from './util.mjs';
import { main as fetchPython } from './fetch-python.mjs';
import { main as makePayload } from './make-payload.mjs';
import { main as installDeps } from './install-deps.mjs';

async function main() {
  log('1/3 准备内嵌 Python 3.11 运行时…');
  await fetchPython();
  log('2/3 组装 Python payload…');
  makePayload();
  log('3/3 安装依赖并自检…');
  await installDeps();
  log('完成：可执行 npm start 开发运行，或 npm run dist 打包');
}

runMain(import.meta.url, main);