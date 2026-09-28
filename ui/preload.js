const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('jarvis', {
  setState: (state) => ipcRenderer.send('jarvis-state', state),
  setInteractive: (on) => ipcRenderer.send('interactive', on),
  focusInput: () => ipcRenderer.send('focus-input'),
  contextMenu: () => ipcRenderer.send('context-menu'),
  quit: () => ipcRenderer.send('quit'),
  on: (channel, fn) => {
    if (!['geometry', 'hotkey', 'compose', 'window-blur'].includes(channel)) return;
    ipcRenderer.on(channel, (_e, payload) => fn(payload));
  },
});
