const SQMI_TO_SQKM = 2.58999;
const toRad = Math.PI / 180;

export const kmBetween = ([x1, y1], [x2, y2]) => {
  const dx = (x2 - x1) * Math.cos(((y1 + y2) / 2) * toRad) * 111.32;
  const dy = (y2 - y1) * 110.57;
  return Math.hypot(dx, dy);
};

/** Build reach objects with downstream/upstream links from rivers.json. */
export function buildNetwork(rivers) {
  const segs = rivers.features.map((f, i) => ({ i, ...f.properties, coords: f.geometry.coordinates }));
  const bySeq = new Map(segs.map((s) => [s.hydroseq, s]));
  for (const s of segs) {
    s.down = bySeq.get(s.dnhydroseq) ?? null;
    s.ups = [];
  }
  for (const s of segs) if (s.down) s.down.ups.push(s);
  return segs;
}

/**
 * Snap a gauge to the reach that best matches both location (within 4 km) and
 * drainage area. NHDPlus drainage area is at the reach's downstream end.
 */
export function snapGauge(segs, g) {
  const areaKm = g.drainageSqMi * SQMI_TO_SQKM;
  let best = null;
  for (const s of segs) {
    let dmin = Infinity;
    for (const c of s.coords) dmin = Math.min(dmin, kmBetween(c, [g.lon, g.lat]));
    if (dmin > 4) continue;
    const score = dmin / 1.5 + Math.abs(Math.log(s.areaSqKm / areaKm));
    if (!best || score < best.score) best = { s, score };
  }
  return best?.s ?? null;
}

export { SQMI_TO_SQKM };
