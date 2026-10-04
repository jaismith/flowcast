/** From the bundle's `site.watershed` (USGS Watershed Boundary Dataset): "the Upper Delaware and East Branch Delaware watersheds", "the Allagash River watershed", or null. */
export function watershedPhrase(basin) {
  if (!basin) return null;
  const parts = basin.parts?.length ? basin.parts : [basin];
  if (parts.length < 2) return `the ${parts[0].name} watershed`;
  const names = parts.map((p) => p.name);
  const list = names.length === 2 ? names.join(' and ') : `${names.slice(0, -1).join(', ')} and ${names.at(-1)}`;
  return `the ${list} watersheds`;
}
