import type { TickRendererProps } from '@visx/axis';

import dayjs from 'dayjs';
import { Text } from '@visx/text';

export const AxisLabel = ({
  formattedValue,
  x,
  y
}: TickRendererProps) => {
  const date = dayjs(formattedValue);

  return (
    <g transform={`translate(${x}, ${y})`}>
      <Text
        verticalAnchor='end'
        textAnchor='middle'
        fontWeight='900'
        fill='#dddddd'
      >
        {date.format('DD')}
      </Text>
      <Text
        y={5}
        verticalAnchor='start'
        textAnchor='middle'
        fontWeight='300'
        fill="#dddddd"
        opacity={.6}
      >
        {date.format('ddd')}
      </Text>
    </g>
  );
};
