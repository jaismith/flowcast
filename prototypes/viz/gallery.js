import * as d3 from 'd3';
import { loadJSON } from './shared/common.js';

const PROTOTYPES = [
  {
    slug: 'river-pulse',
    title: 'River Pulse',
    blurb: 'Water parcels flow down all 618 NHDPlus reaches. Each reach\'s discharge comes from the nearest gauge downstream, scaled by drainage area, and the parcels are colored by flow vs normal or by water temperature. Replays the July 2026 flood or the last 30 days.',
    tags: ['D3 + Canvas', 'particles', 'NWIS 27 gauges', 'NHDPlus'],
  },
  {
    slug: 'watershed-3d',
    title: 'Watershed 3D',
    blurb: 'Real terrain in three.js with a year of ECMWF IFS weather draped over it: rain streaks, falling snow, snowpack building and melting, solar radiation and air temperature. River glow speed follows Callicoon discharge.',
    tags: ['three.js', 'GLSL', 'IFS 9 km grid', 'terrain tiles'],
  },
  {
    slug: 'river-pulse-v2',
    title: 'River Pulse v2 · rain, sun & melt',
    blurb: 'River Pulse with hourly weather. A radar-style rain field drifts over the basin, sunlit slopes glow from the true sun position times gridded shortwave, and snowpack melts. Every raindrop or melt parcel lands, trickles to a stream and rides downstream, so you can watch the lag before it reaches Callicoon.',
    tags: ['D3 + Canvas', 'hourly IFS 9 km grid', 'tracers', 'solar position'],
  },
  {
    slug: 'watershed-3d-v2',
    title: 'Watershed 3D v2 · hourly sun & snow',
    blurb: 'The sun moves hour by hour, lighting slopes with true hillshade, and a shortwave-on-slopes mode sits alongside. Billboard clouds and rain columns follow hourly precipitation, a displaced snowpack melts back from sunny slopes first, and runoff trickles follow steepest descent into the channels.',
    tags: ['three.js', 'GLSL', 'day/night', 'snowpack', 'runoff paths'],
  },
  {
    slug: 'forecast-fan',
    title: 'Forecast Fan',
    blurb: 'flowcast\'s live 7-day forecast unfurls hour by hour, with a distribution slice at the leading edge. Linked to the GEFS rain plume and a 30-day GloFAS ensemble ridgeline, with flowcast\'s medians overlaid for comparison.',
    tags: ['D3', 'uncertainty', 'flowcast live', 'GEFS', 'GloFAS'],
  },
  {
    slug: 'storm-explorer',
    title: 'Storm Explorer',
    blurb: 'A year of hourly data with linked brushing across rain and snow, discharge (baseflow separated), water and air temperature, snowpack and sun. Auto-detected storms show rain-to-peak lag and runoff ratio.',
    tags: ['D3', 'linked brushing', 'hyetograph', 'event analytics'],
  },
  {
    slug: 'river-year',
    title: 'River Year',
    blurb: 'This year against 50 years of history. A radial climatology with percentile bands morphs into a spiral of every day since 1975, colored by departure from normal. Works for flow and for water temperature.',
    tags: ['D3 + Canvas', 'climatology', 'NWIS daily 1975–2026'],
  },
  {
    slug: 'raindrop-journey',
    title: "A Raindrop's Journey",
    blurb: 'Scroll to follow one drop 179 km from a Catskills hillside, through Cannonsville Reservoir, to the Callicoon gauge. Uses 3D satellite terrain, the real NLDI flow path, and gauge readings from the storm.',
    tags: ['MapLibre 3D', 'scrollytelling', 'NLDI path', 'storytelling'],
  },
  {
    slug: 'flood-wave',
    title: 'Flood Wave',
    blurb: 'A joy-plot of every gauge\'s hydrograph, ordered from headwaters to Callicoon. As the time sweep plays, peaks ripple on the ridges and on the map, so you can watch the flood wave travel downstream.',
    tags: ['D3', 'small multiples', 'wave propagation'],
  },
];

d3.select('#grid').selectAll('a').data(PROTOTYPES).join('a')
  .attr('class', 'card')
  .attr('href', (d) => `./${d.slug}/`)
  .html((d, i) => `
    <div class="thumb" style="background-image:url('./thumbs/${d.slug}.jpg')"><span class="n">${i + 1}</span></div>
    <div class="body">
      <h2>${d.title}</h2>
      <p>${d.blurb}</p>
      <div class="tags">${d.tags.map((t) => `<span class="tag">${t}</span>`).join('')}</div>
    </div>`);

const manifest = await loadJSON('manifest.json');
d3.select('#meta').html(`<span>Data cached ${manifest.fetchedAt.slice(0, 10)}</span><span>Delaware River at Callicoon, NY · USGS 01427510 · 1,820 sq mi</span><span>All real data; interpolated or conceptual parts are labeled on each page</span>`);

// Background: the real river network, drawn with slowly flowing dashes.
const rivers = await loadJSON('rivers.json');
const svg = d3.select('#bg');
function drawBg() {
  const w = innerWidth;
  const h = innerHeight;
  const proj = d3.geoMercator().fitExtent([[w * 0.35, -h * 0.1], [w * 1.15, h * 1.1]], rivers);
  const path = d3.geoPath(proj);
  svg.selectAll('*').remove();
  svg.append('style').text('@keyframes flow { to { stroke-dashoffset: -40; } } .fl { animation: flow 2.2s linear infinite; }');
  svg.append('g').selectAll('path').data(rivers.features).join('path')
    .attr('d', path).attr('fill', 'none').attr('stroke', (f) => `rgba(76,201,240,${0.08 + f.properties.order * 0.05})`).attr('stroke-width', (f) => 0.4 + (f.properties.order - 2) * 0.5);
  svg.append('g').selectAll('path').data(rivers.features.filter((f) => f.properties.order >= 3)).join('path')
    .attr('class', 'fl').attr('d', path).attr('fill', 'none').attr('stroke', 'rgba(128,255,219,0.55)').attr('stroke-width', (f) => 0.6 + (f.properties.order - 3) * 0.5).attr('stroke-dasharray', '3 37');
}
drawBg();
addEventListener('resize', drawBg);
