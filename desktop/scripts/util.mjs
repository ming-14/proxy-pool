// 构建期共用工具：路径常量、子进程调用、下载与解压。
// 仅供 scripts/ 使用（Node 侧构建工具，不进入产物）。
import { spawn, execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const DESKTOP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const PROJECT_ROOT = path.resolve(DESKTOP_DIR, '..');
export const STAGING_DIR = path.join(DESKTOP_DIR, 'staging');
export const CACHE_DIR = path.join(STAGING_DIR, 'cache');
export const RUNTIME_DIR = path.join(STAGING_DIR, 'runtime');
export const PYAPP_DIR = path.join(STAGING_DIR, 'pyapp');
export const SITE_PACKAGES = path.join(RUNTIME_DIR, 'Lib', 'site-packages');

// 国内镜像：pypi 直连在大陆网络下经常超时
export const PIP_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple';

export function log(message) {
  console.log(`[desktop] ${message}`);
}

export function rmrf(target) {
  fs.rmSync(target, { recursive: true, force: true });
}

/** 前台执行命令，输出直接透传到当前终端 */
export function run(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { stdio: 'inherit', windowsHide: true, ...opts });
    child.on('error', reject);
    child.on('close', (code) => {
      if (code === 0) resolve();
      else reject(new Error(`${cmd} ${args.join(' ')} 退出码 ${code}`));
    });
  });
}

/** 执行命令并捕获 stdout（用于版本探测等） */
export function capture(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { windowsHide: true, ...opts }, (err, stdout) => {
      if (err) reject(err);
      else resolve({ stdout, stderr: '' });
    });
  });
}

/** 依次尝试多个镜像下载到 dest；已存在且非空则复用缓存 */
export async function downloadFirst(urls, dest) {
  if (fs.existsSync(dest) && fs.statSync(dest).size > 0) {
    log(`复用缓存 ${path.basename(dest)}`);
    return dest;
  }
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  const tmp = `${dest}.part`;
  let lastError = null;
  for (const url of urls) {
    try {
      log(`下载 ${url}`);
      const res = await fetch(url, { redirect: 'follow', signal: AbortSignal.timeout(300000) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const buffer = Buffer.from(await res.arrayBuffer());
      fs.writeFileSync(tmp, buffer);
      fs.renameSync(tmp, dest);
      log(`已下载 ${(buffer.length / 1024 / 1024).toFixed(1)} MB`);
      return dest;
    } catch (e) {
      lastError = e;
      log(`下载失败：${e.message}`);
    }
  }
  throw new Error(`所有镜像均下载失败：${lastError && lastError.message}`);
}

/** 解压 zip（Windows 自带 bsdtar 支持 zip，已实测） */
export async function extractZip(zip, dest) {
  await run('tar', ['-xf', zip, '-C', dest]);
}

/** 独立运行入口：被 prepare.mjs import 时不触发（metaUrl 传调用方的 import.meta.url） */
export function runMain(metaUrl, fn) {
  const entry = process.argv[1] ? path.resolve(process.argv[1]) : '';
  if (entry !== fileURLToPath(metaUrl)) return;
  Promise.resolve()
    .then(fn)
    .catch((e) => {
      console.error(`[desktop] 失败：${e && e.message ? e.message : e}`);
      process.exit(1);
    });
}