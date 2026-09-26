import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import * as d3 from 'd3';
import { loadJSON, mountTopbar, mountSource, fmtCfs, fmtET, HOUR, clamp } from '../shared/common.js';
import { loadHourlyGrid, loadTerrain, solarPosition, sunVectorENU, SNOW_DENSITY_RATIO } from '../shared/hourly-grid.js';

mountTopbar('Watershed 3D v2', 'Hourly sun, shortwave, clouds, snowpack & runoff · three.js');
mountSource('Terrain: AWS Terrain Tiles (z10). Weather: Open-Meteo historical API, ECMWF IFS 9 km, hourly, 0.1° grid (interpolated). Sun: NOAA solar-position approximation. Flow: USGS NWIS IV. Basin: USGS NLDI; rivers: NHDPlus V2.');

const params = new URLSearchParams(location.search);
const [terrain, basin, rivers, waterbodies] = await Promise.all([loadTerrain(), loadJSON('basin.json'), loadJSON('rivers.json'), loadJSON('waterbodies.json')]);
const cache = {};
async function loadWindow(key) {
  cache[key] ??= { grid: await loadHourlyGrid(key), set: await loadJSON(`gauges-${key}.json`) };
  return cache[key];
}

// ---------- terrain geometry ----------
const W = terrain.width;
const H = terrain.height;
const SX = 200;
const SZ = (SX * H) / W;
const [pxM] = terrain.pixelMeters();
const metersPerUnit = (pxM * W) / SX;
const EXAG = 3.5;
const vScale = EXAG / metersPerUnit;
const E0 = terrain.meta.minElevM;
const SNOW_EXAG = 150;
const elevAt = (u, v) => {
  const x = clamp(u * (W - 1), 0, W - 1), y = clamp(v * (H - 1), 0, H - 1);
  const x0 = Math.floor(x), y0 = Math.floor(y), x1 = Math.min(W - 1, x0 + 1), y1 = Math.min(H - 1, y0 + 1);
  const fx = x - x0, fy = y - y0;
  const e = terrain.elev;
  return e[y0 * W + x0] * (1 - fx) * (1 - fy) + e[y0 * W + x1] * fx * (1 - fy) + e[y1 * W + x0] * (1 - fx) * fy + e[y1 * W + x1] * fx * fy;
};
const heightAtUV = (u, v) => (elevAt(u, v) - E0) * vScale;
const uvToXZ = (u, v) => [(u - 0.5) * SX, (v - 0.5) * SZ];
function lonLatToScene(ll, lift = 0) {
  const [u, v] = terrain.lonLatToUV(ll);
  const [x, z] = uvToXZ(u, v);
  return new THREE.Vector3(x, heightAtUV(u, v) + lift, z);
}

const container = document.getElementById('scene');
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
container.appendChild(renderer.domElement);
const labelRenderer = new CSS2DRenderer();
labelRenderer.setSize(innerWidth, innerHeight);
Object.assign(labelRenderer.domElement.style, { position: 'absolute', inset: '0', pointerEvents: 'none' });
container.appendChild(labelRenderer.domElement);
const scene = new THREE.Scene();
scene.fog = new THREE.Fog(0x05080f, 200, 460);
const camera = new THREE.PerspectiveCamera(40, innerWidth / innerHeight, 0.5, 2000);
camera.position.set(-40, 110, 175);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(8, 0, 4);
controls.enableDamping = true;
controls.autoRotate = true;
controls.autoRotateSpeed = 0.3;
controls.maxPolarAngle = Math.PI * 0.47;
controls.minDistance = 30;
controls.maxDistance = 380;

const segX = W / 2 - 1;
const segZ = Math.round(H / 2) - 1;
const geom = new THREE.PlaneGeometry(SX, SZ, segX, segZ);
geom.rotateX(-Math.PI / 2);
const pos = geom.attributes.position;
const uvs = geom.attributes.uv;
const nV = pos.count;
const elevAttr = new Float32Array(nV);
const tnormal = new Float32Array(nV * 3);
const expo = new Float32Array(nV);
const gridUV = new Float32Array(nV * 2);
const du = 1 / segX;
const dv = 1 / segZ;
// Midday sun in mid-March at 42°N (az 180°, el ~46°) for the illustrative snow-exposure factor.
const noonSun = [0, Math.sin((46 * Math.PI) / 180), Math.cos((46 * Math.PI) / 180)]; // x east, y up, z south
for (let k = 0; k < nV; k++) {
  const u = (pos.getX(k) + SX / 2) / SX;
  const v = (pos.getZ(k) + SZ / 2) / SZ;
  pos.setY(k, heightAtUV(u, v));
  uvs.setXY(k, u, v);
  elevAttr[k] = elevAt(u, v);
  // True slope normal in scene axes (x east, y up, z south), metres.
  const gx = (elevAt(u + du, v) - elevAt(u - du, v)) / (2 * du * SX * metersPerUnit);
  const gz = (elevAt(u, v + dv) - elevAt(u, v - dv)) / (2 * dv * SZ * metersPerUnit);
  const len = Math.hypot(gx, 1, gz);
  tnormal[k * 3] = -gx / len;
  tnormal[k * 3 + 1] = 1 / len;
  tnormal[k * 3 + 2] = -gz / len;
  expo[k] = Math.max(0, tnormal[k * 3] * noonSun[0] + tnormal[k * 3 + 1] * noonSun[1] + tnormal[k * 3 + 2] * noonSun[2]);
}
geom.setAttribute('elev', new THREE.BufferAttribute(elevAttr, 1));
geom.setAttribute('tnormal', new THREE.BufferAttribute(tnormal, 3));
geom.setAttribute('expo', new THREE.BufferAttribute(expo, 1));
geom.computeVertexNormals();

