import { Card, Divider, Flex, Space, Text, Title } from '@mantine/core';
import dayjs from 'dayjs';
import relativeTime from 'dayjs/plugin/relativeTime';

import type { Observation } from '../utils/types';

dayjs.extend(relativeTime);

type TooltipProps = {
  observation: Observation,
  latest: boolean
};

const Tooltip = ({
  observation,
  latest
}: TooltipProps) => {
  return (
    <Card
      shadow='sm'
      padding='sm'
      radius='md'
      style={{ width: '200px', boxSizing: 'border-box' }}
    >
      <Flex>
        <Title order={1}>
          {observation.watertemp.toLocaleString(undefined, { maximumFractionDigits: 1 })}
        </Title>
        <Text size='xl'>°F</Text>
      </Flex>
      <Text size='xs'>
        {latest
          ? <>as of {dayjs(observation.timestamp).fromNow()}</>
          : <>{observation.type === 'fcst' && 'expected'} {dayjs(observation.timestamp).format('MMM D, YYYY H:mm')}</>}
      </Text>
      <Space h='sm' />
      <Divider />
      <Space h='sm' />
      <Text size='xs' fs='italic'>
        {observation.type === 'hist'
          ? <>This value is an <b>actual measurement</b>, retrieved from the USGS monitoring station at the selected location. It is accurate to within ~0.5 degrees Fahrenheit.</>
          : <>This is a <b>forecasted value</b>, generated based on independent atmospheric weather forecasts and historical water temperature at this location.</>}
      </Text>
    </Card>
  );
}

export default Tooltip;
