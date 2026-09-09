/**
 * J.A.R.V.I.S. — Renderer
 * Three.js 3D Globe + WebSocket state machine
 */

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 1 — Constants
// ═══════════════════════════════════════════════════════════════════════════
const WS_PORT      = 8765;
const WS_URL       = `ws://localhost:${WS_PORT}`;
const RECONNECT_MS = 2000;
const IDLE_HIDE_MS = 8000;

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 2 — DOM refs
// ═══════════════════════════════════════════════════════════════════════════
const body     = document.body;
const stateDot = document.getElementById('stateDot');

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 3 — Globe state parameters
// ═══════════════════════════════════════════════════════════════════════════
const STATE_PARAMS = {
  idle: {
    colorHex:      0xff2d2d,
    particleSpeed: 0.18,
    particleOp:    0.42,
    globeScale:    1.0,
    coreEmissive:  1.2,
    coreLight:     1.6,
    pulseAmp:      0.015,
    pulseFreq:     0.6,
    lerpSpeed:     0.02,
  },
  listening: {
    colorHex:      0x00ff88,
    particleSpeed: 0.55,
    particleOp:    0.72,
    globeScale:    1.06,
    coreEmissive:  1.8,
    coreLight:     2.4,
    pulseAmp:      0.03,
    pulseFreq:     1.2,
    lerpSpeed:     0.04,
  },
  thinking: {
    colorHex:      0xaa88ff,
    particleSpeed: 0.12,
    particleOp:    0.55,
    globeScale:    1.0,
    coreEmissive:  1.5,
    coreLight:     2.0,
    pulseAmp:      0.02,
    pulseFreq:     0.45,
    lerpSpeed:     0.025,
  },
  speaking: {
    colorHex:      0xff9500,
    particleSpeed: 0.72,
    particleOp:    0.78,
    globeScale:    1.08,
    coreEmissive:  2.0,
    coreLight:     2.6,
    pulseAmp:      0.035,
    pulseFreq:     1.4,
    lerpSpeed:     0.04,
  },
};

// Numeric lerp state — driven every frame toward target
const current = {
  particleSpeed: 0.4,
  particleOp:    0.55,
  globeScale:    1.0,
  coreEmissive:  1.5,
  coreLight:     2.0,
  pulseAmp:      0.03,
  pulseFreq:     1.0,
  lerpSpeed:     0.02,
};
const target = { ...current };

function lerpNum(a, b, t) { return a + (b - a) * t; }

