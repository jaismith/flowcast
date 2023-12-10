import { Chip, Flex, Group, Select, SimpleGrid, Stack, Text } from '@mantine/core';
import { useRef, useState } from 'react';

const SELECTOR_LABEL_PROPS = { size: 'xs', fw: 500 };

const Selector = () => {
  const [features, setFeatures] = useState(['watertemp']);
  const locationRef = useRef(null);

  return (
    <Flex>
      <Stack gap={5}>
        <Text {...SELECTOR_LABEL_PROPS}>Location</Text>
        <Select
          ref={locationRef}
          placeholder='enter a location'
          data={['Callicoon, NY']}
          searchable
          defaultValue='Callicoon, NY'
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
          value={features}
          onChange={setFeatures}
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
    </Flex>
  );
}

export default Selector;