// Basin mask
const maskCanvas = document.createElement('canvas');
maskCanvas.width = 1024;
maskCanvas.height = Math.round((1024 * H) / W);
{
  const m = maskCanvas.getContext('2d');
  m.fillStyle = '#000';
  m.fillRect(0, 0, maskCanvas.width, maskCanvas.height);
  m.fillStyle = '#fff';
  m.filter = 'blur(2px)';
  m.beginPath();
  basin.features[0].geometry.coordinates[0].forEach((c, k) => {
    const [u, v] = terrain.lonLatToUV(c);
    k ? m.lineTo(u * maskCanvas.width, v * maskCanvas.height) : m.moveTo(u * maskCanvas.width, v * maskCanvas.height);
  });
  m.fill();
}
const maskTex = new THREE.CanvasTexture(maskCanvas);
maskTex.flipY = false;

// ---------- weather textures (one texel per grid cell, GPU bilinear) ----------
let grid = null;
let gridTex = null;
let meltTex = null;
let gridData = null;
let meltData = null;
const cellNow = {};
let runoffIdx = null;

function setupGrid(g) {
  grid = g;
  gridData = new Uint8Array(g.nx * g.ny * 4);
  meltData = new Uint8Array(g.nx * g.ny * 4);
  gridTex?.dispose();
  meltTex?.dispose();
  gridTex = new THREE.DataTexture(gridData, g.nx, g.ny, THREE.RGBAFormat);
  meltTex = new THREE.DataTexture(meltData, g.nx, g.ny, THREE.RGBAFormat);
  for (const t of [gridTex, meltTex]) { t.magFilter = THREE.LinearFilter; t.minFilter = THREE.LinearFilter; t.flipY = false; t.needsUpdate = true; }
  const latTop = g.meta.lat0 + (g.ny - 1) * g.meta.dLat;
  for (let k = 0; k < nV; k++) {
    const [lon, lat] = terrain.uvToLonLat(uvs.getX(k), uvs.getY(k));
    gridUV[k * 2] = ((lon - g.meta.lon0) / g.meta.dLon + 0.5) / g.nx;
    gridUV[k * 2 + 1] = ((latTop - lat) / g.meta.dLat + 0.5) / g.ny;
  }
  geom.setAttribute('gridUV', new THREE.BufferAttribute(gridUV, 2));
  for (const key of ['precip', 'snowDepth', 'snowPrev', 'sw', 'temp', 'snowfall']) cellNow[key] = new Float32Array(g.nx * g.ny);
  runoffIdx = new Float32Array(g.nx * g.ny);
  uniforms.uGrid.value = gridTex;
  uniforms.uMelt.value = meltTex;
  riverUniforms.uMelt.value = meltTex;
  snowUniforms.uGrid.value = gridTex;
  snowUniforms.uMelt.value = meltTex;
  updateRiverGridUV();
}

function cellsAt(key, h, out) {
  const nc = grid.nx * grid.ny;
  const h0 = clamp(Math.floor(h), 0, grid.hours - 1);
  const h1 = Math.min(grid.hours - 1, h0 + 1);
  const f = clamp(h - h0, 0, 1);
  const a = grid.vars[key];
  for (let c = 0; c < nc; c++) out[c] = a[h0 * nc + c] * (1 - f) + a[h1 * nc + c] * f;
}

function updateWeather(h, dtH) {
  cellsAt('precip', h, cellNow.precip);
  cellsAt('snowfall', h, cellNow.snowfall);
  cellsAt('snowDepth', h, cellNow.snowDepth);
  cellsAt('snowDepth', h - 1, cellNow.snowPrev);
  cellsAt('sw', h, cellNow.sw);
  cellsAt('temp', h, cellNow.temp);
  const nc = grid.nx * grid.ny;
  for (let c = 0; c < nc; c++) {
    const melt = Math.max(0, cellNow.snowPrev[c] - cellNow.snowDepth[c]) * 1000 * SNOW_DENSITY_RATIO;
    const rain = cellNow.temp[c] > 0.5 ? cellNow.precip[c] : 0;
    runoffIdx[c] = runoffIdx[c] * Math.exp(-dtH / 18) + (rain + melt) * dtH;
    gridData[c * 4] = clamp(Math.sqrt(Math.max(0, cellNow.precip[c]) / 20) * 255, 0, 255);
    gridData[c * 4 + 1] = clamp((cellNow.snowDepth[c] / 0.6) * 255, 0, 255);
    gridData[c * 4 + 2] = clamp((cellNow.sw[c] / 1000) * 255, 0, 255);
    gridData[c * 4 + 3] = clamp(((cellNow.temp[c] + 25) / 60) * 255, 0, 255);
    meltData[c * 4] = clamp((melt / 3) * 255, 0, 255);
    meltData[c * 4 + 1] = clamp((runoffIdx[c] / 25) * 255, 0, 255);
    meltData[c * 4 + 2] = 0;
    meltData[c * 4 + 3] = 255;
  }
  gridTex.needsUpdate = true;
  meltTex.needsUpdate = true;
}

