export const COLORS = {
  VISTA_BLUE: '#7B9BD8',
  MAGNOLIA:'#E7E5EE',
  FRENCH_GRAY:'#BDBCD3',
  DAVY_GRAY:'#48494D',
  SAFFRON: '#E8C547',
  CARROT_ORANGE: '#F39237'
};

export const FORECAST_HORIZON = 24 * 7;

export const ACCESS_API_ROOT = 'https://api.flowcast.jaismith.dev';

export const ACCESS_API_WSS = 'wss://ozyrx6ken2.execute-api.us-east-1.amazonaws.com/prod';

export const MAPBOX_TOKEN = process.env.NEXT_PUBLIC_MAPBOX_TOKEN ?? '';

export const USGS_SITES_API = 'https://waterservices.usgs.gov/nwis/site/';

export const USGS_IV_API = 'https://waterservices.usgs.gov/nwis/iv/';
