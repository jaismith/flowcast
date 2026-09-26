import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import * as d3 from 'd3';
import { loadJSON, dataUrl, mountTopbar, mountSource, fmtCfs, DAY, clamp } from '../shared/common.js';

mountTopbar('Watershed 3D', 'three.js terrain · rain, snowpack & sunlight through a year');
mountSource('Terrain: AWS Terrain Tiles (Mapzen terrarium, z10). Weather: Open-Meteo historical API (ERA5/ERA5-Land), 42 grid points, daily. Flow: USGS NWIS daily values. Basin: USGS NLDI; rivers: NHDPlus V2.');

const [terrainMeta, grid, basin, rivers, waterbodies, daily] = await Promise.all([
  loadJSON('terrain.json'),
  loadJSON('weather-grid-daily.json'),
  loadJSON('basin.json'),
  loadJSON('rivers.json'),
  loadJSON('waterbodies.json'),
  loadJSON('callicoon-daily.json'),
]);

// ---------- terrain heights ----------
const img = await new Promise((res, rej) => {
  const im = new Image();
  im.onload = () => res(im);
  im.onerror = rej;
  im.src = dataUrl('terrain.png');
});
const W = terrainMeta.width;
const H = terrainMeta.height;
const cv = document.createElement('canvas');
cv.width = W;
cv.height = H;
const cctx = cv.getContext('2d', { willReadFrequently: true });
cctx.drawImage(img, 0, 0);
const px = cctx.getImageData(0, 0, W, H).data;
const elev = new Float32Array(W * H);
for (let k = 0; k < W * H; k++) elev[k] = px[k * 4] * 256 + px[k * 4 + 1] + px[k * 4 + 2] / 256 - 32768;

const [lon0, lat0, lon1, lat1] = terrainMeta.bbox;
const merc = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
const my0 = merc(lat1);
const my1 = merc(lat0);
const SX = 200;
const SZ = (SX * H) / W;
const metersPerUnit = ((lon1 - lon0) * 111320 * Math.cos((((lat0 + lat1) / 2) * Math.PI) / 180)) / SX;
const EXAG = 3.5;
const vScale = EXAG / metersPerUnit;
const E0 = terrainMeta.minElevM;

/** lon/lat → fractional image coords (u right, v down) */
const lonLatToUV = ([lon, lat]) => [(lon - lon0) / (lon1 - lon0), (merc(lat) - my0) / (my1 - my0)];
function heightAtUV(u, v) {
  const x = clamp(u * (W - 1), 0, W - 1);
  const y = clamp(v * (H - 1), 0, H - 1);
  const x0 = Math.floor(x), y0 = Math.floor(y);
  const x1 = Math.min(W - 1, x0 + 1), y1 = Math.min(H - 1, y0 + 1);
  const fx = x - x0, fy = y - y0;
  const e = elev[y0 * W + x0] * (1 - fx) * (1 - fy) + elev[y0 * W + x1] * fx * (1 - fy) + elev[y1 * W + x0] * (1 - fx) * fy + elev[y1 * W + x1] * fx * fy;
  return (e - E0) * vScale;
}
const uvToXZ = (u, v) => [(u - 0.5) * SX, (v - 0.5) * SZ];
function lonLatToScene(ll, lift = 0) {
  const [u, v] = lonLatToUV(ll);
  const [x, z] = uvToXZ(u, v);
  return new THREE.Vector3(x, heightAtUV(u, v) + lift, z);
}

// ---------- renderer & scene ----------
const container = document.getElementById('scene');
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setClearColor(0x05080f);
container.appendChild(renderer.domElement);
const labelRenderer = new CSS2DRenderer();
labelRenderer.setSize(window.innerWidth, window.innerHeight);
Object.assign(labelRenderer.domElement.style, { position: 'absolute', inset: '0', pointerEvents: 'none' });
container.appendChild(labelRenderer.domElement);

