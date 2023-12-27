import { Box, Chip, Flex, Group, Select, Stack, Text } from '@mantine/core';
import { useEffect, useState } from 'react';

const SELECTOR_LABEL_PROPS = { size: 'xs', fw: 500 };

const PRESET_TIMEFRAMES = {
  '14 days': 14 * 24,
  '1 month': 30 * 24,
  '3 months': 3 * 30 * 24
};

type SelectorProps = {
  setFeatures: (features: string[]) => void,
  setTimeframe: (timeframe: number) => void
};

const Selector = (props: SelectorProps) => {
  const [featureSelection, setFeatureSelection] = useState(['watertemp']);
  const [timeframeSelection, setTimeframeSelection] = useState(Object.keys(PRESET_TIMEFRAMES)[0]);

  useEffect(() => {
    props.setFeatures(featureSelection)
  }, [props, featureSelection]);
  useEffect(() => {
    if (timeframeSelection == null) return;
    // if (timeframeSelection === 'custom') {

    // }
    props.setTimeframe(PRESET_TIMEFRAMES[timeframeSelection as keyof typeof PRESET_TIMEFRAMES])
  });

  return (
    <Flex style={{ width: '100%' }}>
      <Stack gap={5}>
        <Text {...SELECTOR_LABEL_PROPS}>Location</Text>
        <Select
          placeholder='enter a location'
          data={['Callicoon, NY']}
          defaultValue='Callicoon, NY'
          searchable
          disabled
          styles={{
            input: {
              color: 'black'
            }
          }}
        />
      </Stack>
      <Stack gap={5} style={{ marginLeft: 20 }}>
        <Text {...SELECTOR_LABEL_PROPS}>Features</Text>
        <Chip.Group
          multiple
          value={featureSelection}
          onChange={setFeatureSelection}
        >
          <Group
            style={{
              marginTop: '3px'
            }}
          >
            <Chip value='watertemp' disabled>Water Temperature</Chip>
            <Chip value='streamflow' disabled>Stream Flow</Chip>
          </Group>
        </Chip.Group>
      </Stack>
      <Box style={{ flexGrow: 1 }} />
      <Stack align='end' gap={5}>
        <Text {...SELECTOR_LABEL_PROPS}>Timeframe</Text>
        <Flex align='center' gap='sm'>
          <Select
            data={[...Object.keys(PRESET_TIMEFRAMES)]}//, 'custom']} // todo
            value={timeframeSelection}
            onChange={value => !!value && setTimeframeSelection(value)}
            disabled
            style={{
              width: 110
            }}
          />
        </Flex>
      </Stack>
    </Flex>
  );
}

export default Selector;
