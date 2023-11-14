import dayjs from 'dayjs';
import { Datapoint, DatapointFeatures } from './types';

export const ctof = (c: number) => (c * 1.8) + 32;

export const getDateRange = (start: Date, end: Date, interval: number = 6) => {
  const startdt = dayjs(start);
  const enddt = dayjs(end);

  const range = [startdt.subtract(startdt.hour() % interval, 'hour').add(interval, 'hour')];
  let cur = range[range.length - 1].add(interval, 'hour');
  while (cur.isBefore(enddt)) {
    range.push(cur);
    cur = cur.add(interval, 'hour');
  }

  return range.map(d => d.toDate());
};

export const getNumericRange = (start: number, end: number, interval: number) => {
  const range = [start - (start % interval)];
  while (range[range.length - 1] < end) range.push(range[range.length - 1] + interval);
  return range;
};

export const getAxisBounds = (data: Datapoint[], keys: (keyof DatapointFeatures)[], pad: number) => {
  const [min, max] = data.reduce(([min, max], datapoint) => {
    keys.forEach(key => {
      const value = datapoint[key];
      if (value < min) min = value;
      else if (value > max) max = value;
    });
    return [min, max];
  }, [data[0][keys[0]], data[0][keys[0]]]);
  console.log([min - pad, max + pad]);
  return [min - pad, max + pad];
};

export const getAxisTickValues = (data: Datapoint[], keys: (keyof DatapointFeatures)[], pad: number, interval: number) => {
  const [min, max] = getAxisBounds(data, keys, pad);
  return getNumericRange(min, max, interval);
}
