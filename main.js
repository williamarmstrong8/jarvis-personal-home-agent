const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

// ── Single-instance lock — kill any previous Electron instance ────────────────
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
  process.exit(0);
}

let win = null;
let pythonProc = null;
let quitting = false;

function jarvisHome() {
  if (!app.isPackaged) {
    return path.join(__dirname, '..');
  }
  const marker = path.join(process.resourcesPath, 'jarvis-home.txt');
  if (fs.existsSync(marker)) {
    const home = fs.readFileSync(marker, 'utf8').trim();
    if (home) return home;
  }
  return path.join(os.homedir(), 'Desktop', 'Jarvis 2.0');
}

function startPython() {
  if (!app.isPackaged) return;

  const home = jarvisHome();
  const python = path.join(home, '.venv', 'bin', 'python');
  const mainPy = path.join(home, 'main.py');

  if (!fs.existsSync(python) || !fs.existsSync(mainPy)) {
    dialog.showErrorBox(
      'Jarvis',
      `Can't find the Jarvis project.\n\nLooked in:\n${home}\n\nFrom the Jarvis 2.0 folder run:\nnpm run install:app`,
    );
    app.quit();
    return;
  }

  const logDir = path.join(home, 'data', 'logs');
  fs.mkdirSync(logDir, { recursive: true });
  const logFd = fs.openSync(path.join(logDir, 'app.log'), 'a');

  const pathParts = (process.env.PATH || '/usr/bin:/bin:/usr/sbin:/sbin').split(':');
  for (const extra of ['/opt/homebrew/bin', '/usr/local/bin']) {
    if (!pathParts.includes(extra)) pathParts.unshift(extra);
  }

  pythonProc = spawn(python, [mainPy, '--no-electron'], {
    cwd: home,
    env: {
      ...process.env,
      PATH: pathParts.join(':'),
      PYTHONUNBUFFERED: '1',
      JARVIS_ELECTRON_PARENT: '1',
    },
    stdio: ['ignore', logFd, logFd],
  });

  pythonProc.on('exit', (code, signal) => {
    pythonProc = null;
    if (!quitting) {
      dialog.showErrorBox(
        'Jarvis',
        `The voice engine stopped (${signal || code}).\nSee data/logs/app.log in the Jarvis 2.0 folder.`,
      );
      app.quit();
    }
  });
}

function stopPython() {
  return new Promise((resolve) => {
    if (!pythonProc) {
      resolve();
      return;
    }
    const proc = pythonProc;
    const timer = setTimeout(() => {
      try { proc.kill('SIGKILL'); } catch (_) { /* already gone */ }
      resolve();
    }, 2500);
    proc.once('exit', () => {
      clearTimeout(timer);
      resolve();
    });
    try {
      proc.kill('SIGTERM');
    } catch (_) {
      clearTimeout(timer);
      resolve();
    }
  });
}

function createWindow() {
  if (!app.isReady()) return;
  if (win) return;

  const { screen } = require('electron');
  const { width: screenWidth } = screen.getPrimaryDisplay().workAreaSize;

  const WIN_W  = 200;
  const WIN_H  = 200;
  const MARGIN = 20;

  const xPos = screenWidth - WIN_W - MARGIN;
  const yPos = MARGIN;

  win = new BrowserWindow({
    width:           WIN_W,
    height:          WIN_H,
    x:               xPos,
    y:               yPos,
    frame:           false,
    transparent:     true,
    alwaysOnTop:     true,
    show:            false,
    resizable:       false,
    movable:         true,
    focusable:       true,
    acceptFirstMouse: true,
    skipTaskbar:     false,
    hasShadow:       true,
    webPreferences: {
      preload:          path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration:  false,
    },
  });

  win.loadFile(path.join(__dirname, 'index.html'));

  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.setAlwaysOnTop(true, 'floating');

  win.once('ready-to-show', () => {
    win.showInactive();
  });

  win.webContents.on('did-fail-load', (e, code, desc) => {
    console.error('[Electron] Failed to load:', code, desc);
  });
  win.webContents.on('crashed', () => {
    console.error('[Electron] Renderer process crashed');
  });
  win.webContents.on('console-message', (e, level, msg) => {
    if (level >= 2) console.error('[Renderer]', msg);
  });

  ipcMain.on('show-window', () => {
    if (win) win.showInactive();
  });

  ipcMain.on('hide-window', () => {
    if (win) win.showInactive();
  });

  let dragOffset = null;
  let dragTimer  = null;

  function stopDrag() {
    if (dragTimer) clearInterval(dragTimer);
    dragTimer  = null;
    dragOffset = null;
  }

  ipcMain.on('drag-start', (_e, screenX, screenY) => {
    if (!win) return;
    const [x, y] = win.getPosition();
    dragOffset = { x: screenX - x, y: screenY - y };
    if (dragTimer) clearInterval(dragTimer);
    dragTimer = setInterval(() => {
      if (!win || !dragOffset) return;
      const { screen } = require('electron');
      const pos = screen.getCursorScreenPoint();
      win.setPosition(
        Math.round(pos.x - dragOffset.x),
        Math.round(pos.y - dragOffset.y),
      );
    }, 16);
  });

  ipcMain.on('drag-end', stopDrag);
  win.on('blur', stopDrag);
}

app.on('second-instance', () => {
  if (win) {
    if (win.isMinimized()) win.restore();
    win.showInactive();
  }
});

app.whenReady().then(() => {
  createWindow();
  startPython();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (!app.isReady()) return;
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

app.on('before-quit', (e) => {
  if (quitting || !pythonProc) return;
  e.preventDefault();
  quitting = true;
  stopPython().then(() => app.quit());
});
