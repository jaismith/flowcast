// Basin names from the USGS Watershed Boundary Dataset, written by scripts/fetch_basin_name.py.
const basins = Object.fromEntries(
  Object.values(import.meta.glob('../data/basin-*.json', { eager: true, import: 'default' })).map((b) => [b.site, b]),
);

export function basinFor(site) {
  return basins[site] ?? null;
}

/** "the Upper Delaware and East Branch Delaware watersheds", "the Allagash River watershed", or null. */
export function watershedPhrase(basin) {
  if (!basin) return null;
  const parts = basin.parts?.length ? basin.parts : [basin];
  if (parts.length < 2) return `the ${parts[0].name} watershed`;
  const names = parts.map((p) => p.name);
  const list = names.length === 2 ? names.join(' and ') : `${names.slice(0, -1).join(', ')} and ${names.at(-1)}`;
  return `the ${list} watersheds`;
}
