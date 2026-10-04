/**
 * The serving backend's data contract, v1 (serving/schema/ on cursor/production-backend-30da, PR #64): the
 * fields the page reads, typed. Times are ISO 8601 UTC strings; series are hour-ending. Optional fields may be
 * missing or null for some sites; the page drops what depends on them.
 */

export type Time = string;
export type SiteId = `USGS-${string}`;
export type Status = 'active' | 'snoozed' | 'waking' | 'delayed' | 'paused';
export type FloodCategory = 'action' | 'minor' | 'moderate' | 'major';

/** Regular series: values[i] is at start + i * step_h hours. */
export interface Series {
  unit: string;
  start: Time;
  step_h: number;
  values: (number | null)[];
}

export interface QuantileSeries {
  unit: string;
  start: Time;
  step_h: number;
  q05: (number | null)[];
  q25: (number | null)[];
  q50: (number | null)[];
  q75: (number | null)[];
  q95: (number | null)[];
}

export interface ForecastPointer {
  /** YYYYMMDDHH, UTC */
  issue: string;
  issue_time: Time;
  /** /data/v1/sites/{id}/forecasts/{issue}.json (immutable) */
  url: string;
  age_h?: number;
}

/** `GET /data/v1/sites.json` */
export interface SiteIndex {
  schema: 'flowcast.sites/v1';
  generated: Time;
  /** The site shown at `/`. */
  default: SiteId;
  sites: SiteSummary[];
}

export interface SiteSummary {
  id: SiteId;
  slug: string | null;
  name: string;
  river: string;
  town: string;
  state: string;
  lat: number;
  lon: number;
  area_mi2: number;
  /** Water temperature is forecast; false = flow-only. */
  has_temp: boolean;
  in_training_region: boolean;
  /** True once the first forecast is published (then live.json exists). Every listed site can be woken. */
  forecast_ready: boolean;
  forecast_issued_at: Time | null;
  status?: Status | null;
  live_url?: string | null;
}

/** `GET /data/v1/sites/{id}/live.json` */
export interface Live {
  schema: 'flowcast.live/v1';
  id: SiteId;
  generated: Time;
  status: Status;
  always_on?: boolean;
  /** End of the visit-driven active window; null for always-on or snoozed sites. */
  awake_until?: Time | null;
  /** Newest forecast; null if the site has never been forecast. */
  forecast: ForecastPointer | null;
  static_url: string;
  now: {
    observed_at: Time | null;
    flow_cfs: number | null;
    stage_ft: number | null;
    water_temp_c: number | null;
    gauge_stale?: boolean;
  };
  /** Last 30 days, hourly. */
  observations: { discharge?: Series; water_temperature?: Series; stage?: Series };
}

/** `GET /data/v1/sites/{id}/static.json` */
export interface Static {
  schema: 'flowcast.static/v1';
  id: SiteId;
  name: string;
  short_name?: string;
  river?: string;
  lat: number;
  lon: number;
  timezone: string;
  has_temperature?: boolean;
  nws_lid?: string | null;
  usgs_url?: string;
  basin?: {
    area_sq_mi?: number;
    forest_frac?: number | null;
    frac_snow?: number | null;
    n_major_dams?: number | null;
    /** NID dams inside the basin (the page shows "3 of 100"). */
    n_dams?: number | null;
    /** Longest travel time from the headwaters to the gauge, hours. */
    travel_time_max_h?: number | null;
    /** Median daily mean flow, training years, ft3/s. */
    median_flow_cfs?: number | null;
  };
  /** NWS flood categories; flow_cfs (the stage through the USGS rating, or NWPS's flow) draws the flood bands. */
  flood_categories?: { category: FloodCategory; stage_ft: number; flow_cfs: number | null; flow_source?: string }[];
  /** Basin outline, rivers, gauges and dams. */
  geometry?: Geometry | null;
  watershed_description?: string | null;
  /** USGS Watershed Boundary Dataset: the smallest unit covering most of the basin, and its parts. */
  watershed?: { level: string; huc: string; name: string; parts: { huc: string; name: string; share?: number }[]; source: string } | null;
  /** Quantiles of the daily mean by day of year, for "normal for the date" bands and the Now status. */
  climatology?: Climatology | null;
}

/** static.geometry. GeoJSON in WGS84 lon/lat. */
export interface Geometry {
  /** [west, south, east, north] of the basin. */
  bounds: [number, number, number, number];
  basin: GeoJSON.Feature<GeoJSON.Polygon | GeoJSON.MultiPolygon>;
  /** NHDPlus flowlines digitized upstream to downstream, with Strahler order. */
  rivers: GeoJSON.FeatureCollection<GeoJSON.LineString, { order: number }>;
  gauges: GeoJSON.FeatureCollection<GeoJSON.Point, { name: string; active: boolean; q: number | null }>;
  dams: GeoJSON.FeatureCollection<GeoJSON.Point, { name: string; river: string | null; year: number | null; storage_af: number }>;
}

/** Day-of-year quantiles [0.1, 0.25, 0.5, 0.75, 0.9] of the daily mean: 366 rows of 5. */
export interface Climatology {
  quantiles: number[];
  years?: string;
  flow_cfs: (number | null)[][];
  water_temp_c: (number | null)[][] | null;
}

/** `GET` a forecast pointer's url */
export interface Forecast {
  schema: 'flowcast.forecast/v1';
  id: SiteId;
  issue: string;
  issue_time: Time;
  /** Hourly discharge, leads 1-168 h. */
  flow: QuantileSeries;
  temperature?: {
    /** Hourly water temperature, leads 1-180 h, degC. */
    hourly: QuantileSeries;
    daily_max: { date: string; lead_day: number; q05: number; q25: number; q50: number; q75: number; q95: number }[];
  } | null;
  weather?: {
    /** 6 h bins ahead; bin i covers (start + (i-1)*step_h, start + i*step_h]. mm per bin. */
    bins: { start: Time; step_h: number; rain_mm?: (number | null)[]; snow_mm?: (number | null)[]; snowmelt_mm?: (number | null)[] | null };
    /** Observed rain, last 72 h before issue, 6 h bins, mm. */
    past_rain?: Series;
    /** SNODAS basin-mean SWE at issue, mm. */
    snowpack_swe_mm?: number | null;
  };
}

/** Error body of /api answers: 404 unknown_site (not a USGS id) or not_supported (not a model basin). */
export interface ApiError {
  error: 'unknown_site' | 'not_supported' | 'method_not_allowed' | 'no_route';
  detail?: string;
}

/** `POST /api/visit?site={id}` and `GET /api/status?site={id}` */
export interface ApiStatus {
  id: SiteId;
  status: Status;
  awake_until?: Time | null;
  forecast: ForecastPointer | null;
  eta_s?: number | null;
}
