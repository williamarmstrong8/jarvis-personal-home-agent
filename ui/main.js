const { app, BrowserWindow, Tray, Menu, ipcMain, dialog, nativeImage } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
  process.exit(0);
}

let win = null;
let tray = null;
let pythonProc = null;
let quitting = false;
let currentState = 'offline';

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

function quitApp() {
  if (quitting) {
    app.quit();
    return;
  }
  quitting = true;
  stopPython().then(() => app.quit());
}

function trayIcon(state) {
  const file = path.join(__dirname, 'tray', `${state}.png`);
  const fallback = path.join(__dirname, 'tray', 'idle.png');
  const img = nativeImage.createFromPath(fs.existsSync(file) ? file : fallback);
  img.setTemplateImage(false);
  return img;
}

function setTrayState(state) {
  currentState = state || 'idle';
  if (!tray) return;
  tray.setImage(trayIcon(currentState));
  const labels = {
    idle: 'Standby',
    listening: 'Listening',
    thinking: 'Thinking',
    speaking: 'Speaking',
    offline: 'Connecting',
  };
  tray.setToolTip(`Jarvis · ${labels[currentState] || 'Standby'}`);
}

function positionPanel() {
  if (!win || !tray) return;
  const { screen } = require('electron');
  const trayBounds = tray.getBounds();
  const { width } = win.getBounds();
  const display = screen.getDisplayNearestPoint({ x: trayBounds.x, y: trayBounds.y });
  const area = display.workArea;
  let x = Math.round(trayBounds.x + trayBounds.width / 2 - width / 2);
  const y = Math.round(trayBounds.y + trayBounds.height + 6);
  x = Math.min(Math.max(area.x + 6, x), area.x + area.width - width - 6);
  win.setPosition(x, y, false);
}

function togglePanel() {
  if (!app.isReady()) return;
  if (!win) createPanel();
  if (!win) return;
  if (win.isVisible()) {
    win.hide();
    return;
  }
  positionPanel();
  win.show();
  win.focus();
}

function createPanel() {
  if (!app.isReady()) return;
  if (win) return;

  win = new BrowserWindow({
    width: 300,
    height: 320,
    show: false,
    frame: false,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    hasShadow: true,
    focusable: true,
    type: process.platform === 'darwin' ? 'panel' : 'normal',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.loadFile(path.join(__dirname, 'panel.html'));

  win.on('blur', () => {
    if (!quitting) win.hide();
  });
  win.on('closed', () => {
    win = null;
  });
}

function createTray() {
  tray = new Tray(trayIcon('offline'));
  tray.setIgnoreDoubleClickEvents(true);
  setTrayState('offline');

  tray.on('click', togglePanel);
  tray.on('right-click', () => {
    const menu = Menu.buildFromTemplate([
      { label: `Jarvis — ${currentState}`, enabled: false },
      { type: 'separator' },
      { label: 'Show details', click: () => { if (win?.isVisible()) return; togglePanel(); } },
      { type: 'separator' },
      { label: 'Quit Jarvis', click: quitApp },
    ]);
    tray.popUpContextMenu(menu);
  });
}

ipcMain.on('jarvis-state', (_e, state) => setTrayState(state));
ipcMain.on('quit', quitApp);

app.on('second-instance', () => togglePanel());

app.whenReady().then(() => {
  if (process.platform === 'darwin') app.dock.hide();
  createTray();
  createPanel();
  startPython();
});

app.on('window-all-closed', () => {
  // Menu bar extra — stay running when the dropdown hides.
});

app.on('activate', () => {
  if (!app.isReady()) return;
  togglePanel();
});

app.on('before-quit', (e) => {
  if (quitting) return;
  e.preventDefault();
  quitApp();
});