const scene = new THREE.Scene();
scene.fog = new THREE.Fog(0x05080f, 180, 420);
const camera = new THREE.PerspectiveCamera(40, window.innerWidth / window.innerHeight, 0.5, 1500);
camera.position.set(-55, 120, 185);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(8, 0, 4);
controls.enableDamping = true;
controls.autoRotate = true;
controls.autoRotateSpeed = 0.35;
controls.maxPolarAngle = Math.PI * 0.47;
controls.minDistance = 30;
controls.maxDistance = 360;

// ---------- weather field (IDW weights precomputed once) ----------
const TW = 160;
const TH = Math.round((TW * H) / W);
const pts = grid.points.map(lonLatToUV);
const NP = pts.length;
const weights = new Float32Array(TW * TH * NP);
for (let j = 0; j < TH; j++) {
  for (let i = 0; i < TW; i++) {
    const u = (i + 0.5) / TW;
    const v = (j + 0.5) / TH;
    let sum = 0;
    const base = (j * TW + i) * NP;
    for (let p = 0; p < NP; p++) {
      const du = (u - pts[p][0]) * (SX / SZ);
      const dv = v - pts[p][1];
      const wgt = 1 / Math.pow(du * du + dv * dv + 1e-4, 1.6);
      weights[base + p] = wgt;
      sum += wgt;
    }
    for (let p = 0; p < NP; p++) weights[base + p] /= sum;
  }
}
const fieldData = new Uint8Array(TW * TH * 4);
const fieldTex = new THREE.DataTexture(fieldData, TW, TH, THREE.RGBAFormat);
fieldTex.magFilter = THREE.LinearFilter;
fieldTex.minFilter = THREE.LinearFilter;
fieldTex.flipY = false;
const field = { precip: new Float32Array(TW * TH), snow: new Float32Array(TW * TH), temp: new Float32Array(TW * TH) };
const PRECIP_MAX = 1.6; // in/day
const SNOW_MAX = 0.8; // m
const RAD_MAX = 32; // MJ/m²/day
const T_MIN = -5;
const T_MAX = 90;

const dayVals = { precip: new Float32Array(NP), snow: new Float32Array(NP), rad: new Float32Array(NP), temp: new Float32Array(NP) };
function updateField(dayF) {
  const d0 = clamp(Math.floor(dayF), 0, grid.days - 1);
  const d1 = Math.min(grid.days - 1, d0 + 1);
  const f = dayF - d0;
  for (let p = 0; p < NP; p++) {
    dayVals.precip[p] = grid.precip[p][d0] * (1 - f) + grid.precip[p][d1] * f;
    dayVals.snow[p] = grid.snowDepth[p][d0] * (1 - f) + grid.snowDepth[p][d1] * f;
    dayVals.rad[p] = grid.radiation[p][d0] * (1 - f) + grid.radiation[p][d1] * f;
    dayVals.temp[p] = grid.temp[p][d0] * (1 - f) + grid.temp[p][d1] * f;
  }
  for (let t = 0; t < TW * TH; t++) {
    let pr = 0, sn = 0, ra = 0, te = 0;
    const base = t * NP;
    for (let p = 0; p < NP; p++) {
      const wgt = weights[base + p];
      pr += wgt * dayVals.precip[p];
      sn += wgt * dayVals.snow[p];
      ra += wgt * dayVals.rad[p];
      te += wgt * dayVals.temp[p];
    }
    field.precip[t] = pr;
    field.snow[t] = sn;
    field.temp[t] = te;
    fieldData[t * 4] = clamp((Math.sqrt(pr / PRECIP_MAX)) * 255, 0, 255);
    fieldData[t * 4 + 1] = clamp((sn / SNOW_MAX) * 255, 0, 255);
    fieldData[t * 4 + 2] = clamp((ra / RAD_MAX) * 255, 0, 255);
    fieldData[t * 4 + 3] = clamp(((te - T_MIN) / (T_MAX - T_MIN)) * 255, 0, 255);
  }
  fieldTex.needsUpdate = true;
}

