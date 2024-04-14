import { Box, Button, Checkbox, Chip, Flex, Group, Popover, Select, Slider, SliderProps, Space, Stack, Switch, Text } from '@mantine/core';
import { IconChevronDown } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { COLORS } from '../utils/constants';

const SELECTOR_LABEL_PROPS = { size: 'xs', fw: 500 };

export const PRESET_TIMEFRAMES: { [label: string]: number } = {
  '10 days': 10 * 24,
  '14 days': 14 * 24,
  '1 month': 30 * 24,
  '3 months': 3 * 30 * 24
};

export const PRESET_ACCURACY_HORIZONS = {
  '6h': 6 * 60 * 60,
  '12h': 12 * 60 * 60,
  '1d': 24 * 60 * 60,
  '2d': 2 * 24 * 60 * 60
};
export const DEFAULT_ACCURACY_HORIZON_IDX = 2;
const PRESET_ACCURACY_HORIZON_MARKS = Object.keys(PRESET_ACCURACY_HORIZONS)
.reduce((marks: NonNullable<SliderProps['marks']>, label, idx) => {
  marks.push({ label, value: idx * 100 / (Object.keys(PRESET_ACCURACY_HORIZONS).length - 1) });
  return marks;
}, []);

type SelectorProps = {
  setFeatures: (features: string[]) => void,
  setTimeframe: (timeframe: number) => void,
  setShowHistoricalAccuracy: (show: boolean) => void,
  setHistoricalAccuracyHorizon: (horizon: number) => void
};

const Selector = (props: SelectorProps) => {
  const [featureSelection, setFeatureSelection] = useState(['watertemp']);
  const [timeframeSelection, setTimeframeSelection] = useState(Object.keys(PRESET_TIMEFRAMES)[0]);
  const [showHistoricalAccuracy, setShowHistoricalAccuracy]= useState(false);
  const [historicalAccuracyHorizon, setHistoricalAccuracyHorizon] = useState(Object.values(PRESET_ACCURACY_HORIZONS)[DEFAULT_ACCURACY_HORIZON_IDX]);

  useEffect(() => {
    props.setFeatures(featureSelection)
  }, [props, featureSelection]);
  useEffect(() => {
    if (timeframeSelection == null) return;
    // todo support custom timeframe
    props.setTimeframe(PRESET_TIMEFRAMES[timeframeSelection as keyof typeof PRESET_TIMEFRAMES])
  }, [props, timeframeSelection]);
  useEffect(() => {
    props.setShowHistoricalAccuracy(showHistoricalAccuracy);
    props.setHistoricalAccuracyHorizon(historicalAccuracyHorizon);
  }, [props, showHistoricalAccuracy, historicalAccuracyHorizon]);

  const handleSliderChange = (val: number) => {
    const label = PRESET_ACCURACY_HORIZON_MARKS.find(({ value }) => value === val)?.label;
    if (label) {
      const horizon = PRESET_ACCURACY_HORIZONS[label as keyof typeof PRESET_ACCURACY_HORIZONS];
      if (horizon) setHistoricalAccuracyHorizon(horizon);
    }
  };

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
            gap={10}
          >
            <Chip value='watertemp' color={COLORS.CARROT_ORANGE}>Water Temperature</Chip>
            <Chip value='streamflow' color={COLORS.VISTA_BLUE}>Stream Flow</Chip>
          </Group>
        </Chip.Group>
      </Stack>
      <Box style={{ flexGrow: 1 }} />
      <Stack align='end' justify='end' gap={5}>
        <Popover position="bottom-end">
          <Popover.Target>
            <Button variant='light' style={{ paddingLeft: 5, paddingRight: 5 }}>
              <IconChevronDown size={25}/>
            </Button>
          </Popover.Target>
          <Popover.Dropdown>
            <Stack>
              <Text {...SELECTOR_LABEL_PROPS}>Show Historical Accuracy</Text>
              <Flex align='center' style={{ marginBottom: 10 }}>
                <Checkbox
                  size='md'
                  style={{ marginRight: 10 }}
                  onChange={(event) => setShowHistoricalAccuracy(event.target.checked)}
                />
                <Slider
                  style={{ width: 150 }}
                  marks={PRESET_ACCURACY_HORIZON_MARKS}
                  step={100 / (Object.keys(PRESET_ACCURACY_HORIZON_MARKS).length - 1)}
                  defaultValue={PRESET_ACCURACY_HORIZON_MARKS[DEFAULT_ACCURACY_HORIZON_IDX].value}
                  onChange={handleSliderChange}
                  label={null}
                />
              </Flex>
            </Stack>
          </Popover.Dropdown>
        </Popover>
      </Stack>
      <Stack align='end' gap={5} style={{ marginLeft: 10 }}>
        <Text {...SELECTOR_LABEL_PROPS}>Timeframe</Text>
        <Flex align='center' gap='sm'>
          <Select
            data={[...Object.keys(PRESET_TIMEFRAMES)]}//, 'custom']} // todo
            value={timeframeSelection}
            onChange={value => !!value && setTimeframeSelection(value)}
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
