const {
  app, BrowserWindow, Menu, ipcMain, dialog, screen, globalShortcut,
} = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
  process.exit(0);
}

// The island window is a fixed transparent canvas hanging from the top edge of
// the screen. The visible black shape inside it grows out of the camera notch;
// everything around it is click-through.
const CANVAS_W = 680;
const CANVAS_H = 660;
const HOTKEY = process.env.JARVIS_HOTKEY || 'Alt+Space';

let win = null;
let pythonProc = null;
let quitting = false;
let currentState = 'offline';
let interactive = false;

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

// ── Notch geometry ────────────────────────────────────────────────────────────

function targetDisplay() {
  // The built-in panel is the one with a notch; fall back to the primary.
  const all = screen.getAllDisplays();
  return all.find((d) => d.internal) || screen.getPrimaryDisplay();
}

function notchGeometry(display) {
  // Notched MacBooks report a ~37–38pt menu bar; classic displays ~24pt.
  const menuBar = Math.max(0, display.workArea.y - display.bounds.y) || 24;
  const hasNotch = menuBar >= 32;
  const envWidth = Number(process.env.JARVIS_NOTCH_WIDTH);
  const width = envWidth > 0 ? envWidth : (hasNotch ? 186 : 160);
  // Without a hardware notch the island draws its own, a touch taller than
  // the menu bar so it still reads as a shape rather than a black stripe.
  const height = hasNotch ? menuBar : Math.max(menuBar + 8, 32);
  return { width, height, hasNotch };
}

function placeWindow() {
  if (!win) return;
  const display = targetDisplay();
  const { bounds } = display;
  const x = Math.round(bounds.x + bounds.width / 2 - CANVAS_W / 2);
  win.setBounds({ x, y: bounds.y, width: CANVAS_W, height: CANVAS_H }, false);
  win.webContents.send('geometry', notchGeometry(display));
}

function setInteractive(on) {
  if (!win || interactive === on) return;
  interactive = on;
  if (on) {
    win.setIgnoreMouseEvents(false);
  } else {
    win.setIgnoreMouseEvents(true, { forward: true });
  }
}

function createIsland() {
  if (win) return;

  win = new BrowserWindow({
    width: CANVAS_W,
    height: CANVAS_H,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    hasShadow: false,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    focusable: true,
    roundedCorners: false,
    enableLargerThanScreen: true,
    type: process.platform === 'darwin' ? 'panel' : 'toolbar',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });

  // Above the menu bar (and full-screen apps), on every Space.
  win.setAlwaysOnTop(true, 'screen-saver');
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
  win.setIgnoreMouseEvents(true, { forward: true });

  win.loadFile(path.join(__dirname, 'island.html'));
  win.webContents.on('did-finish-load', () => {
    placeWindow();
    win.showInactive();
    // macOS may nudge a new window below the menu bar; pin it back to the edge.
    placeWindow();
  });
  win.on('blur', () => {
    win?.webContents.send('window-blur');
  });
  win.on('closed', () => {
    win = null;
  });
}

function showContextMenu() {
  const labels = {
    idle: 'Standby',
    listening: 'Listening',
    thinking: 'Thinking',
    speaking: 'Speaking',
    offline: 'Connecting',
  };
  const menu = Menu.buildFromTemplate([
    { label: `Jarvis — ${labels[currentState] || 'Standby'}`, enabled: false },
    { type: 'separator' },
    { label: 'Talk to Jarvis', accelerator: HOTKEY, click: () => win?.webContents.send('hotkey') },
    { label: 'Type a request…', click: () => win?.webContents.send('compose') },
    { type: 'separator' },
    { label: 'Quit Jarvis', click: quitApp },
  ]);
  menu.popup({ window: win });
}

ipcMain.on('jarvis-state', (_e, state) => { currentState = state || 'idle'; });
ipcMain.on('interactive', (_e, on) => setInteractive(Boolean(on)));
ipcMain.on('focus-input', () => {
  if (!win) return;
  setInteractive(true);
  app.focus({ steal: true });
  win.focus();
});
ipcMain.on('context-menu', showContextMenu);
ipcMain.on('quit', quitApp);

app.on('second-instance', () => win?.webContents.send('compose'));

app.whenReady().then(() => {
  if (process.platform === 'darwin') app.dock.hide();
  createIsland();
  startPython();

  if (!globalShortcut.register(HOTKEY, () => win?.webContents.send('hotkey'))) {
    console.warn(`[island] Could not register ${HOTKEY} — another app owns it`);
  }

  for (const evt of ['display-added', 'display-removed', 'display-metrics-changed']) {
    screen.on(evt, placeWindow);
  }
});

app.on('will-quit', () => globalShortcut.unregisterAll());

app.on('window-all-closed', () => {
  // The island is the whole UI — stay alive if the window is ever torn down.
});

app.on('activate', () => {
  if (!app.isReady()) return;
  if (!win) createIsland();
  win?.webContents.send('compose');
});

app.on('before-quit', (e) => {
  if (quitting) return;
  e.preventDefault();
  quitApp();
});