// Basin mask texture (inside = white)
const maskCanvas = document.createElement('canvas');
maskCanvas.width = 1024;
maskCanvas.height = Math.round((1024 * H) / W);
{
  const mctx = maskCanvas.getContext('2d');
  mctx.fillStyle = '#000';
  mctx.fillRect(0, 0, maskCanvas.width, maskCanvas.height);
  mctx.fillStyle = '#fff';
  mctx.filter = 'blur(2px)';
  mctx.beginPath();
  basin.features[0].geometry.coordinates[0].forEach((c, k) => {
    const [u, v] = lonLatToUV(c);
    const x = u * maskCanvas.width;
    const y = v * maskCanvas.height;
    k ? mctx.lineTo(x, y) : mctx.moveTo(x, y);
  });
  mctx.fill();
}
const maskTex = new THREE.CanvasTexture(maskCanvas);
maskTex.flipY = false;

// ---------- terrain mesh ----------
const segX = W / 2 - 1;
const segZ = Math.round(H / 2) - 1;
const geom = new THREE.PlaneGeometry(SX, SZ, segX, segZ);
geom.rotateX(-Math.PI / 2);
const pos = geom.attributes.position;
const uvs = geom.attributes.uv;
const elevAttr = new Float32Array(pos.count);
for (let k = 0; k < pos.count; k++) {
  const u = (pos.getX(k) + SX / 2) / SX;
  const v = (pos.getZ(k) + SZ / 2) / SZ;
  const hgt = heightAtUV(u, v);
  pos.setY(k, hgt);
  uvs.setXY(k, u, v);
  elevAttr[k] = hgt / vScale + E0;
}
geom.setAttribute('elev', new THREE.BufferAttribute(elevAttr, 1));
geom.computeVertexNormals();

const uniforms = {
  uField: { value: fieldTex },
  uMask: { value: maskTex },
  uMode: { value: 0 },
  uSun: { value: new THREE.Vector3(0.4, 0.8, 0.3).normalize() },
  uElevRange: { value: new THREE.Vector2(terrainMeta.minElevM, terrainMeta.maxElevM) },
};
const terrainMat = new THREE.ShaderMaterial({
  uniforms,
  vertexShader: /* glsl */ `
    attribute float elev;
    varying vec2 vUv;
    varying vec3 vNormal;
    varying float vElev;
    varying float vDepth;
    void main() {
      vUv = uv;
      vNormal = normal;
      vElev = elev;
      vec4 mv = modelViewMatrix * vec4(position, 1.0);
      vDepth = -mv.z;
      gl_Position = projectionMatrix * mv;
    }`,
  fragmentShader: /* glsl */ `
    uniform sampler2D uField;
    uniform sampler2D uMask;
    uniform int uMode;
    uniform vec3 uSun;
    uniform vec2 uElevRange;
    varying vec2 vUv;
    varying vec3 vNormal;
    varying float vElev;
    varying float vDepth;

    vec3 ramp(float t, vec3 a, vec3 b, vec3 c, vec3 d) {
      t = clamp(t, 0.0, 1.0);
      if (t < 0.333) return mix(a, b, t / 0.333);
      if (t < 0.666) return mix(b, c, (t - 0.333) / 0.333);
      return mix(c, d, (t - 0.666) / 0.334);
    }

    void main() {
      vec4 f = texture2D(uField, vUv);
      float mask = texture2D(uMask, vUv).r;
      float e = (vElev - uElevRange.x) / (uElevRange.y - uElevRange.x);
      vec3 base = ramp(e, vec3(0.10, 0.22, 0.16), vec3(0.18, 0.32, 0.17), vec3(0.36, 0.38, 0.24), vec3(0.62, 0.58, 0.50));
      vec3 n = normalize(vNormal);
      float diff = max(dot(n, normalize(uSun)), 0.0);
      float shade = 0.28 + 0.9 * diff;
      vec3 col = base * shade;

      if (uMode == 0) {
        float snow = smoothstep(0.03, 0.7, f.g) * (0.45 + 0.4 * smoothstep(0.1, 0.9, e + f.g));
        col = mix(col, vec3(0.93, 0.96, 1.0) * (0.45 + 0.6 * diff), snow);
        float rain = f.r;
        vec3 rainCol = ramp(rain, vec3(0.2, 0.3, 0.9), vec3(0.35, 0.35, 1.0), vec3(0.6, 0.3, 1.0), vec3(1.0, 0.4, 0.9));
        col = mix(col, rainCol * (0.55 + 0.5 * diff), smoothstep(0.08, 1.0, rain) * 0.72);
      } else if (uMode == 1) {
        vec3 c = ramp(f.b, vec3(0.12, 0.05, 0.3), vec3(0.6, 0.1, 0.45), vec3(0.98, 0.45, 0.1), vec3(1.0, 0.95, 0.55));
        col = mix(col, c * (0.45 + 0.7 * diff), 0.78);
      } else {
        vec3 c = ramp(f.a, vec3(0.2, 0.35, 0.95), vec3(0.4, 0.85, 0.95), vec3(1.0, 0.85, 0.4), vec3(0.95, 0.25, 0.15));
        col = mix(col, c * (0.45 + 0.7 * diff), 0.75);
      }
      // Outside the basin: dim and desaturate.
      float g = dot(col, vec3(0.3, 0.59, 0.11));
      col = mix(vec3(g) * 0.35, col, 0.15 + 0.85 * mask);
      float fog = smoothstep(180.0, 420.0, vDepth);
      col = mix(col, vec3(0.02, 0.03, 0.06), fog);
      gl_FragColor = vec4(col, 1.0);
    }`,
});
scene.add(new THREE.Mesh(geom, terrainMat));

