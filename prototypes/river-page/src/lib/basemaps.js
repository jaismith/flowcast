// Keyless basemaps to compare. Vector styles get our terrain hillshade on top; raster styles that already
// carry relief (or imagery) do not. Stadia (Stamen) serves localhost without a key but needs one in production.

const OFM_GLYPHS = 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf';
const OSM = ['OpenStreetMap contributors', 'https://www.openstreetmap.org/copyright'];
const NOTO = { regular: ['Noto Sans Regular'], bold: ['Noto Sans Bold'] };

const raster = (tiles, { maxzoom = 18, overlay } = {}) => ({
  version: 8,
  glyphs: OFM_GLYPHS,
  sources: {
    base: { type: 'raster', tiles: [tiles], tileSize: 256, maxzoom },
    ...(overlay ? { overlay: { type: 'raster', tiles: [overlay], tileSize: 256, maxzoom } } : {}),
  },
  layers: [
    { id: 'base', type: 'raster', source: 'base' },
    ...(overlay ? [{ id: 'overlay', type: 'raster', source: 'overlay' }] : []),
  ],
});

const usgs = (name) => `https://basemap.nationalmap.gov/arcgis/rest/services/${name}/MapServer/tile/{z}/{y}/{x}`;
const esri = (name) => `https://server.arcgisonline.com/ArcGIS/rest/services/${name}/MapServer/tile/{z}/{y}/{x}`;

export const BASEMAPS = {
  positron: {
    label: 'OpenFreeMap Positron',
    style: 'https://tiles.openfreemap.org/styles/positron',
    hillshade: true,
    fonts: NOTO,
    credits: [['OpenFreeMap', 'https://openfreemap.org'], ['OpenMapTiles', 'https://www.openmaptiles.org/'], OSM],
  },
  bright: {
    label: 'OpenFreeMap Bright',
    style: 'https://tiles.openfreemap.org/styles/bright',
    hillshade: true,
    fonts: NOTO,
    credits: [['OpenFreeMap', 'https://openfreemap.org'], ['OpenMapTiles', 'https://www.openmaptiles.org/'], OSM],
  },
  liberty: {
    label: 'OpenFreeMap Liberty',
    style: 'https://tiles.openfreemap.org/styles/liberty',
    hillshade: true,
    fonts: NOTO,
    credits: [['OpenFreeMap', 'https://openfreemap.org'], ['OpenMapTiles', 'https://www.openmaptiles.org/'], OSM],
  },
  versatilesGray: {
    label: 'VersaTiles Gray',
    style: 'https://tiles.versatiles.org/assets/styles/gray/style.json',
    hillshade: true,
    fonts: { regular: ['noto_sans_regular'], bold: ['noto_sans_bold'] },
    credits: [['VersaTiles', 'https://versatiles.org'], OSM],
  },
  versatilesColorful: {
    label: 'VersaTiles Colorful',
    style: 'https://tiles.versatiles.org/assets/styles/colorful/style.json',
    hillshade: true,
    fonts: { regular: ['noto_sans_regular'], bold: ['noto_sans_bold'] },
    credits: [['VersaTiles', 'https://versatiles.org'], OSM],
  },
  usgsRelief: {
    label: 'USGS relief + hydro',
    style: raster(usgs('USGSShadedReliefOnly'), { maxzoom: 16, overlay: usgs('USGSHydroCached') }),
    hillshade: false,
    fonts: NOTO,
    credits: [['USGS The National Map', 'https://www.usgs.gov/programs/national-geospatial-program/national-map']],
  },
  usgsTopo: {
    label: 'USGS Topo',
    style: raster(usgs('USGSTopo'), { maxzoom: 16 }),
    hillshade: false,
    fonts: NOTO,
    credits: [['USGS The National Map', 'https://www.usgs.gov/programs/national-geospatial-program/national-map']],
  },
  stamenTerrain: {
    label: 'Stamen Terrain',
    style: raster('https://tiles.stadiamaps.com/tiles/stamen_terrain/{z}/{x}/{y}.png'),
    hillshade: false,
    fonts: NOTO,
    credits: [['Stadia Maps', 'https://stadiamaps.com/'], ['Stamen Design', 'https://stamen.com/'], ['OpenMapTiles', 'https://www.openmaptiles.org/'], OSM],
  },
  stamenToner: {
    label: 'Stamen Toner Lite',
    style: raster('https://tiles.stadiamaps.com/tiles/stamen_toner_lite/{z}/{x}/{y}.png'),
    hillshade: true,
    fonts: NOTO,
    credits: [['Stadia Maps', 'https://stadiamaps.com/'], ['Stamen Design', 'https://stamen.com/'], ['OpenMapTiles', 'https://www.openmaptiles.org/'], OSM],
  },
  openTopo: {
    label: 'OpenTopoMap',
    style: raster('https://a.tile.opentopomap.org/{z}/{x}/{y}.png', { maxzoom: 17 }),
    hillshade: false,
    fonts: NOTO,
    credits: [['OpenTopoMap', 'https://opentopomap.org'], ['SRTM', 'https://www2.jpl.nasa.gov/srtm/'], OSM],
  },
  imagery: {
    label: 'Esri satellite',
    style: raster(esri('World_Imagery'), { maxzoom: 18 }),
    hillshade: false,
    fonts: NOTO,
    credits: [['Esri, Maxar, Earthstar Geographics', 'https://www.esri.com/']],
  },
  dark: {
    label: 'OpenFreeMap Dark',
    style: 'https://tiles.openfreemap.org/styles/dark',
    hillshade: true,
    fonts: NOTO,
    credits: [['OpenFreeMap', 'https://openfreemap.org'], ['OpenMapTiles', 'https://www.openmaptiles.org/'], OSM],
  },
};

/** The basemap to use: an explicit choice, else the theme's default. */
export function basemapFor(name, themeDefault) {
  return BASEMAPS[name] ?? BASEMAPS[themeDefault] ?? BASEMAPS.positron;
}
