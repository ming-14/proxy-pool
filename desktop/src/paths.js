'use strict';

const path = require('path');
const { app } = require('electron');

// desktop/ 目录（本文件在 desktop/src/ 下）
const DESKTOP_DIR = path.resolve(__dirname, '..');
// 仓库根（desktop/ 的上一级）——开发态 staging 由此处源码组装
const PROJECT_ROOT = path.resolve(DESKTOP_DIR, '..');

/**
 * 解析运行时路径。
 *
 * 打包后（免安装文件夹 dist/win-unpacked/，交付时可整体改名）：
 *   python.exe / pyapp 在 resources/ 下（extraResources 投放，只读语义）；
 *   运行数据（.cache.db、logs/）落在 exe 同级目录，便于用户查看与迁移。
 * 开发态：python 与 pyapp 取 desktop/staging，数据落在 desktop/.dev/data。
 */
function resolvePaths() {
  if (app.isPackaged) {
    const resourcesDir = process.resourcesPath;
    return {
      packaged: true,
      pythonExe: path.join(resourcesDir, 'python', 'python.exe'),
      pyappDir: path.join(resourcesDir, 'pyapp'),
      dataDir: path.dirname(process.execPath),
    };
  }
  return {
    packaged: false,
    pythonExe: path.join(DESKTOP_DIR, 'staging', 'runtime', 'python.exe'),
    pyappDir: path.join(DESKTOP_DIR, 'staging', 'pyapp'),
    dataDir: path.join(DESKTOP_DIR, '.dev', 'data'),
  };
}

module.exports = { resolvePaths, DESKTOP_DIR, PROJECT_ROOT };