// Skirt base so the block reads as a physical model
{
  const skirt = new THREE.Mesh(new THREE.BoxGeometry(SX, 2, SZ), new THREE.MeshBasicMaterial({ color: 0x0a1222 }));
  skirt.position.y = -1.05;
  scene.add(skirt);
}

// ---------- rivers with flowing glow ----------
const riverPos = [];
const riverDist = [];
const riverOrder = [];
for (const f of rivers.features) {
  const c = f.geometry.coordinates;
  let dist = 0;
  let prev = null;
  const pts3 = c.map((ll) => lonLatToScene(ll, 0.25));
  for (let k = 0; k < pts3.length - 1; k++) {
    const a = pts3[k];
    const b = pts3[k + 1];
    if (prev) dist += 0;
    riverPos.push(a.x, a.y, a.z, b.x, b.y, b.z);
    const seg = a.distanceTo(b);
    riverDist.push(-f.properties.pathKm * 1.3 - dist, -f.properties.pathKm * 1.3 - dist - seg);
    dist += seg;
    riverOrder.push(f.properties.order, f.properties.order);
    prev = b;
  }
}
const riverGeom = new THREE.BufferGeometry();
riverGeom.setAttribute('position', new THREE.Float32BufferAttribute(riverPos, 3));
riverGeom.setAttribute('dist', new THREE.Float32BufferAttribute(riverDist, 1));
riverGeom.setAttribute('order', new THREE.Float32BufferAttribute(riverOrder, 1));
const riverUniforms = { uPhase: { value: 0 } };
const riverMat = new THREE.ShaderMaterial({
  uniforms: riverUniforms,
  transparent: true,
  depthWrite: false,
  blending: THREE.AdditiveBlending,
  vertexShader: /* glsl */ `
    attribute float dist;
    attribute float order;
    varying float vDist;
    varying float vOrder;
    void main() {
      vDist = dist;
      vOrder = order;
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }`,
  fragmentShader: /* glsl */ `
    uniform float uPhase;
    varying float vDist;
    varying float vOrder;
    void main() {
      float pulse = 0.5 + 0.5 * sin(vDist * 1.6 + uPhase);
      pulse = pow(pulse, 6.0);
      float base = 0.35 + vOrder * 0.12;
      vec3 col = mix(vec3(0.2, 0.55, 0.95), vec3(0.75, 1.0, 1.0), pulse);
      gl_FragColor = vec4(col, base + pulse * 0.7);
    }`,
});
scene.add(new THREE.LineSegments(riverGeom, riverMat));

