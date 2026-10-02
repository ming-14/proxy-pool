// 下载官方 embeddable CPython 3.11 到 staging/runtime，并改写 _pth 启用 site-packages。
import fs from 'node:fs';
import path from 'node:path';
import { CACHE_DIR, RUNTIME_DIR, capture, downloadFirst, extractZip, log, rmrf, runMain } from './util.mjs';

const PY_VERSION = '3.11.9';
const ARCHIVE = `python-${PY_VERSION}-embed-amd64.zip`;
const MIRRORS = [
  `https://registry.npmmirror.com/-/binary/python/${PY_VERSION}/${ARCHIVE}`,
  `https://mirrors.huaweicloud.com/python/${PY_VERSION}/${ARCHIVE}`,
  `https://www.python.org/ftp/python/${PY_VERSION}/${ARCHIVE}`,
];

// _pth 的存在让解释器进入隔离模式：显式列出搜索路径并启用 site（否则 site-packages 不生效），
// 同时会忽略 PYTHON* 环境变量——故运行时靠命令行 -u -B -X utf8 而非环境变量。
const PTH_CONTENT = ['python311.zip', '.', 'Lib\\site-packages', 'import site', ''].join('\n');

const REQUIRED_FILES = ['python.exe', 'python311.zip', 'python311._pth', 'python311.dll', 'vcruntime140.dll', 'sqlite3.dll'];

export async function main() {
  const zip = await downloadFirst(MIRRORS, path.join(CACHE_DIR, ARCHIVE));
  rmrf(RUNTIME_DIR);
  fs.mkdirSync(RUNTIME_DIR, { recursive: true });
  await extractZip(zip, RUNTIME_DIR);

  fs.writeFileSync(path.join(RUNTIME_DIR, 'python311._pth'), PTH_CONTENT, 'utf8');
  fs.mkdirSync(path.join(RUNTIME_DIR, 'Lib', 'site-packages'), { recursive: true });

  for (const rel of REQUIRED_FILES) {
    if (!fs.existsSync(path.join(RUNTIME_DIR, rel))) {
      throw new Error(`内嵌运行时缺少 ${rel}（embeddable 包结构可能已变）`);
    }
  }

  const python = path.join(RUNTIME_DIR, 'python.exe');
  const { stdout } = await capture(python, ['-V']);
  log(`内嵌运行时就绪：${stdout.trim()} -> ${RUNTIME_DIR}`);
}

runMain(import.meta.url, main);