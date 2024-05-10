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
      style={{ width: '180px', boxSizing: 'border-box' }}
    >
      <Text size='xs'>Water Temperature</Text>
      <Flex>
        <Title order={1} size={48} fw={600} style={{ lineHeight: 1.05 }}>
          {observation.watertemp?.toLocaleString(undefined, { maximumFractionDigits: 1 })}
        </Title>
        <Text size='xl'>°F</Text>
      </Flex>
      <Space h={8} />
      <Text size='xs'>Stream Flow</Text>
      <Flex>
        <Title order={3} fw='normal'>
          {observation.streamflow?.toLocaleString(undefined, { maximumFractionDigits: 0 })}
        </Title>
        <Text size='sm' style={{ paddingLeft: 2 }}>ft{String.fromCharCode(179)}/s</Text>
      </Flex>
      <Space h='sm' />
      <Divider />
      <Space h='sm' />
      {/* <Text size='xs' fs='italic'>
        {observation.type === 'hist'
          ? <>This value is an <b>actual measurement</b>, retrieved from the USGS monitoring station at the selected location. It is accurate to within ~0.5 degrees Fahrenheit.</>
          : <>This is a <b>forecasted value</b>, generated based on independent atmospheric weather forecasts and historical water temperature at this location.</>}
      </Text> */}
      <Text size='xs' fs='italic'>
        {observation.type === 'hist'
          ? <>This value is an <b>actual USGS measurement</b> from</>
          : <>This is a <b>forecasted value</b> expected on</>}
        {' ' + dayjs(observation.timestamp).format('MMM D, YYYY [at] H:mm')}
      </Text>
    </Card>
  );
}

export default Tooltip;
