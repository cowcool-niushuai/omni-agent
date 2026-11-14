'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('omniSplash', {
  retry: () => ipcRenderer.invoke('omni:retry'),
  openLogs: () => ipcRenderer.invoke('omni:open-logs'),
  quit: () => ipcRenderer.invoke('omni:quit'),
});