// Reservoirs
for (const f of waterbodies.features.filter((d) => d.properties.areaSqKm > 1)) {
  const polys = f.geometry.type === 'MultiPolygon' ? f.geometry.coordinates : [f.geometry.coordinates];
  for (const poly of polys) {
    const ring = poly[0];
    const shape = new THREE.Shape(ring.map((ll) => { const [u, v] = lonLatToUV(ll); const [x, z] = uvToXZ(u, v); return new THREE.Vector2(x, -z); }));
    const g = new THREE.ShapeGeometry(shape);
    g.rotateX(-Math.PI / 2);
    const hMin = d3.min(ring, (ll) => heightAtUV(...lonLatToUV(ll)));
    const m = new THREE.Mesh(g, new THREE.MeshBasicMaterial({ color: 0x1f6fb0, transparent: true, opacity: 0.85 }));
    m.position.y = hMin + 0.3;
    scene.add(m);
  }
}

// Basin outline
{
  const ring = basin.features[0].geometry.coordinates[0].map((ll) => lonLatToScene(ll, 0.6));
  const g = new THREE.BufferGeometry().setFromPoints(ring);
  scene.add(new THREE.Line(g, new THREE.LineBasicMaterial({ color: 0x80ffdb, transparent: true, opacity: 0.8 })));
}

// Callicoon flow beam
const gaugeLL = [-75.0574167, 41.75675];
const gaugePos = lonLatToScene(gaugeLL, 0);
const beam = new THREE.Mesh(new THREE.CylinderGeometry(0.7, 0.7, 1, 24, 1, true), new THREE.MeshBasicMaterial({ color: 0x80ffdb, transparent: true, opacity: 0.55, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide }));
beam.position.copy(gaugePos);
scene.add(beam);

function label(text, ll, lift, cls = '') {
  const el = document.createElement('div');
  el.className = `lbl ${cls}`;
  el.textContent = text;
  const o = new CSS2DObject(el);
  o.position.copy(lonLatToScene(ll, lift));
  scene.add(o);
  return el;
}
const gaugeLabel = label('Callicoon gauge', gaugeLL, 4, 'gauge');
label('Cannonsville Res.', [-75.32, 42.07], 3);
label('Pepacton Res.', [-74.93, 42.09], 3);
label('Hancock', [-75.28, 41.955], 3);
label('Catskills', [-74.47, 42.2], 8);

// ---------- precipitation particles ----------
const RAIN_N = 7000;
const SNOW_N = 6000;
const rainPos = new Float32Array(RAIN_N * 6);
const rainGeom = new THREE.BufferGeometry();
rainGeom.setAttribute('position', new THREE.BufferAttribute(rainPos, 3));
const rain = new THREE.LineSegments(rainGeom, new THREE.LineBasicMaterial({ color: 0xa8b8ff, transparent: true, opacity: 0.55, depthWrite: false }));
scene.add(rain);
const snowPos = new Float32Array(SNOW_N * 3);
const snowGeom = new THREE.BufferGeometry();
snowGeom.setAttribute('position', new THREE.BufferAttribute(snowPos, 3));
const flake = (() => {
  const c = document.createElement('canvas');
  c.width = c.height = 32;
  const g = c.getContext('2d');
  const grd = g.createRadialGradient(16, 16, 0, 16, 16, 16);
  grd.addColorStop(0, 'rgba(255,255,255,1)');
  grd.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grd;
  g.fillRect(0, 0, 32, 32);
  return new THREE.CanvasTexture(c);
})();
const snow = new THREE.Points(snowGeom, new THREE.PointsMaterial({ size: 0.9, map: flake, transparent: true, depthWrite: false, color: 0xffffff }));
scene.add(snow);