function lerpState(cur, tgt) {
  const t = cur.lerpSpeed;
  cur.particleSpeed = lerpNum(cur.particleSpeed, tgt.particleSpeed, t);
  cur.particleOp    = lerpNum(cur.particleOp,    tgt.particleOp,    t);
  cur.globeScale    = lerpNum(cur.globeScale,     tgt.globeScale,    t);
  cur.coreEmissive  = lerpNum(cur.coreEmissive,   tgt.coreEmissive,  t);
  cur.coreLight     = lerpNum(cur.coreLight,       tgt.coreLight,     t);
  cur.pulseAmp      = lerpNum(cur.pulseAmp,        tgt.pulseAmp,      t);
  cur.pulseFreq     = lerpNum(cur.pulseFreq,       tgt.pulseFreq,     t);
  cur.lerpSpeed     = lerpNum(cur.lerpSpeed,       tgt.lerpSpeed,     t);
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 4 — Three.js scene
// ═══════════════════════════════════════════════════════════════════════════
let scene, camera, renderer3d, clock;
let innerCore, midShell, outerShell;
let particleMesh, streakMesh;
let particlePositions, particlePhases, particleRadii, particleSpeeds;
let streakPositions,   streakPhases,   streakRadii,   streakSpeeds;
let circuitLines = [];
let coreLight, rimLight;
let currentColor, targetColor;

const PARTICLE_COUNT = 3000;
const STREAK_COUNT   = 800;

function initGlobe() {
  const canvas = document.getElementById('globeCanvas');

  // ── Renderer ──────────────────────────────────────────────────────────
  renderer3d = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true,
    powerPreference: 'high-performance' });
  renderer3d.setSize(window.innerWidth, window.innerHeight);
  renderer3d.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer3d.setClearColor(0x000000, 0);

  // ── Scene & camera ────────────────────────────────────────────────────
  scene  = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(50, window.innerWidth / window.innerHeight, 0.1, 1000);
  camera.position.z = 3.4;

  clock  = new THREE.Clock();

  // ── Lights ────────────────────────────────────────────────────────────
  scene.add(new THREE.AmbientLight(0xffffff, 0.1));

  coreLight = new THREE.PointLight(0xff2d2d, 2.0, 8.0);
  coreLight.position.set(0, 0, 0);
  scene.add(coreLight);

  rimLight = new THREE.PointLight(0xff2d2d, 0.6, 6.0);
  rimLight.position.set(2, 1, 2);
  scene.add(rimLight);

  // ── Colors ────────────────────────────────────────────────────────────
  currentColor = new THREE.Color(0xff2d2d);
  targetColor  = new THREE.Color(0xff2d2d);

  // ── Inner core sphere ─────────────────────────────────────────────────
  const coreGeo = new THREE.SphereGeometry(0.35, 32, 32);
  const coreMat = new THREE.MeshStandardMaterial({
    color:             0xff2d2d,
    emissive:          new THREE.Color(0xff2d2d),
    emissiveIntensity: 1.5,
    transparent:       true,
    opacity:           0.92,
    roughness:         0.2,
    metalness:         0.8,
  });
  innerCore = new THREE.Mesh(coreGeo, coreMat);
  scene.add(innerCore);

  // ── Mid shell (icosahedron wireframe) ─────────────────────────────────
  const midGeo = new THREE.IcosahedronGeometry(0.55, 3);
  const midMat = new THREE.MeshBasicMaterial({
    color: 0xff2d2d, wireframe: true, transparent: true, opacity: 0.08,
  });
  midShell = new THREE.Mesh(midGeo, midMat);
  scene.add(midShell);

  // ── Outer shell (icosahedron wireframe) ───────────────────────────────
  const outerGeo = new THREE.IcosahedronGeometry(0.75, 2);
  const outerMat = new THREE.MeshBasicMaterial({
    color: 0xff2d2d, wireframe: true, transparent: true, opacity: 0.05,
  });
  outerShell = new THREE.Mesh(outerGeo, outerMat);
  scene.add(outerShell);

  // ── Primary particle cloud (3000) ─────────────────────────────────────
  const pGeo = new THREE.BufferGeometry();
  particlePositions = new Float32Array(PARTICLE_COUNT * 3);
  particlePhases    = new Float32Array(PARTICLE_COUNT);
  particleRadii     = new Float32Array(PARTICLE_COUNT);
  particleSpeeds    = new Float32Array(PARTICLE_COUNT);

  for (let i = 0; i < PARTICLE_COUNT; i++) {
    particlePhases[i]  = Math.random() * Math.PI * 2;
    particleRadii[i]   = 0.85 + Math.random() * 0.95;
    particleSpeeds[i]  = 0.3  + Math.random() * 0.7;
    // Initialise positions on sphere surface
    const th = particlePhases[i];
    const ph = Math.PI / 2 + (Math.random() - 0.5) * 2.2;
    const r  = particleRadii[i];
    particlePositions[i * 3]     = r * Math.sin(ph) * Math.cos(th);
    particlePositions[i * 3 + 1] = r * Math.cos(ph);
    particlePositions[i * 3 + 2] = r * Math.sin(ph) * Math.sin(th);
  }

  pGeo.setAttribute('position', new THREE.BufferAttribute(particlePositions, 3));

  const pMat = new THREE.PointsMaterial({
    color:       0xff2d2d,
    size:        0.014,
    sizeAttenuation: true,
    transparent: true,
    opacity:     0.55,
    blending:    THREE.AdditiveBlending,
    depthWrite:  false,
  });

  particleMesh = new THREE.Points(pGeo, pMat);
  scene.add(particleMesh);

  // ── Streak layer (800 faster/brighter particles) ──────────────────────
  const sGeo = new THREE.BufferGeometry();
  streakPositions = new Float32Array(STREAK_COUNT * 3);
  streakPhases    = new Float32Array(STREAK_COUNT);
  streakRadii     = new Float32Array(STREAK_COUNT);
  streakSpeeds    = new Float32Array(STREAK_COUNT);

  for (let i = 0; i < STREAK_COUNT; i++) {
    streakPhases[i]  = Math.random() * Math.PI * 2;
    streakRadii[i]   = 0.80 + Math.random() * 0.30;
    streakSpeeds[i]  = 0.8  + Math.random() * 1.2;
    const th = streakPhases[i];
    const ph = Math.PI / 2 + (Math.random() - 0.5) * 1.8;
    const r  = streakRadii[i];
    streakPositions[i * 3]     = r * Math.sin(ph) * Math.cos(th);
    streakPositions[i * 3 + 1] = r * Math.cos(ph);
    streakPositions[i * 3 + 2] = r * Math.sin(ph) * Math.sin(th);
  }

  sGeo.setAttribute('position', new THREE.BufferAttribute(streakPositions, 3));

  const sMat = new THREE.PointsMaterial({
    color:       0xff2d2d,
    size:        0.022,
    sizeAttenuation: true,
    transparent: true,
    opacity:     0.70,
    blending:    THREE.AdditiveBlending,
    depthWrite:  false,
  });

  streakMesh = new THREE.Points(sGeo, sMat);
  scene.add(streakMesh);

  // ── Circuit lines ─────────────────────────────────────────────────────
  buildCircuitLines(0xff2d2d);

  // ── Resize handler ────────────────────────────────────────────────────
  window.addEventListener('resize', () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer3d.setSize(window.innerWidth, window.innerHeight);
  });

  // ── Start animation loop ──────────────────────────────────────────────
  animate();
}

