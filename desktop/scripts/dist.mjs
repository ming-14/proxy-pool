// 打包为免安装文件夹（electron-builder dir 目标）。
// electron-builder 在构建时会下载自身二进制（winCodeSign 等），同样走 npmmirror。
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { DESKTOP_DIR, log, runMain } from './util.mjs';

async function main() {
  const env = { ...process.env };
  env.ELECTRON_MIRROR ??= 'https://npmmirror.com/mirrors/electron/';
  env.ELECTRON_BUILDER_BINARIES_MIRROR ??= 'https://npmmirror.com/mirrors/electron-builder-binaries/';

  // 走 electron-builder 自带 CLI 入口，避免依赖 PATH 中的 shim
  const cli = path.join(DESKTOP_DIR, 'node_modules', 'electron-builder', 'out', 'cli', 'cli.js');
  log('开始打包（dir 目标）…');
  const result = spawnSync(process.execPath, [cli, '--win', '--dir'], {
    cwd: DESKTOP_DIR,
    stdio: 'inherit',
    windowsHide: true,
    env,
  });
  if (result.status !== 0) throw new Error(`electron-builder 退出码 ${result.status}`);
  log('打包完成：desktop/dist/win-unpacked（可整体重命名为 proxy-pool）');
}

runMain(import.meta.url, main);