const drops = { rain: [], snow: [] };
for (let k = 0; k < RAIN_N; k++) drops.rain.push({ x: 0, y: -999, z: 0, v: 0, ground: 0 });
for (let k = 0; k < SNOW_N; k++) drops.snow.push({ x: 0, y: -999, z: 0, v: 0, ground: 0, ph: Math.random() * 6.28 });

function spawn(d, isSnow) {
  // Rejection sample a location weighted by local precipitation, restricted to the right phase.
  for (let tries = 0; tries < 30; tries++) {
    const t = Math.floor(Math.random() * TW * TH);
    const p = field.precip[t];
    const cold = field.temp[t] < 33;
    if (cold !== isSnow) continue;
    if (Math.random() * PRECIP_MAX * 0.6 > p) continue;
    const u = ((t % TW) + Math.random()) / TW;
    const v = (Math.floor(t / TW) + Math.random()) / TH;
    const [x, z] = uvToXZ(u, v);
    d.x = x;
    d.z = z;
    d.ground = heightAtUV(u, v);
    d.y = d.ground + 18 + Math.random() * 22;
    d.v = isSnow ? 3 + Math.random() * 2 : 38 + Math.random() * 14;
    return true;
  }
  d.y = -999;
  return false;
}

let basinPrecip = 0;
let basinSnowFrac = 0;
function updateDrops(dt, t) {
  const activeRain = Math.round(clamp(basinPrecip * (1 - basinSnowFrac) / 1.0, 0, 1) * RAIN_N);
  const activeSnow = Math.round(clamp(basinPrecip * basinSnowFrac / 0.6, 0, 1) * SNOW_N);
  drops.rain.forEach((d, k) => {
    if (k >= activeRain) { if (d.y > -900) d.y = -999; } else if (d.y < d.ground) spawn(d, false);
    d.y -= d.v * dt;
    const o = k * 6;
    rainPos[o] = d.x; rainPos[o + 1] = d.y; rainPos[o + 2] = d.z;
    rainPos[o + 3] = d.x + 0.15; rainPos[o + 4] = d.y + 1.1; rainPos[o + 5] = d.z;
  });
  drops.snow.forEach((d, k) => {
    if (k >= activeSnow) { if (d.y > -900) d.y = -999; } else if (d.y < d.ground) spawn(d, true);
    d.y -= d.v * dt;
    const o = k * 3;
    snowPos[o] = d.x + Math.sin(t * 1.3 + d.ph) * 0.6;
    snowPos[o + 1] = d.y;
    snowPos[o + 2] = d.z + Math.cos(t * 0.9 + d.ph) * 0.6;
  });
  rainGeom.attributes.position.needsUpdate = true;
  snowGeom.attributes.position.needsUpdate = true;
}

// ---------- time series for UI ----------
const gridStart = Date.parse(grid.start);
const dates = d3.range(grid.days).map((d) => new Date(gridStart + d * DAY));
const meanPrecip = dates.map((_, d) => d3.mean(grid.precip, (p) => p[d]));
const meanSnow = dates.map((_, d) => d3.mean(grid.snowDepth, (p) => p[d]));
const meanRad = dates.map((_, d) => d3.mean(grid.radiation, (p) => p[d]));
const meanTemp = dates.map((_, d) => d3.mean(grid.temp, (p) => p[d]));
const dailyStart = Date.parse(daily.start);
const qAt = (date) => daily.q[Math.round((date - dailyStart) / DAY)] ?? null;
const qSeries = dates.map(qAt);

