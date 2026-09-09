const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('jarvis', {
  setState: (state) => ipcRenderer.send('jarvis-state', state),
  quit: () => ipcRenderer.send('quit'),
});
