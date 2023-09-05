import dayjs from 'dayjs';

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
  const range = [(start % interval) + interval];
  while (range[range.length - 1] > end) range.push(range[range.length - 1] + interval);
  return range;
};
