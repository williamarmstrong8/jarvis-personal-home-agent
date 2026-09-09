#!/usr/bin/env node
/**
 * Build Jarvis.app and copy it to /Applications.
 * Does not add a login item — launch it yourself from Applications.
 */
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const BUILD = path.join(ROOT, 'build');
const VENV_PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

function run(cmd, args, opts = {}) {
  const result = spawnSync(cmd, args, {
    cwd: ROOT,
    stdio: 'inherit',
    ...opts,
  });
  if (result.status !== 0) {
    process.exit(result.status || 1);
  }
}

function main() {
  if (!fs.existsSync(VENV_PYTHON)) {
    console.error('Missing .venv — run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt');
    process.exit(1);
  }
  if (!fs.existsSync(path.join(ROOT, 'node_modules', 'electron'))) {
    console.error('Missing node_modules — run: npm install');
    process.exit(1);
  }

  fs.mkdirSync(BUILD, { recursive: true });
  fs.writeFileSync(path.join(BUILD, 'jarvis-home.txt'), ROOT + '\n', 'utf8');

  console.log('Generating app icon…');
  run(VENV_PYTHON, [path.join(ROOT, 'scripts', 'make-icon.py')]);
  run(VENV_PYTHON, [path.join(ROOT, 'scripts', 'make-tray-icons.py')]);

  const builder = path.join(ROOT, 'node_modules', '.bin', 'electron-builder');
  if (!fs.existsSync(builder)) {
    console.error('electron-builder is not installed. Run: npm install');
    process.exit(1);
  }

  const archFlag = process.arch === 'arm64' ? '--arm64' : '--x64';
  console.log('Packaging Jarvis.app…');
  run(builder, ['--mac', 'dir', archFlag]);

  const candidates = [
    path.join(ROOT, 'dist', `mac-${process.arch}`, 'Jarvis.app'),
    path.join(ROOT, 'dist', 'mac', 'Jarvis.app'),
  ];
  const built = candidates.find((p) => fs.existsSync(p));
  if (!built) {
    console.error('Build finished but Jarvis.app was not found in dist/');
    process.exit(1);
  }

  const dest = '/Applications/Jarvis.app';
  console.log(`Installing to ${dest}…`);
  if (fs.existsSync(dest)) {
    run('rm', ['-rf', dest]);
  }
  run('ditto', [built, dest]);
  spawnSync('xattr', ['-cr', dest]);

  console.log('');
  console.log('Jarvis lives in the menu bar (next to Wi‑Fi). It does not start at login.');
  console.log('Open it from Applications once — then click the dot for details.');
  console.log('Quit from the dropdown (or right-click the icon → Quit Jarvis).');
  console.log('');
  console.log('First launch: grant Microphone (and Screen Recording if asked).');
  console.log('If macOS refuses to open it: right-click Jarvis → Open.');
}

main();
