// 用系统 Python 把 pip 引导进内嵌运行时，再由内嵌解释器安装依赖并自检。
//
// 依赖交给内嵌解释器自己的 pip 解析：它天然取 cp311/win_amd64 的正确 wheel，
// 无需 --platform/--abi 之类的交叉安装参数（避免选错轮子）。
import fs from 'node:fs';
import path from 'node:path';
import { DESKTOP_DIR, PIP_INDEX, RUNTIME_DIR, SITE_PACKAGES, log, run, runMain } from './util.mjs';

const HOST_PYTHON = process.env.PYTHON || 'python';

export async function main() {
  const python = path.join(RUNTIME_DIR, 'python.exe');
  if (!fs.existsSync(python)) {
    throw new Error('缺少内嵌 Python 运行时，请先运行 npm run prepare:runtime');
  }
  const lock = path.join(DESKTOP_DIR, 'requirements-desktop.lock');
  if (!fs.existsSync(lock)) throw new Error(`缺少依赖清单 ${lock}`);

  log(`引导 pip 到内嵌运行时（用 ${HOST_PYTHON}）…`);
  // --no-compile/-B：pip 装包时会另起 compileall 生成 .pyc，-B 拦不住，必须显式关闭；
  // 运行时启动带 -B，本就不需要字节码缓存，产物保持干净确定
  await run(HOST_PYTHON, [
    '-B', '-m', 'pip', 'install', '--quiet', '--no-compile', '--target', SITE_PACKAGES,
    'pip', '-i', PIP_INDEX, '--no-warn-script-location',
  ]);

  log('安装依赖（仅二进制 wheel）…');
  // -s：不加载宿主机的 user site-packages（site 会按 APPDATA 自动挂上宿主目录，
  // 不隔离的话 pip 会认为依赖"已满足"而装到宿主机上，产物就不自包含了）
  await run(python, [
    '-s', '-B', '-m', 'pip', 'install', '--only-binary=:all:', '--no-compile',
    '--no-warn-script-location', '-r', lock, '-i', PIP_INDEX,
  ]);

  log('运行自检…');
  // -B：自检会 import vendor 代码，不写 pyc 以免把 __pycache__ 带进产物
  await run(python, ['-s', '-B', path.join(DESKTOP_DIR, 'scripts', 'smoke.py')]);
  log('依赖安装完成并通过自检');
}

runMain(import.meta.url, main);