// ---------- terrain material ----------
const uniforms = {
  uGrid: { value: null },
  uMelt: { value: null },
  uMask: { value: maskTex },
  uMode: { value: 0 },
  uSun: { value: new THREE.Vector3(0, 1, 0) },
  uElevRange: { value: new THREE.Vector2(terrain.meta.minElevM, terrain.meta.maxElevM) },
};
const COMMON_GLSL = /* glsl */ `
  vec3 ramp4(float t, vec3 a, vec3 b, vec3 c, vec3 d) {
    t = clamp(t, 0.0, 1.0);
    if (t < 0.333) return mix(a, b, t / 0.333);
    if (t < 0.666) return mix(b, c, (t - 0.333) / 0.333);
    return mix(c, d, (t - 0.666) / 0.334);
  }
  // Direct light from the sun when it's up, fading to a dim moonlit fill at night.
  float sunLight(vec3 n, vec3 sun) {
    float up = sun.y;
    float day = smoothstep(-0.04, 0.12, up);
    float inc = max(dot(n, sun), 0.0);
    float night = 0.3 + 0.2 * max(dot(n, normalize(vec3(-0.4, 0.8, -0.3))), 0.0);
    return mix(night, 0.14 + 1.0 * inc, day);
  }
`;
const terrainMat = new THREE.ShaderMaterial({
  uniforms,
  vertexShader: /* glsl */ `
    attribute float elev;
    attribute vec3 tnormal;
    attribute vec2 gridUV;
    varying vec2 vUv;
    varying vec2 vGrid;
    varying vec3 vTN;
    varying float vElev;
    varying float vDepth;
    void main() {
      vUv = uv;
      vGrid = gridUV;
      vTN = tnormal;
      vElev = elev;
      vec4 mv = modelViewMatrix * vec4(position, 1.0);
      vDepth = -mv.z;
      gl_Position = projectionMatrix * mv;
    }`,
  fragmentShader: /* glsl */ `
    uniform sampler2D uGrid;
    uniform sampler2D uMelt;
    uniform sampler2D uMask;
    uniform int uMode;
    uniform vec3 uSun;
    uniform vec2 uElevRange;
    varying vec2 vUv;
    varying vec2 vGrid;
    varying vec3 vTN;
    varying float vElev;
    varying float vDepth;
    ${COMMON_GLSL}
    void main() {
      vec4 f = texture2D(uGrid, vGrid);
      vec4 mt = texture2D(uMelt, vGrid);
      float mask = texture2D(uMask, vUv).r;
      vec3 n = normalize(vTN);
      float e = (vElev - uElevRange.x) / (uElevRange.y - uElevRange.x);
      vec3 base = ramp4(e, vec3(0.10, 0.22, 0.14), vec3(0.19, 0.31, 0.16), vec3(0.36, 0.37, 0.24), vec3(0.6, 0.56, 0.48));
      float light = sunLight(n, uSun);
      // Wet ground darkens with the recent rain + melt index.
      base *= 1.0 - 0.3 * smoothstep(0.05, 0.6, mt.g);
      vec3 col = base * light;
      if (uMode == 1) {
        float sinEl = max(uSun.y, 0.12);
        float inc = max(dot(n, uSun), 0.0);
        float swH = f.b * 1000.0;
        float swSlope = uSun.y > 0.0 ? swH * (0.2 + 0.8 * inc / sinEl) : 0.0;
        vec3 c = ramp4(swSlope / 850.0, vec3(0.05, 0.02, 0.15), vec3(0.55, 0.08, 0.45), vec3(0.98, 0.45, 0.1), vec3(1.0, 0.97, 0.6));
        col = mix(col, c, 0.85);
      } else if (uMode == 2) {
        vec3 c = ramp4(f.a, vec3(0.2, 0.35, 0.95), vec3(0.4, 0.85, 0.95), vec3(1.0, 0.85, 0.4), vec3(0.95, 0.25, 0.15));
        col = mix(col, c * (0.5 + 0.6 * light), 0.75);
      }
      float g = dot(col, vec3(0.3, 0.59, 0.11));
      col = mix(vec3(g) * 0.35, col, 0.15 + 0.85 * mask);
      col = mix(col, vec3(0.02, 0.03, 0.06), smoothstep(200.0, 460.0, vDepth));
      gl_FragColor = vec4(col, 1.0);
    }`,
});
scene.add(new THREE.Mesh(geom, terrainMat));
{
  const skirt = new THREE.Mesh(new THREE.BoxGeometry(SX, 2, SZ), new THREE.MeshBasicMaterial({ color: 0x0a1222 }));
  skirt.position.y = -1.05;
  scene.add(skirt);
}

// ---------- snowpack: displaced white cover ----------
const snowUniforms = { uGrid: { value: null }, uMelt: { value: null }, uSun: uniforms.uSun, uExag: { value: SNOW_EXAG * vScale }, uMask: { value: maskTex } };
const snowMat = new THREE.ShaderMaterial({
  uniforms: snowUniforms,
  transparent: true,
  polygonOffset: true,
  polygonOffsetFactor: -2,
  polygonOffsetUnits: -2,
  vertexShader: /* glsl */ `
    uniform sampler2D uGrid;
    uniform float uExag;
    attribute vec3 tnormal;
    attribute vec2 gridUV;
    attribute float expo;
    varying vec3 vTN;
    varying float vSnow;
    varying vec2 vGrid;
    varying vec2 vUv;
    void main() {
      vGrid = gridUV;
      vUv = uv;
      vTN = tnormal;
      float cellDepth = texture(uGrid, gridUV).g * 0.6;
      // Illustrative downscaling: slopes that face the midday sun hold less snow.
      float depth = cellDepth * clamp(1.45 - 0.75 * expo, 0.35, 1.5);
      vSnow = depth;
      vec3 p = position + vec3(0.0, depth * uExag, 0.0);
      gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
    }`,
  fragmentShader: /* glsl */ `
    uniform sampler2D uMelt;
    uniform sampler2D uMask;
    uniform vec3 uSun;
    varying vec3 vTN;
    varying float vSnow;
    varying vec2 vGrid;
    varying vec2 vUv;
    ${COMMON_GLSL}
    void main() {
      if (vSnow < 0.02) discard;
      float light = sunLight(normalize(vTN), uSun);
      vec3 col = mix(vec3(0.55, 0.62, 0.8), vec3(0.97, 0.98, 1.0), clamp(light, 0.0, 1.0)) * (0.35 + 0.75 * light);
      float melt = texture2D(uMelt, vGrid).r;
      col = mix(col, vec3(1.0, 0.86, 0.95), melt * 0.45);
      float mask = texture2D(uMask, vUv).r;
      gl_FragColor = vec4(col, smoothstep(0.02, 0.09, vSnow) * (0.35 + 0.6 * mask));
    }`,
});
const snowMesh = new THREE.Mesh(geom, snowMat);
scene.add(snowMesh);

