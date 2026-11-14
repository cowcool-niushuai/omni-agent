'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('omniDesktop', {
  isDesktop: true,
  pickFolder: () => ipcRenderer.invoke('omni:pick-folder'),
});
