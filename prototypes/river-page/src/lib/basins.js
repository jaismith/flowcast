// Basin names from the USGS Watershed Boundary Dataset, written by scripts/fetch_basin_name.py.
const basins = Object.fromEntries(
  Object.values(import.meta.glob('../data/basin-*.json', { eager: true, import: 'default' })).map((b) => [b.site, b]),
);

export function basinFor(site) {
  return basins[site] ?? null;
}

/** "the Upper Delaware and East Branch Delaware watersheds", or null for a single-unit basin. */
export function partsPhrase(basin) {
  const parts = basin?.parts ?? [];
  if (parts.length < 2) return null;
  const names = parts.map((p) => p.name);
  const list = names.length === 2 ? names.join(' and ') : `${names.slice(0, -1).join(', ')} and ${names.at(-1)}`;
  return `the ${list} watersheds`;
}