const tl = d3.select('#timeline svg');
let x;
function drawTimeline() {
  const node = tl.node();
  const w = node.clientWidth;
  const h = node.clientHeight;
  const m = { l: 44, r: 44, t: 8, b: 18 };
  x = d3.scaleUtc(d3.extent(dates), [m.l, w - m.r]);
  const yq = d3.scaleLog([Math.max(100, d3.min(qSeries)), d3.max(qSeries) * 1.1], [h - m.b, m.t + 10]);
  const ys = d3.scaleLinear([0, d3.max(meanSnow) * 1.1], [h - m.b, m.t + 20]);
  const yp = d3.scaleLinear([0, d3.max(meanPrecip)], [m.t, (h - m.b) * 0.5]);
  tl.selectAll('*').remove();
  tl.append('g').attr('class', 'axis').attr('transform', `translate(0,${h - m.b})`).call(d3.axisBottom(x).ticks(12).tickSizeOuter(0));
  tl.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(yq).ticks(3, '~s'));
  tl.append('path').attr('d', d3.area().x((_, i) => x(dates[i])).y0(h - m.b).y1((v) => ys(v))(meanSnow)).attr('fill', 'rgba(241,245,255,0.18)').attr('stroke', 'rgba(241,245,255,0.5)');
  const bw = Math.max(1, (w - m.l - m.r) / dates.length - 0.5);
  tl.append('g').selectAll('rect').data(meanPrecip).join('rect')
    .attr('x', (_, i) => x(dates[i])).attr('y', m.t).attr('width', bw).attr('height', (v) => yp(v) - m.t)
    .attr('fill', (_, i) => (meanTemp[i] < 32 ? 'rgba(241,245,255,0.8)' : 'rgba(123,140,255,0.85)'));
  tl.append('path').attr('d', d3.line().defined((v) => v != null).x((_, i) => x(dates[i])).y((v) => yq(v))(qSeries)).attr('fill', 'none').attr('stroke', '#80ffdb').attr('stroke-width', 1.3);
  tl.append('text').attr('x', w - m.r).attr('y', h - m.b - 8).attr('text-anchor', 'end').style('font-size', '11px').text('Callicoon flow (cfs, log) · snow depth (white area) · daily precip bars (white = below freezing)');
  tl.append('line').attr('class', 'cursor').attr('y1', m.t).attr('y2', h - m.b).attr('stroke', '#ffb703').attr('stroke-width', 1.5);
  tl.on('pointerdown pointermove', (event) => {
    if (event.type === 'pointermove' && event.buttons !== 1) return;
    dayF = clamp((x.invert(d3.pointer(event)[0]) - gridStart) / DAY, 0, grid.days - 1);
  });
}
drawTimeline();

// ---------- legend & modes ----------
const MODES = [
  { key: 'water', lo: 'dry', hi: '1.6 in/day', colors: ['#3346e6', '#5a59ff', '#9a4dff', '#ff66e6'] },
  { key: 'sun', lo: '0', hi: `${RAD_MAX} MJ/m²`, colors: ['#1f0d4d', '#9a1a73', '#fa731a', '#fff28c'] },
  { key: 'temp', lo: `${T_MIN} °F`, hi: `${T_MAX} °F`, colors: ['#3359f2', '#66d9f2', '#ffd966', '#f24026'] },
];
let mode = 0;
function drawLegend() {
  const c = document.getElementById('legend').getContext('2d');
  const g = c.createLinearGradient(0, 0, 150, 0);
  MODES[mode].colors.forEach((col, k) => g.addColorStop(k / 3, col));
  c.fillStyle = g;
  c.fillRect(0, 0, 150, 8);
  document.getElementById('lg-lo').textContent = MODES[mode].lo;
  document.getElementById('lg-hi').textContent = MODES[mode].hi;
}
MODES.forEach((m, k) => {
  document.getElementById(`m-${m.key}`).onclick = () => {
    mode = k;
    uniforms.uMode.value = k;
    MODES.forEach((mm, kk) => document.getElementById(`m-${mm.key}`).classList.toggle('active', kk === k));
    drawLegend();
  };
});
drawLegend();

