'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// 渲染层唯一入口：仅暴露必要的调用，保持 contextIsolation + sandbox
contextBridge.exposeInMainWorld('desktopApi', {
  getState: () => ipcRenderer.invoke('app:getState'),
  getDefaults: () => ipcRenderer.invoke('app:getDefaults'),
  start: (payload) => ipcRenderer.invoke('app:start', payload),
  stop: () => ipcRenderer.invoke('app:stop'),
  restart: () => ipcRenderer.invoke('app:restart'),
  onState: (cb) => {
    ipcRenderer.on('state', (_e, state) => cb(state));
  },
  onLog: (cb) => {
    ipcRenderer.on('log', (_e, lines) => cb(lines));
  },
});