// ---------- rivers (glow brightens with recent runoff) ----------
const riverPos = [];
const riverDist = [];
const riverOrder = [];
const riverLL = [];
for (const f of rivers.features) {
  const pts3 = f.geometry.coordinates.map((ll) => lonLatToScene(ll, 0.25));
  let dist = 0;
  for (let k = 0; k < pts3.length - 1; k++) {
    const a = pts3[k];
    const b = pts3[k + 1];
    riverPos.push(a.x, a.y, a.z, b.x, b.y, b.z);
    const seg = a.distanceTo(b);
    riverDist.push(-f.properties.pathKm * 1.3 - dist, -f.properties.pathKm * 1.3 - dist - seg);
    dist += seg;
    riverOrder.push(f.properties.order, f.properties.order);
    riverLL.push(f.geometry.coordinates[k], f.geometry.coordinates[k + 1]);
  }
}
const riverGeom = new THREE.BufferGeometry();
riverGeom.setAttribute('position', new THREE.Float32BufferAttribute(riverPos, 3));
riverGeom.setAttribute('dist', new THREE.Float32BufferAttribute(riverDist, 1));
riverGeom.setAttribute('order', new THREE.Float32BufferAttribute(riverOrder, 1));
riverGeom.setAttribute('gridUV', new THREE.Float32BufferAttribute(new Float32Array(riverLL.length * 2), 2));
function updateRiverGridUV() {
  const a = riverGeom.attributes.gridUV;
  const latTop = grid.meta.lat0 + (grid.ny - 1) * grid.meta.dLat;
  riverLL.forEach(([lon, lat], k) => a.setXY(k, ((lon - grid.meta.lon0) / grid.meta.dLon + 0.5) / grid.nx, ((latTop - lat) / grid.meta.dLat + 0.5) / grid.ny));
  a.needsUpdate = true;
}
const riverUniforms = { uPhase: { value: 0 }, uMelt: { value: null }, uDay: { value: 1 } };
scene.add(new THREE.LineSegments(riverGeom, new THREE.ShaderMaterial({
  uniforms: riverUniforms,
  transparent: true,
  depthWrite: false,
  blending: THREE.AdditiveBlending,
  vertexShader: /* glsl */ `
    attribute float dist;
    attribute float order;
    attribute vec2 gridUV;
    varying float vDist;
    varying float vOrder;
    varying vec2 vGrid;
    void main() {
      vDist = dist;
      vOrder = order;
      vGrid = gridUV;
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }`,
  fragmentShader: /* glsl */ `
    uniform float uPhase;
    uniform sampler2D uMelt;
    varying float vDist;
    varying float vOrder;
    varying vec2 vGrid;
    void main() {
      float runoff = texture2D(uMelt, vGrid).g;
      float pulse = pow(0.5 + 0.5 * sin(vDist * 1.6 + uPhase), 6.0);
      float base = 0.28 + vOrder * 0.09 + runoff * 0.6;
      vec3 col = mix(vec3(0.2, 0.55, 0.95), vec3(0.8, 1.0, 1.0), pulse * (0.6 + runoff));
      gl_FragColor = vec4(col, base + pulse * (0.4 + runoff));
    }`,
})));

for (const f of waterbodies.features.filter((d) => d.properties.areaSqKm > 1)) {
  const polys = f.geometry.type === 'MultiPolygon' ? f.geometry.coordinates : [f.geometry.coordinates];
  for (const poly of polys) {
    const ring = poly[0];
    const shape = new THREE.Shape(ring.map((ll) => { const [u, v] = terrain.lonLatToUV(ll); const [x, z] = uvToXZ(u, v); return new THREE.Vector2(x, -z); }));
    const g = new THREE.ShapeGeometry(shape);
    g.rotateX(-Math.PI / 2);
    const m = new THREE.Mesh(g, new THREE.MeshBasicMaterial({ color: 0x1f6fb0, transparent: true, opacity: 0.85 }));
    m.position.y = d3.min(ring, (ll) => heightAtUV(...terrain.lonLatToUV(ll))) + 0.3;
    scene.add(m);
  }
}
scene.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(basin.features[0].geometry.coordinates[0].map((ll) => lonLatToScene(ll, 0.6))), new THREE.LineBasicMaterial({ color: 0x80ffdb, transparent: true, opacity: 0.8 })));

function label(text, ll, lift, cls = '') {
  const el = document.createElement('div');
  el.className = `lbl ${cls}`;
  el.textContent = text;
  const o = new CSS2DObject(el);
  o.position.copy(lonLatToScene(ll, lift));
  scene.add(o);
  return el;
}
const gaugeLL = [-75.0574167, 41.75675];
const gaugeLabel = label('Callicoon gauge', gaugeLL, 4, 'gauge');
label('Cannonsville Res.', [-75.32, 42.07], 3);
label('Pepacton Res.', [-74.93, 42.09], 3);
label('Catskills', [-74.47, 42.2], 8);

// ---------- sun disc ----------
const sunTex = (() => {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d');
  const grd = g.createRadialGradient(64, 64, 4, 64, 64, 64);
  grd.addColorStop(0, 'rgba(255,250,220,1)');
  grd.addColorStop(0.2, 'rgba(255,210,120,0.9)');
  grd.addColorStop(1, 'rgba(255,160,60,0)');
  g.fillStyle = grd;
  g.fillRect(0, 0, 128, 128);
  return new THREE.CanvasTexture(c);
})();
const sunSprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: sunTex, transparent: true, depthWrite: false, fog: false }));
sunSprite.scale.set(40, 40, 1);
scene.add(sunSprite);

