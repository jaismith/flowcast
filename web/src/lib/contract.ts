/**
 * The JSON the page reads from the forecast backend: a site index and one bundle per site, plus the two lazy-
 * forecasting endpoints. Proposed as v0 from what the page uses; web/CONTRACT.md lists the same fields with
 * units for the backend. Times are Unix seconds (UTC). Arrays of quantiles are flattened row-major, with the
 * quantile index fastest: value(row, q) = values[row * quantiles.length + q]. Missing values are null.
 */

/** `GET sites.json` */
export interface SiteIndex {
  schema: 'flowcast.sites.v0';
  /** Site shown at `/`. */
  default: string;
  sites: SiteSummary[];
}

export interface SiteSummary {
  /** USGS site number without the `USGS-` prefix, e.g. "01427510". Used in `/site/<id>`. */
  id: string;
  /** "Delaware River at Callicoon, NY" */
  name: string;
  /** "Callicoon" */
  short: string;
  /** "Delaware River" */
  river: string;
  /** "Callicoon, NY" */
  place: string;
  lat: number;
  lon: number;
}

/** `GET sites/<id>.json` */
export interface SiteBundle {
  schema: 'flowcast.site.v0';
  generated_at: number;
  site: SiteInfo;
  /** Current USGS stage-discharge rating, for river level and flood stages as flows; null if the gauge has none. */
  rating: Rating | null;
  geo: Geo;
  climatology: Climatology;
  observed: Observed;
  /** Latest flow forecast; null while a snoozed site has never been forecast. */
  flow_forecast: FlowForecast | null;
  /** Latest water-temperature forecast; null if the gauge has no temperature model or none yet. */
  water_temp_forecast: WaterTempForecast | null;
}

export interface SiteInfo extends SiteSummary {
  area_mi2: number;
  forest_frac: number;
  snow_frac: number;
  /** Median daily flow over the record, ft3/s. */
  median_flow_cfs: number;
  /** Longest and mean travel time from the basin to the gauge, hours. */
  travel_time_max_h: number;
  nid_dams: number;
  nid_major_dams: number;
  /** NWS forecast point, e.g. "CCRN6", or null. */
  nws_lid: string | null;
  /** NWS flood categories in ft of stage; any may be missing. Null if the point has none. */
  flood_stage_ft: { action?: number; minor?: number; moderate?: number; major?: number } | null;
  /** USGS Watershed Boundary Dataset names for the basin, or null. */
  watershed: Watershed | null;
}

export interface Watershed {
  /** "HUC-8" */
  level: string;
  huc: string;
  name: string;
  /** The HUC units covering the basin, largest share first. */
  parts: { huc: string; name: string }[];
  source: string;
}

export interface Rating {
  rating_id: string;
  stage_ft: number[];
  flow_cfs: number[];
}

/** GeoJSON in WGS84 lon/lat. */
export interface Geo {
  /** [west, south, east, north] of the basin. */
  bounds: [number, number, number, number];
  /** Basin outline, Polygon or MultiPolygon Feature. */
  basin: GeoJSON.Feature<GeoJSON.Polygon | GeoJSON.MultiPolygon>;
  /** The gauge itself. */
  gauge: GeoJSON.Feature<GeoJSON.Point>;
  /** NHDPlus flowlines digitized upstream to downstream, with Strahler stream order. */
  rivers: GeoJSON.FeatureCollection<GeoJSON.LineString, { order: number }>;
  /** Other gauges in the basin; `q` is the latest flow (ft3/s) or null. */
  gauges: GeoJSON.FeatureCollection<GeoJSON.Point, { name: string; active: boolean; q: number | null }>;
  /** NID dams; the map shows those of 50,000 acre-ft or more. */
  dams: GeoJSON.FeatureCollection<GeoJSON.Point, { name: string; river: string | null; year: number | null; storage_af: number }>;
}

/** Quantiles of the daily mean for each day of year 1-366 (366 rows x 5). */
export interface Climatology {
  /** Always [0.1, 0.25, 0.5, 0.75, 0.9]. */
  quantiles: number[];
  flow_cfs: (number | null)[];
  water_temp_c: (number | null)[];
  /** "2000-2022" */
  years: string;
}

/** A regular series: value k is at t0 + k * step_h hours (hourly values are hour-ending). */
export interface Series {
  t0: number;
  step_h: number;
  v: (number | null)[];
}

/** Gauge and basin observations up to the latest reading, starting at least 7 days before it. */
export interface Observed {
  flow_cfs: Series;
  water_temp_c: Series | null;
  /** Gauge height, if the gauge reports it; otherwise the page converts flow with the rating. */
  stage_ft: Series | null;
  /** Basin-mean snow water equivalent (SNODAS), daily: the snowpack now. */
  swe_mm: Series | null;
  /** Basin-mean precipitation, daily totals. */
  precip_mm: Series;
}

export interface FlowForecast {
  issued_at: number;
  /** Hours after issued_at; hourly at least to 48 h and covering 168 h. */
  leads_h: number[];
  /** Always [0.05, 0.25, 0.5, 0.75, 0.95]. */
  quantiles: number[];
  /** leads_h x quantiles, ft3/s. */
  flow_cfs: (number | null)[];
  /**
   * Water input behind the forecast, in bins of bin_h hours from issued_at to 168 h: GEFS basin-mean
   * precipitation, the share of it falling as snow, and forecast snowmelt (null when the backend has none, in
   * which case the page shows no melt).
   */
  precip: { bin_h: number; mean_mm: number[]; snow_share: number[]; melt_mm: number[] | null };
}

export interface WaterTempForecast {
  issued_at: number;
  /** Hours after issued_at, every hour covering at least 168 h. */
  leads_h: number[];
  quantiles: number[];
  /** leads_h x quantiles, degC. */
  temp_c: (number | null)[];
  /** Each local day's high (quantiles over the members' own peaks). */
  daily_high: { dates: string[]; temp_c: (number | null)[] };
}

/** `GET /api/status?site=<id>` */
export interface SiteStatus {
  /** ready: the bundle has a current forecast. warming: a run is in progress. snoozed: no recent visits, not forecasting. */
  state: 'ready' | 'warming' | 'snoozed';
  /** Latest forecast issue time, or null if none. */
  issued_at: number | null;
  /** Seconds until a warming forecast is expected, if known. */
  eta_s?: number | null;
}
