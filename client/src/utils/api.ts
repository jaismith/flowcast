import axios from 'axios';

import type { Datapoint } from 'utils/types';
import { ACCESS_API_ROOT } from 'utils/constants';

export const getForecast = async (): Promise<Datapoint[]> => {
  let data = [];

  try {
    const res = await axios.get(ACCESS_API_ROOT);
    data = res.data.map((d: any) => ({
      ...d,
      ds: new Date(d.ds)
    }));
  } catch (err) {
    console.error('error fetching data from ', ACCESS_API_ROOT, err)
  }

  return data;
};
