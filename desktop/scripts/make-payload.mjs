// 从工作区组装 Python payload（staging/pyapp）：cli.py + src + vendor + config。
//
// 必须从工作区复制而非 git archive：前端产物（src/framework/web/static）与
// vendor/geoip 都被 .gitignore 忽略，且 .gitattributes 有 eol 规则会破坏 mmdb。
import fs from 'node:fs';
import path from 'node:path';
import { PROJECT_ROOT, PYAPP_DIR, log, rmrf, runMain } from './util.mjs';

const EXCLUDE_DIR_NAMES = new Set(['__pycache__', '.pytest_cache', '.ruff_cache']);
const EXCLUDE_EXTS = new Set(['.pyc', '.pyo']);

function keep(source) {
  const name = path.basename(source);
  if (EXCLUDE_DIR_NAMES.has(name)) return false;
  if (EXCLUDE_EXTS.has(path.extname(name))) return false;
  return true;
}

function copyInto(sourceRel, destRel) {
  const from = path.join(PROJECT_ROOT, sourceRel);
  if (!fs.existsSync(from)) throw new Error(`缺少 ${sourceRel}`);
  const to = path.join(PYAPP_DIR, destRel);
  fs.mkdirSync(path.dirname(to), { recursive: true });
  fs.cpSync(from, to, { recursive: true, filter: keep });
  log(`已复制 ${sourceRel} -> pyapp/${destRel}`);
}

export function main() {
  // 前端产物不进版本库，必须由 frontend 构建；缺失时打出的是没有面板的空壳
  const staticIndex = path.join(PROJECT_ROOT, 'src', 'framework', 'web', 'static', 'index.html');
  if (!fs.existsSync(staticIndex)) {
    throw new Error('未找到前端产物 src/framework/web/static/index.html；请先 cd frontend && npm run build');
  }

  rmrf(PYAPP_DIR);
  fs.mkdirSync(PYAPP_DIR, { recursive: true });
  copyInto('cli.py', 'cli.py');
  copyInto('src', 'src');
  copyInto('vendor', 'vendor');
  copyInto('config', 'config');

  const envFile = path.join(PROJECT_ROOT, 'config', '.env');
  if (fs.existsSync(envFile)) {
    log('已包含 config/.env（含密钥，分发该文件夹等于分发密钥，外发前请移除）');
  } else {
    log('未发现 config/.env（纯净度检查等密钥需另行提供）');
  }
}

runMain(import.meta.url, main);