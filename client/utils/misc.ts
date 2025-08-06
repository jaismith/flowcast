export const ctof = (c: number) => (c * 1.8) + 32;

export const getUsgsSiteUrl = (site_no: string) => `https://waterdata.usgs.gov/monitoring-location/${site_no}`;
