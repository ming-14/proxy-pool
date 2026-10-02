// 确保 electron 二进制已下载。
//
// npm 11 默认拦截「依赖」的 install 脚本，electron 的二进制下载因而不会执行
// （dist/electron.exe 缺失，npm start 会直接失败）。项目自身的 postinstall 不受
// 该拦截影响，故在这里补跑 electron 自带的 install.js；镜像走 npmmirror，
// 避免直连 GitHub 超时。
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { DESKTOP_DIR, log } from './util.mjs';

const electronDir = path.join(DESKTOP_DIR, 'node_modules', 'electron');
const electronExe = path.join(electronDir, 'dist', 'electron.exe');

if (!fs.existsSync(electronDir)) {
  log('未安装 electron（node_modules 缺失），跳过');
  process.exit(0);
}
if (fs.existsSync(electronExe)) {
  log('electron 二进制已就绪');
  process.exit(0);
}

log('下载 electron 二进制…');
const env = { ...process.env };
env.ELECTRON_MIRROR ??= 'https://npmmirror.com/mirrors/electron/';
const result = spawnSync(process.execPath, [path.join(electronDir, 'install.js')], {
  cwd: electronDir,
  stdio: 'inherit',
  windowsHide: true,
  env,
});
if (result.status !== 0 || !fs.existsSync(electronExe)) {
  console.error('[desktop] electron 二进制下载失败，请检查网络或手动设置 ELECTRON_MIRROR');
  process.exit(1);
}
log('electron 二进制就绪');