'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// 渲染层唯一入口：仅暴露必要的调用，保持 contextIsolation + sandbox
contextBridge.exposeInMainWorld('desktopApi', {
  getState: () => ipcRenderer.invoke('app:getState'),
  getDefaults: () => ipcRenderer.invoke('app:getDefaults'),
  start: (payload) => ipcRenderer.invoke('app:start', payload),
  onState: (cb) => {
    ipcRenderer.on('state', (_e, state) => cb(state));
  },
});
