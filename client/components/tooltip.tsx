import { Card, Divider, Flex, Space, Text, Title } from '@mantine/core';
import dayjs from 'dayjs';
import relativeTime from 'dayjs/plugin/relativeTime';

import type { Observation } from '../utils/types';

dayjs.extend(relativeTime);

type TooltipProps = {
  observation: Observation,
};

const Tooltip = ({
  observation
}: TooltipProps) => {
  return (
    <Card
      shadow='sm'
      padding='md'
      radius='md'
      style={{ maxWidth: '200px' }}
    >
      <Flex>
        <Title order={1}>
          {observation.watertemp}
        </Title>
        <Text size='xl'>°F</Text>
      </Flex>
      <Text size='xs'>
        as of {dayjs(observation.timestamp).fromNow()}
      </Text>
      <Space h='sm' />
      <Divider />
      <Space h='sm' />
      <Text size='xs' fs='italic'>
        This value is an <b>actual measurement</b>, retrieved from the USGS monitoring station at the selected location. It is accurate to within ~0.5 degrees Fahrenheit.
      </Text>
    </Card>
  );
}

export default Tooltip;