let playing = true;
let speed = 5;
const playBtn = document.getElementById('play');
playBtn.onclick = () => { playing = !playing; playBtn.textContent = playing ? '❚❚ Pause' : '▶ Play'; };
document.getElementById('speed').onchange = (e) => { speed = +e.target.value; };
const rotBtn = document.getElementById('rotate');
rotBtn.onclick = () => { controls.autoRotate = !controls.autoRotate; rotBtn.classList.toggle('active', controls.autoRotate); };
window.addEventListener('keydown', (e) => { if (e.code === 'Space') { e.preventDefault(); playBtn.click(); } });

window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
  labelRenderer.setSize(window.innerWidth, window.innerHeight);
  drawTimeline();
});

// ---------- loop ----------
// Start in early winter so snow and melt arrive quickly; ?day=YYYY-MM-DD&mode=sun|temp override.
const params = new URLSearchParams(location.search);
let dayF = Math.max(0, params.has('day') ? Math.round((Date.parse(params.get('day')) - gridStart) / DAY) : dates.findIndex((d) => d.getUTCMonth() === 11 && d.getUTCDate() === 1));
if (params.get('paused') != null) playBtn.click();
{
  const k = MODES.findIndex((m) => m.key === params.get('mode'));
  if (k > 0) document.getElementById(`m-${MODES[k].key}`).click();
}
const fmtDay = d3.utcFormat('%a %b %-d, %Y');
let lastT = performance.now();
let phase = 0;
let uiAcc = 1;
const qMedian = d3.median(qSeries.filter((v) => v != null));

function sunDir(date) {
  const doy = d3.utcDay.count(d3.utcYear(date), date);
  const decl = 23.44 * Math.sin(((2 * Math.PI) / 365) * (doy - 81));
  const elevDeg = 90 - 42 + decl;
  const el = (elevDeg * Math.PI) / 180;
  return new THREE.Vector3(-0.3 * Math.cos(el), Math.sin(el), Math.cos(el)).normalize();
}

function animate(now) {
  const dt = Math.min(0.05, (now - lastT) / 1000);
  lastT = now;
  if (playing) {
    dayF += dt * speed;
    if (dayF > grid.days - 1) dayF = 0;
  }
  updateField(dayF);
  const di = clamp(Math.round(dayF), 0, grid.days - 1);
  const date = dates[di];
  basinPrecip = meanPrecip[di];
  basinSnowFrac = clamp((34 - meanTemp[di]) / 6, 0, 1);
  updateDrops(dt, now / 1000);
  uniforms.uSun.value.copy(sunDir(date));
  const q = qSeries[di] ?? qMedian;
  phase += dt * (1.5 + 5 * Math.sqrt(q / qMedian));
  riverUniforms.uPhase.value = phase;
  const beamH = 2 + 26 * Math.sqrt(q / 30000);
  beam.scale.set(1, beamH, 1);
  beam.position.y = gaugePos.y + beamH / 2;
  controls.update();
  renderer.render(scene, camera);
  labelRenderer.render(scene, camera);
  if ((uiAcc += dt) > 0.1) {
    uiAcc = 0;
    document.getElementById('date').textContent = fmtDay(date);
    document.getElementById('s-precip').textContent = `${meanPrecip[di].toFixed(2)} in`;
    document.getElementById('s-snow').textContent = `${(meanSnow[di] * 39.37).toFixed(1)} in`;
    document.getElementById('s-rad').textContent = `${meanRad[di].toFixed(1)} MJ/m²`;
    document.getElementById('s-q').textContent = fmtCfs(q);
    gaugeLabel.textContent = `Callicoon · ${fmtCfs(q)}`;
    const cx = x(date);
    tl.select('.cursor').attr('x1', cx).attr('x2', cx);
  }
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);