function buildCircuitLines(colorHex) {
  // Remove old lines
  circuitLines.forEach(l => scene.remove(l));
  circuitLines = [];

  const color = new THREE.Color(colorHex);
  const LINE_COUNT = 8;

  for (let i = 0; i < LINE_COUNT; i++) {
    const phi   = Math.random() * Math.PI;
    const theta = Math.random() * Math.PI * 2;

    // Start: on outer shell surface
    const startR = 0.82;
    const start  = new THREE.Vector3(
      startR * Math.sin(phi) * Math.cos(theta),
      startR * Math.cos(phi),
      startR * Math.sin(phi) * Math.sin(theta),
    );

    // End: further out
    const endR = 1.4 + Math.random() * 0.8;
    const end  = new THREE.Vector3(
      endR * Math.sin(phi) * Math.cos(theta),
      endR * Math.cos(phi),
      endR * Math.sin(phi) * Math.sin(theta),
    );

    // Mid: slight bend
    const mid = new THREE.Vector3().lerpVectors(start, end, 0.5);
    mid.x += (Math.random() - 0.5) * 0.3;
    mid.y += (Math.random() - 0.5) * 0.3;
    mid.z += (Math.random() - 0.5) * 0.3;

    const points = [start, mid, end];
    const geo    = new THREE.BufferGeometry().setFromPoints(points);
    const mat    = new THREE.LineBasicMaterial({
      color, transparent: true, opacity: 0.12,
    });

    const line = new THREE.Line(geo, mat);
    scene.add(line);
    circuitLines.push(line);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 5 — setGlobeState
// ═══════════════════════════════════════════════════════════════════════════
function setGlobeState(name) {
  const p = STATE_PARAMS[name] || STATE_PARAMS.idle;

  // Update target numeric params
  Object.assign(target, {
    particleSpeed: p.particleSpeed,
    particleOp:    p.particleOp,
    globeScale:    p.globeScale,
    coreEmissive:  p.coreEmissive,
    coreLight:     p.coreLight,
    pulseAmp:      p.pulseAmp,
    pulseFreq:     p.pulseFreq,
    lerpSpeed:     p.lerpSpeed,
  });

  // Update target color
  targetColor.setHex(p.colorHex);

  // Regenerate circuit lines in new color
  buildCircuitLines(p.colorHex);

}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 6 — updateParticles
// ═══════════════════════════════════════════════════════════════════════════
function updateParticles(time, cur) {
  const speed = cur.particleSpeed;

  // Primary cloud
  for (let i = 0; i < PARTICLE_COUNT; i++) {
    const ph = particlePhases[i];
    const sp = particleSpeeds[i];

    const theta = ph + time * speed * sp;
    const phi   = Math.PI / 2 + Math.sin(ph * 2.3 + time * 0.3 * sp) * 1.1;
    const r     = particleRadii[i] + Math.sin(time * 0.8 + ph) * 0.12;

    particlePositions[i * 3]     = r * Math.sin(phi) * Math.cos(theta);
    particlePositions[i * 3 + 1] = r * Math.cos(phi);
    particlePositions[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta);
  }
  particleMesh.geometry.attributes.position.needsUpdate = true;

  // Streak layer — faster (2.5x multiplier)
  const streakSpeed = speed * 2.5;
  for (let i = 0; i < STREAK_COUNT; i++) {
    const ph = streakPhases[i];
    const sp = streakSpeeds[i];

    const theta = ph + time * streakSpeed * sp;
    const phi   = Math.PI / 2 + Math.sin(ph * 1.7 + time * 0.5 * sp) * 0.9;
    const r     = streakRadii[i] + Math.sin(time * 1.2 + ph) * 0.08;

    streakPositions[i * 3]     = r * Math.sin(phi) * Math.cos(theta);
    streakPositions[i * 3 + 1] = r * Math.cos(phi);
    streakPositions[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta);
  }
  streakMesh.geometry.attributes.position.needsUpdate = true;
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 7 — Animation loop
// ═══════════════════════════════════════════════════════════════════════════
function animate() {
  requestAnimationFrame(animate);

  const time = clock.getElapsedTime();

  // Lerp numeric state
  lerpState(current, target);

  // Lerp color (separate THREE.Color objects — no aliasing)
  currentColor.lerp(targetColor, current.lerpSpeed * 1.5);

  // Update particles
  updateParticles(time, current);

  // Pulse offset on top of lerped scale
  const pulse = Math.sin(time * current.pulseFreq * Math.PI * 2) * current.pulseAmp;
  const scale = current.globeScale + pulse;

  innerCore.scale.setScalar(scale);
  midShell.scale.setScalar(scale  * 0.98);
  outerShell.scale.setScalar(scale * 0.96);

  // Differential shell rotation (creates 3D depth illusion)
  midShell.rotation.y   += 0.003;
  midShell.rotation.x   += 0.001;
  outerShell.rotation.y -= 0.002;
  outerShell.rotation.z += 0.0005;

  // Slow globe drift
  innerCore.rotation.y += 0.002;
  innerCore.rotation.x += 0.0005;

  // Update material colors and intensities
  const col = currentColor;

  innerCore.material.color.copy(col);
  innerCore.material.emissive.copy(col);
  innerCore.material.emissiveIntensity = current.coreEmissive;

  midShell.material.color.copy(col);
  outerShell.material.color.copy(col);

  particleMesh.material.color.copy(col);
  particleMesh.material.opacity = current.particleOp;

  streakMesh.material.color.copy(col);
  streakMesh.material.opacity = Math.min(1, current.particleOp * 1.25);

  coreLight.color.copy(col);
  coreLight.intensity = current.coreLight;

  rimLight.color.copy(col);
  rimLight.intensity  = current.coreLight * 0.25;

  circuitLines.forEach(l => {
    l.material.color.copy(col);
  });

  renderer3d.render(scene, camera);
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 8 — State map + applyState
// ═══════════════════════════════════════════════════════════════════════════
const ALL_STATE_CLASSES = ['state-idle', 'state-listening', 'state-thinking', 'state-speaking'];

const STATE_CONFIG = {
  idle:      { cssClass: 'state-idle'      },
  listening: { cssClass: 'state-listening' },
  thinking:  { cssClass: 'state-thinking'  },
  speaking:  { cssClass: 'state-speaking'  },
};

let idleTimer = null;

function applyState(name) {
  const cfg = STATE_CONFIG[name];
  if (!cfg) return;

  body.classList.remove(...ALL_STATE_CLASSES);
  body.classList.add(cfg.cssClass);

  if (stateDot) {
    stateDot.className = 'state-dot';
    if (name !== 'idle') stateDot.classList.add(`dot-${name}`);
  }

  setGlobeState(name);
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 9 — Subtitle + Tool HUD
// ═══════════════════════════════════════════════════════════════════════════
function typeSubtitle(_text) {}
function clearSubtitle() {}
function showToolHud(_name, _detail) {}
function startBarAnimation() {}
function stopBarAnimation()  {}
function resetBars()         {}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 10 — WebSocket + event handling
// ═══════════════════════════════════════════════════════════════════════════
function handleEvent(data) {
  clearTimeout(idleTimer);

  switch (data.event) {
    case 'wake':
      window.jarvis?.showWindow();
      applyState('idle');
      break;

    case 'listening':
      applyState('listening');
      clearSubtitle();
      break;

    case 'followup_listening':
      applyState('listening');
      break;

    case 'thinking':
      applyState('thinking');
      break;

    case 'speaking':
      applyState('speaking');
      break;

    case 'idle':
      applyState('idle');
      break;

    case 'tool':
      break;
  }
}

let ws = null;

function connect() {
  ws = new WebSocket(WS_URL);

  ws.addEventListener('open', () => {});

  ws.addEventListener('message', (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.event) handleEvent(data);
      if (data.type)  handleSystemMessage(data);
    } catch {}
  });

  ws.addEventListener('close', () => {
    setTimeout(connect, RECONNECT_MS);
  });

  ws.addEventListener('error', () => ws.close());
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 11 — Suit-up + Ambient system message handler
// ═══════════════════════════════════════════════════════════════════════════
const suitUpOverlay  = document.getElementById('suitUpOverlay');
const suitPhaseLabel = document.getElementById('suitPhaseLabel');
const suitChecklist  = document.getElementById('suitChecklist');
const corners        = [];

let _suitFadeTimer = null;

function handleSystemMessage(data) {
  switch (data.type) {

    // ── Suit-up ────────────────────────────────────────────────────────────

    case 'suit_up_start': {
      // Reset overlay
      suitChecklist.innerHTML = '';
      suitPhaseLabel.textContent = '';
      clearTimeout(_suitFadeTimer);
      suitUpOverlay.classList.remove('fading');
      suitUpOverlay.classList.add('active');
      // Dim the globe
      if (innerCore) innerCore.material.opacity = 0.05;
      break;
    }

    case 'suit_up_phase': {
      suitPhaseLabel.textContent = data.label || '';
      break;
    }

    case 'suit_up_check': {
      const row = document.createElement('div');
      row.className = 'suit-check-row';
      row.innerHTML =
        `<span>${data.item || ''}</span>` +
        `<span class="check-status">${data.status || ''}</span>`;
      suitChecklist.appendChild(row);
      // Auto-scroll to latest
      suitChecklist.scrollTop = suitChecklist.scrollHeight;
      break;
    }

    case 'suit_up_complete': {
      suitPhaseLabel.textContent = 'SYSTEMS NOMINAL';
      _suitFadeTimer = setTimeout(() => {
        suitUpOverlay.classList.add('fading');
        setTimeout(() => {
          suitUpOverlay.classList.remove('active', 'fading');
          suitChecklist.innerHTML = '';
          suitPhaseLabel.textContent = '';
          // Restore globe opacity
          if (innerCore) innerCore.material.opacity = 0.92;
          applyState('idle');
        }, 650);
      }, 400);
      break;
    }

    // ── Ambient ────────────────────────────────────────────────────────────

    case 'ambient_pulse': {
      triggerAmbientPulse(data.intensity || 0.5);
      break;
    }

    case 'ambient_remark': {
      triggerAmbientPulse(0.7);
      break;
    }
  }
}

// ── Ambient globe pulse (single slow heartbeat) ───────────────────────────
let _ambientPulseActive = false;

function triggerAmbientPulse(intensity) {
  if (_ambientPulseActive || !innerCore) return;
  _ambientPulseActive = true;

  const baseScale   = current.globeScale;
  const peakScale   = baseScale + intensity * 0.15;
  const duration    = 3000; // ms
  const start       = performance.now();

  function tick(now) {
    const t        = Math.min((now - start) / duration, 1);
    // ease-in-out sine: 0 → 1 → 0
    const eased    = Math.sin(t * Math.PI);
    const scale    = baseScale + (peakScale - baseScale) * eased;

    if (innerCore)    innerCore.scale.setScalar(scale);
    if (midShell)     midShell.scale.setScalar(scale * 0.98);
    if (outerShell)   outerShell.scale.setScalar(scale * 0.96);

    if (t < 1) {
      requestAnimationFrame(tick);
    } else {
      _ambientPulseActive = false;
    }
  }
  requestAnimationFrame(tick);
}

// ── Window drag ───────────────────────────────────────────────────────────
document.addEventListener('mousedown', (e) => {
  if (e.button !== 0) return;
  window.jarvis?.dragStart(e.screenX, e.screenY);
});

window.addEventListener('mouseup', () => {
  window.jarvis?.dragEnd();
});

// ── Boot ──────────────────────────────────────────────────────────────────
initGlobe();
applyState('idle');
setTimeout(connect, 2000);