// ---------- clouds (billboard puffs per grid cell) ----------
const puffTex = (() => {
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const g = c.getContext('2d');
  const grd = g.createRadialGradient(32, 32, 2, 32, 32, 32);
  grd.addColorStop(0, 'rgba(255,255,255,1)');
  grd.addColorStop(0.5, 'rgba(255,255,255,0.45)');
  grd.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grd;
  g.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
})();
const PUFFS_PER_CELL = 14;
let cloud = null;
function buildClouds() {
  if (cloud) { scene.remove(cloud.points); cloud.points.geometry.dispose(); }
  const nc = grid.nx * grid.ny;
  const n = nc * PUFFS_PER_CELL;
  const p = new Float32Array(n * 3);
  const cell = new Float32Array(n);
  const inten = new Float32Array(n);
  const seed = new Float32Array(n);
  for (let c = 0; c < nc; c++) {
    const [lon, lat] = grid.cellLonLat(c);
    for (let k = 0; k < PUFFS_PER_CELL; k++) {
      const i = c * PUFFS_PER_CELL + k;
      const v = lonLatToScene([lon + (Math.random() - 0.5) * grid.meta.dLon * 1.3, lat + (Math.random() - 0.5) * grid.meta.dLat * 1.3]);
      p[i * 3] = v.x;
      p[i * 3 + 1] = 11 + Math.random() * 4;
      p[i * 3 + 2] = v.z;
      cell[i] = c;
      seed[i] = Math.random();
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(p, 3));
  g.setAttribute('intensity', new THREE.BufferAttribute(inten, 1));
  g.setAttribute('seed', new THREE.BufferAttribute(seed, 1));
  const mat = new THREE.ShaderMaterial({
    uniforms: { uTex: { value: puffTex }, uDay: { value: 1 }, uScale: { value: innerHeight / 2 } },
    transparent: true,
    depthWrite: false,
    vertexShader: /* glsl */ `
      attribute float intensity;
      attribute float seed;
      uniform float uScale;
      varying float vI;
      varying float vSeed;
      void main() {
        vI = intensity;
        vSeed = seed;
        vec4 mv = modelViewMatrix * vec4(position + vec3(0.0, intensity * 2.5, 0.0), 1.0);
        gl_PointSize = (12.0 + 18.0 * intensity) * (0.7 + 0.6 * seed) * uScale / -mv.z;
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: /* glsl */ `
      uniform sampler2D uTex;
      uniform float uDay;
      varying float vI;
      varying float vSeed;
      void main() {
        if (vI < 0.02) discard;
        float a = texture2D(uTex, gl_PointCoord).a;
        vec3 lightCol = mix(vec3(0.25, 0.28, 0.36), vec3(0.95, 0.96, 1.0), uDay);
        vec3 col = mix(lightCol, lightCol * 0.45, clamp(vI, 0.0, 1.0));
        gl_FragColor = vec4(col, a * clamp(vI * 0.5, 0.0, 0.36));
      }`,
  });
  const points = new THREE.Points(g, mat);
  points.renderOrder = 5;
  scene.add(points);
  cloud = { points, cell, inten, n };
}
function updateClouds() {
  const { cell, inten, n } = cloud;
  for (let i = 0; i < n; i++) {
    const P = Math.max(0, cellNow.precip[cell[i]]);
    const target = P < 0.3 ? 0 : clamp(Math.sqrt(P / 8), 0.15, 1);
    inten[i] += (target - inten[i]) * 0.15;
  }
  cloud.points.geometry.attributes.intensity.needsUpdate = true;
  cloud.points.visible = layers.clouds;
}

// ---------- rain columns and snowfall ----------
const RAIN_N = 6000;
const SNOW_N = 4000;
const rainPos = new Float32Array(RAIN_N * 6);
const rainGeom = new THREE.BufferGeometry();
rainGeom.setAttribute('position', new THREE.BufferAttribute(rainPos, 3));
const rainLines = new THREE.LineSegments(rainGeom, new THREE.LineBasicMaterial({ color: 0xa8b8ff, transparent: true, opacity: 0.5, depthWrite: false }));
scene.add(rainLines);
const snowPos = new Float32Array(SNOW_N * 3);
const snowGeom = new THREE.BufferGeometry();
snowGeom.setAttribute('position', new THREE.BufferAttribute(snowPos, 3));
const snowPts = new THREE.Points(snowGeom, new THREE.PointsMaterial({ size: 0.8, map: puffTex, transparent: true, depthWrite: false, color: 0xffffff }));
scene.add(snowPts);
const drops = { rain: Array.from({ length: RAIN_N }, () => ({ y: -999 })), snow: Array.from({ length: SNOW_N }, () => ({ y: -999, ph: Math.random() * 6 })) };
let cumRain = null;
let cumSnow = null;
function buildSpawnTables() {
  const nc = grid.nx * grid.ny;
  cumRain = new Float32Array(nc);
  cumSnow = new Float32Array(nc);
  let a = 0, b = 0;
  for (let c = 0; c < nc; c++) {
    const P = Math.max(0, cellNow.precip[c]);
    if (cellNow.temp[c] > 0.5) a += P; else b += P;
    cumRain[c] = a;
    cumSnow[c] = b;
  }
}
function pickCell(cum) {
  const total = cum[cum.length - 1];
  if (total <= 0) return -1;
  const r = Math.random() * total;
  let lo = 0, hi = cum.length - 1;
  while (lo < hi) { const m = (lo + hi) >> 1; if (cum[m] < r) lo = m + 1; else hi = m; }
  return lo;
}
function spawnDrop(d, cum, snow) {
  const c = pickCell(cum);
  if (c < 0) { d.y = -999; return; }
  const [lon, lat] = grid.cellLonLat(c);
  const v = lonLatToScene([lon + (Math.random() - 0.5) * grid.meta.dLon, lat + (Math.random() - 0.5) * grid.meta.dLat]);
  d.x = v.x; d.z = v.z; d.ground = v.y;
  d.y = 14 + Math.random() * 5;
  d.v = snow ? 3 + Math.random() * 2 : 30 + Math.random() * 12;
}
function updateDrops(dt, t) {
  const tr = cumRain[cumRain.length - 1];
  const ts = cumSnow[cumSnow.length - 1];
  const nc = cumRain.length;
  const activeRain = layers.clouds ? Math.round(clamp(tr / (nc * 3), 0, 1) * RAIN_N) : 0;
  const activeSnow = layers.clouds ? Math.round(clamp(ts / (nc * 1.2), 0, 1) * SNOW_N) : 0;
  drops.rain.forEach((d, k) => {
    if (k >= activeRain) d.y = -999;
    else if (d.y < (d.ground ?? 0)) spawnDrop(d, cumRain, false);
    d.y -= (d.v ?? 0) * dt;
    const o = k * 6;
    rainPos[o] = d.x ?? 0; rainPos[o + 1] = d.y; rainPos[o + 2] = d.z ?? 0;
    rainPos[o + 3] = (d.x ?? 0) + 0.12; rainPos[o + 4] = d.y + 1.0; rainPos[o + 5] = d.z ?? 0;
  });
  drops.snow.forEach((d, k) => {
    if (k >= activeSnow) d.y = -999;
    else if (d.y < (d.ground ?? 0)) spawnDrop(d, cumSnow, true);
    d.y -= (d.v ?? 0) * dt;
    const o = k * 3;
    snowPos[o] = (d.x ?? 0) + Math.sin(t * 1.3 + d.ph) * 0.5; snowPos[o + 1] = d.y; snowPos[o + 2] = (d.z ?? 0) + Math.cos(t * 0.9 + d.ph) * 0.5;
  });
  rainGeom.attributes.position.needsUpdate = true;
  snowGeom.attributes.position.needsUpdate = true;
}

// ---------- runoff trickles: steepest descent on the DEM ----------
const TW = 320;
const TH = Math.round((TW * H) / W);
const downhill = new Int32Array(TW * TH);
{
  const e = (i, j) => elevAt((i + 0.5) / TW, (j + 0.5) / TH);
  for (let j = 0; j < TH; j++) {
    for (let i = 0; i < TW; i++) {
      let best = -1;
      let bestDrop = 0;
      const e0 = e(i, j);
      for (let dj = -1; dj <= 1; dj++) for (let di = -1; di <= 1; di++) {
        if (!di && !dj) continue;
        const ii = i + di, jj = j + dj;
        if (ii < 0 || jj < 0 || ii >= TW || jj >= TH) continue;
        const drop = (e0 - e(ii, jj)) / Math.hypot(di, dj);
        if (drop > bestDrop) { bestDrop = drop; best = jj * TW + ii; }
      }
      downhill[j * TW + i] = best;
    }
  }
}
const TRICKLE_N = 5000;
const trickle = Array.from({ length: TRICKLE_N }, () => ({ cell: -1, life: 0, kind: 0, f: 0, next: -1 }));
const trPos = new Float32Array(TRICKLE_N * 3);
const trCol = new Float32Array(TRICKLE_N * 3);
const trGeom = new THREE.BufferGeometry();
trGeom.setAttribute('position', new THREE.BufferAttribute(trPos, 3));
trGeom.setAttribute('color', new THREE.BufferAttribute(trCol, 3));
const trPts = new THREE.Points(trGeom, new THREE.PointsMaterial({ size: 0.9, vertexColors: true, map: puffTex, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }));
scene.add(trPts);
const cellXYZ = (idx) => {
  const u = ((idx % TW) + 0.5) / TW;
  const v = (Math.floor(idx / TW) + 0.5) / TH;
  const [x, z] = uvToXZ(u, v);
  return [x, heightAtUV(u, v) + 0.6, z];
};
let cumRunoff = null;
function buildRunoffTable() {
  const nc = grid.nx * grid.ny;
  cumRunoff = new Float32Array(nc);
  let a = 0;
  for (let c = 0; c < nc; c++) {
    if (grid.meta.inBasin[c]) a += (cellNow.temp[c] > 0.5 ? Math.max(0, cellNow.precip[c]) : 0) + Math.max(0, cellNow.snowPrev[c] - cellNow.snowDepth[c]) * 300;
    cumRunoff[c] = a;
  }
}
function updateTrickles(dt, dtH) {
  const total = cumRunoff[cumRunoff.length - 1];
  const want = layers.runoff ? clamp(total / 40, 0, 1) : 0;
  const latTop = grid.meta.lat0 + (grid.ny - 1) * grid.meta.dLat;
  for (let k = 0; k < TRICKLE_N; k++) {
    const t = trickle[k];
    if (t.cell < 0 || t.life <= 0) {
      if (Math.random() < want * 0.05 && dtH > 0) {
        const c = pickCell(cumRunoff);
        if (c >= 0) {
          const [lon, lat] = grid.cellLonLat(c);
          const [u, v] = terrain.lonLatToUV([lon + (Math.random() - 0.5) * grid.meta.dLon, lat + (Math.random() - 0.5) * grid.meta.dLat]);
          t.cell = clamp(Math.floor(v * TH), 0, TH - 1) * TW + clamp(Math.floor(u * TW), 0, TW - 1);
          t.next = downhill[t.cell];
          t.f = 0;
          t.life = 2.5 + Math.random() * 2;
          t.kind = cellNow.snowPrev[c] - cellNow.snowDepth[c] > 0.0005 ? 1 : 0;
        }
      }
      if (t.cell < 0 || t.life <= 0) { trPos[k * 3 + 1] = -999; continue; }
    }
    t.life -= dt;
    t.f += dt * 7;
    while (t.f >= 1 && t.next >= 0) { t.cell = t.next; t.next = downhill[t.cell]; t.f -= 1; }
    if (t.next < 0) t.f = 0;
    const a = cellXYZ(t.cell);
    const b = t.next >= 0 ? cellXYZ(t.next) : a;
    trPos[k * 3] = a[0] + (b[0] - a[0]) * t.f;
    trPos[k * 3 + 1] = a[1] + (b[1] - a[1]) * t.f;
    trPos[k * 3 + 2] = a[2] + (b[2] - a[2]) * t.f;
    const fade = Math.min(1, t.life);
    if (t.kind) { trCol[k * 3] = 0.85 * fade; trCol[k * 3 + 1] = 0.7 * fade; trCol[k * 3 + 2] = 1.0 * fade; }
    else { trCol[k * 3] = 0.55 * fade; trCol[k * 3 + 1] = 0.85 * fade; trCol[k * 3 + 2] = 1.0 * fade; }
  }
  trGeom.attributes.position.needsUpdate = true;
  trGeom.attributes.color.needsUpdate = true;
}

// ---------- timeline ----------
const tl = d3.select('#timeline svg');
let tlX = null;
let series = null;
function drawTimeline(state) {
  const node = tl.node();
  const w = node.clientWidth;
  const h = node.clientHeight;
  const m = { l: 44, r: 44, t: 8, b: 18 };
  const n = grid.hours;
  const times = d3.range(n).map((i) => new Date(grid.t0 + i * HOUR));
  tlX = d3.scaleUtc(d3.extent(times), [m.l, w - m.r]);
  const x = tlX;
  const q = state.outlet.q;
  const qOff = Math.round((grid.t0 - Date.parse(state.set.start)) / HOUR);
  const qAt = (i) => q[i + qOff] ?? null;
  const yq = d3.scaleLinear([0, d3.max(q) * 1.1], [h - m.b, m.t + 8]);
  const yp = d3.scaleLinear([0, Math.max(1, d3.max(series.precip))], [m.t, (h - m.b) * 0.5]);
  const ys = d3.scaleLinear([0, Math.max(0.1, d3.max(series.snow))], [h - m.b, m.t + 26]);
  const ysw = d3.scaleLinear([0, 900], [h - m.b, m.t + 10]);
  tl.selectAll('*').remove();
  // Night bands from the sun position.
  const c = d3.geoCentroid(basin);
  const night = times.map((t) => solarPosition(t, c[1], c[0]).elevation < 0);
  const bw = (w - m.l - m.r) / n;
  tl.append('g').selectAll('rect').data(d3.range(n).filter((i) => night[i])).join('rect').attr('x', (i) => x(times[i])).attr('width', bw + 0.5).attr('y', m.t).attr('height', h - m.b - m.t).attr('fill', 'rgba(0,0,0,0.28)');
  tl.append('g').attr('class', 'axis').attr('transform', `translate(0,${h - m.b})`).call(d3.axisBottom(x).ticks(w / 110).tickSizeOuter(0));
  tl.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(yq).ticks(3, '~s'));
  tl.append('path').attr('d', d3.area().x((_, i) => x(times[i])).y0(h - m.b).y1((v) => ys(v ?? 0))(series.snow)).attr('fill', 'rgba(238,242,255,0.16)').attr('stroke', 'rgba(238,242,255,0.5)');
  tl.append('path').attr('d', d3.line().x((_, i) => x(times[i])).y((v) => ysw(v ?? 0))(series.sw)).attr('fill', 'none').attr('stroke', 'rgba(255,176,64,0.6)').attr('stroke-width', 0.9);
  tl.append('g').selectAll('rect.p').data(d3.range(n).filter((i) => series.precip[i] > 0.05)).join('rect').attr('class', 'p')
    .attr('x', (i) => x(times[i])).attr('width', Math.max(1, bw)).attr('y', m.t).attr('height', (i) => yp(series.precip[i]) - m.t).attr('fill', 'rgba(123,140,255,0.8)');
  tl.append('path').attr('d', d3.line().defined((i) => qAt(i) != null).x((i) => x(times[i])).y((i) => yq(qAt(i)))(d3.range(n))).attr('fill', 'none').attr('stroke', '#80ffdb').attr('stroke-width', 1.3);
  tl.append('text').attr('x', w - m.r).attr('y', h - m.b - 8).attr('text-anchor', 'end').style('font-size', '10.5px').text('Callicoon flow (cfs) · precip bars · snow depth (white) · shortwave (orange) · night shaded');
  tl.append('line').attr('class', 'cursor').attr('y1', m.t).attr('y2', h - m.b).attr('stroke', '#ffb703').attr('stroke-width', 1.5);
  tl.on('pointerdown pointermove', (event) => {
    if (event.type === 'pointermove' && event.buttons !== 1) return;
    clock = clamp(x.invert(d3.pointer(event)[0]).getTime(), grid.t0, grid.t0 + (grid.hours - 1) * HOUR);
    runoffIdx.fill(0);
  });
}

// ---------- state, modes & controls ----------
let state = null;
let clock = 0;
let playing = true;
let speed = 5;
const layers = { clouds: true, snow: true, runoff: true };
const MODES = [
  { key: 'weather' },
  { key: 'sw', lo: '0', hi: '850 W/m²', colors: ['#0d0526', '#8c1473', '#fa731a', '#fff799'] },
  { key: 'temp', lo: '−25 °C', hi: '35 °C', colors: ['#3359f2', '#66d9f2', '#ffd966', '#f24026'] },
];
function setMode(k) {
  uniforms.uMode.value = k;
  MODES.forEach((m, kk) => document.getElementById(`m-${m.key}`).classList.toggle('active', kk === k));
  const row = document.getElementById('legend-row');
  row.style.display = MODES[k].colors ? 'flex' : 'none';
  if (MODES[k].colors) {
    const c = document.getElementById('legend').getContext('2d');
    const g = c.createLinearGradient(0, 0, 140, 0);
    MODES[k].colors.forEach((col, i) => g.addColorStop(i / 3, col));
    c.fillStyle = g;
    c.fillRect(0, 0, 140, 8);
    document.getElementById('lg-lo').textContent = MODES[k].lo;
    document.getElementById('lg-hi').textContent = MODES[k].hi;
  }
}
MODES.forEach((m, k) => { document.getElementById(`m-${m.key}`).onclick = () => setMode(k); });
for (const key of Object.keys(layers)) {
  document.getElementById(`l-${key}`).onclick = (ev) => {
    layers[key] = !layers[key];
    ev.target.classList.toggle('active', layers[key]);
  };
}
const playBtn = document.getElementById('play');
playBtn.onclick = () => { playing = !playing; playBtn.textContent = playing ? '❚❚ Pause' : '▶ Play'; };
document.getElementById('speed').onchange = (e) => { speed = +e.target.value; };
const rotBtn = document.getElementById('rotate');
rotBtn.onclick = () => { controls.autoRotate = !controls.autoRotate; rotBtn.classList.toggle('active', controls.autoRotate); };
document.getElementById('window').onchange = (e) => setWindow(e.target.value);
window.addEventListener('keydown', (e) => { if (e.code === 'Space') { e.preventDefault(); playBtn.click(); } });
window.addEventListener('resize', () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
  labelRenderer.setSize(innerWidth, innerHeight);
  if (cloud) cloud.points.material.uniforms.uScale.value = innerHeight / 2;
  if (state) drawTimeline(state);
});

async function setWindow(key) {
  const data = await loadWindow(key);
  setupGrid(data.grid);
  state = { ...data, outlet: data.set.sites.find((s) => s.id === '01427510') };
  series = { precip: grid.basinMean('precip'), snow: grid.basinMean('snowDepth'), sw: grid.basinMean('sw'), temp: grid.basinMean('temp') };
  cellsAt('precip', 0, cellNow.precip);
  buildClouds();
  document.getElementById('window').value = key;
  if (params.has('t')) clock = Date.parse(params.get('t'));
  else if (key === 'melt') {
    let best = 24;
    for (let i = 24; i < grid.hours; i++) if (series.snow[i - 24] - series.snow[i] > series.snow[best - 24] - series.snow[best]) best = i;
    clock = grid.t0 + Math.max(0, best - 72) * HOUR;
  } else {
    const qOff = Math.round((grid.t0 - Date.parse(state.set.start)) / HOUR);
    const q = state.outlet.q;
    const peak = q.indexOf(d3.max(q)) - qOff;
    let i = Math.max(0, peak - 96);
    while (i < peak && series.precip[i] < 0.8) i++;
    clock = grid.t0 + Math.max(0, i - 18) * HOUR;
  }
  drawTimeline(state);
}

await setWindow(params.get('window') === 'storm' ? 'storm' : 'melt');
if (params.has('paused')) playBtn.click();
{
  const k = MODES.findIndex((m) => m.key === params.get('mode'));
  setMode(k > 0 ? k : 0);
}

// ---------- loop ----------
const center = d3.geoCentroid(basin);
const dayColor = new THREE.Color(0x14264a);
const nightColor = new THREE.Color(0x03050b);
const duskColor = new THREE.Color(0x3a2a4a);
const sky = new THREE.Color();
let lastT = performance.now();
let phase = 0;
let ui = 1;
const gaugePos = lonLatToScene(gaugeLL, 0);
const beam = new THREE.Mesh(new THREE.CylinderGeometry(0.7, 0.7, 1, 24, 1, true), new THREE.MeshBasicMaterial({ color: 0x80ffdb, transparent: true, opacity: 0.55, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide }));
scene.add(beam);

function animate(now) {
  const dt = Math.min(0.05, (now - lastT) / 1000);
  lastT = now;
  const dtH = playing ? dt * speed : 0;
  if (playing) {
    clock += dtH * HOUR;
    if (clock > grid.t0 + (grid.hours - 1) * HOUR) { clock = grid.t0; runoffIdx.fill(0); }
  }
  const h = grid.hourAt(clock);
  const date = new Date(clock);
  updateWeather(h, dtH);
  const sp = solarPosition(date, center[1], center[0]);
  const [e, nn, u] = sunVectorENU(sp);
  uniforms.uSun.value.set(e, u, -nn);
  const up = u;
  const day = clamp((up + 0.05) / 0.2, 0, 1);
  const dusk = Math.max(0, 1 - Math.abs(up - 0.03) / 0.12);
  sky.copy(nightColor).lerp(dayColor, day).lerp(duskColor, dusk * 0.6);
  renderer.setClearColor(sky);
  scene.fog.color.copy(sky);
  sunSprite.position.set(e * 320, u * 320, -nn * 320);
  sunSprite.visible = up > -0.03;
  cloud.points.material.uniforms.uDay.value = day;
  snowMesh.visible = layers.snow;
  updateClouds();
  buildSpawnTables();
  updateDrops(dt, now / 1000);
  buildRunoffTable();
  updateTrickles(dt, dtH);
  const qOff = Math.round((clock - Date.parse(state.set.start)) / HOUR);
  const q = state.outlet.q[clamp(qOff, 0, state.outlet.q.length - 1)] ?? 1000;
  phase += dt * (1.5 + 5 * Math.sqrt(q / 2000));
  riverUniforms.uPhase.value = phase;
  const beamH = 2 + 26 * Math.sqrt(q / 30000);
  beam.scale.set(1, beamH, 1);
  beam.position.set(gaugePos.x, gaugePos.y + beamH / 2, gaugePos.z);
  controls.update();
  renderer.render(scene, camera);
  labelRenderer.render(scene, camera);
  if ((ui += dt) > 0.1) {
    ui = 0;
    const hi = clamp(Math.round(h), 0, grid.hours - 1);
    const meltMean = d3.mean(grid.basinCells, (c) => Math.max(0, cellNow.snowPrev[c] - cellNow.snowDepth[c]) * 1000 * SNOW_DENSITY_RATIO);
    document.getElementById('date').textContent = fmtET(date);
    document.getElementById('s-sun').textContent = sp.elevation > 0 ? `${sp.elevation.toFixed(0)}° · az ${sp.azimuth.toFixed(0)}°` : `night (${sp.elevation.toFixed(0)}°)`;
    document.getElementById('s-sw').textContent = `${(series.sw[hi] ?? 0).toFixed(0)} W/m²`;
    document.getElementById('s-precip').textContent = `${(series.precip[hi] ?? 0).toFixed(2)} mm/h`;
    document.getElementById('s-snow').textContent = `${((series.snow[hi] ?? 0) * 100).toFixed(1)} cm`;
    document.getElementById('s-melt').textContent = `${meltMean.toFixed(2)} mm/h`;
    document.getElementById('s-q').textContent = fmtCfs(q);
    gaugeLabel.textContent = `Callicoon · ${fmtCfs(q)}`;
    tl.select('.cursor').attr('x1', tlX(date)).attr('x2', tlX(date));
  }
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);
